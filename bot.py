import os
import time
import json
import logging
import requests
from datetime import datetime, timezone

# ── CONFIGURAZIONE ───────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TWELVEDATA_KEY  = os.environ.get("TWELVEDATA_KEY", "")
CHAT_ID         = os.environ.get("CHAT_ID", "351600461")
CHAT_ID_2       = os.environ.get("CHAT_ID_2", "")
NOTION_TOKEN    = os.environ.get("NOTION_TOKEN", "")
NOTION_DB_ID    = os.environ.get("NOTION_DB_ID", "")

CAPITALE_INIZIALE = float(os.environ.get("CAPITALE", "500"))
RISCHIO_PERC      = float(os.environ.get("RISCHIO_PERC", "4"))
MAX_TRADE         = int(os.environ.get("MAX_TRADE", "6"))
MAX_SIZE_PERC     = float(os.environ.get("MAX_SIZE_PERC", "20"))
STOP_LOSS_PERC    = float(os.environ.get("STOP_LOSS_PERC", "2"))
TAKE_PROFIT_R     = float(os.environ.get("TAKE_PROFIT_R", "2"))
MAX_LOSS_GIORNO   = float(os.environ.get("MAX_LOSS_GIORNO", "10"))
MAX_LOSS_TOTALE   = float(os.environ.get("MAX_LOSS_TOTALE", "25"))
SCAN_INTERVAL     = int(os.environ.get("SCAN_INTERVAL", "3600"))

ASSETS = [
    {"symbol": "EUR/USD",  "name": "Euro / Dollaro",    "type": "forex"},
    {"symbol": "GBP/USD",  "name": "Sterlina / Dollaro", "type": "forex"},
    {"symbol": "XAU/USD",  "name": "Oro",                "type": "commodity"},
    {"symbol": "WTI/USD",  "name": "Petrolio WTI",       "type": "commodity"},
    {"symbol": "SPY",      "name": "ETF S&P 500",         "type": "equity"},
    {"symbol": "QQQ",      "name": "ETF Nasdaq",          "type": "equity"},
]

# Fasce orarie operative per tipo asset (ora CET = UTC+1, CEST = UTC+2)
# Il bot usa UTC internamente e aggiunge 1 ora per CET
FASCE_ORARIE = {
    "forex":     {"giorni": [0,1,2,3,4], "apertura": 7,  "chiusura": 19},  # lun-ven 08-20 CET
    "commodity": {"giorni": [0,1,2,3,4], "apertura": 8,  "chiusura": 20},  # lun-ven 09-21 CET
    "equity":    {"giorni": [0,1,2,3,4], "apertura": 14, "chiusura": 21},  # lun-ven 15-22 CET (NYSE)
}

STATE_FILE = "stato.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── PERSISTENZA STATO ─────────────────────────────────────────────────────────
def stato_default() -> dict:
    return {
        "capitale":            CAPITALE_INIZIALE,
        "capitale_iniziale":   CAPITALE_INIZIALE,
        "trade_aperti":        [],
        "perdita_giorno":      0.0,
        "perdita_totale":      0.0,
        "perdite_consecutive": 0,
        "bloccato":            False,
        "ultimo_blocco":       None,
        "data_reset_giorno":   datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

def carica_stato() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            dati = json.load(f)
            log.info(f"Stato caricato: capitale €{dati.get('capitale', '?')}, "
                     f"trade aperti: {len(dati.get('trade_aperti', []))}")
            return dati
    except FileNotFoundError:
        log.info("Nessun stato salvato trovato — parto da zero")
        return stato_default()
    except Exception as e:
        log.error(f"Errore caricamento stato: {e} — parto da zero")
        return stato_default()

def salva_stato() -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(stato, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Errore salvataggio stato: {e}")

def reset_perdita_giorno() -> None:
    """Resetta la perdita giornaliera a mezzanotte UTC."""
    oggi = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if stato.get("data_reset_giorno") != oggi:
        stato["perdita_giorno"]    = 0.0
        stato["data_reset_giorno"] = oggi
        salva_stato()
        log.info("Reset perdita giornaliera")

# Carica stato all'avvio
stato = carica_stato()

# ── FILTRO ORARI DI MERCATO ───────────────────────────────────────────────────
def mercato_aperto(asset_type: str) -> bool:
    """Controlla se il mercato è aperto per il tipo di asset (orari CET)."""
    now_utc     = datetime.now(timezone.utc)
    ora_cet     = now_utc.hour + 1          # approssimazione CET (UTC+1)
    giorno      = now_utc.weekday()         # 0=lunedì, 6=domenica
    fascia      = FASCE_ORARIE.get(asset_type)
    if not fascia:
        return False
    if giorno not in fascia["giorni"]:
        return False
    return fascia["apertura"] <= ora_cet < fascia["chiusura"]

def prossima_apertura() -> str:
    """Restituisce un messaggio leggibile sulla prossima apertura."""
    now_utc = datetime.now(timezone.utc)
    giorno  = now_utc.weekday()
    if giorno >= 5:
        return "I mercati riaprono lunedì mattina."
    return "I mercati riaprono domani mattina."

# ── NOTION ────────────────────────────────────────────────────────────────────
def notion_log(tipo: str, dati: dict) -> None:
    """Scrive una riga nel database Notion."""
    if not NOTION_TOKEN or not NOTION_DB_ID:
        log.warning("Notion non configurato — log saltato")
        return
    url     = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization":  f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type":   "application/json",
    }

    ora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    props = {
        "Nome":     {"title": [{"text": {"content": dati.get("nome", tipo)}}]},
        "Tipo":     {"select": {"name": tipo}},
        "Asset":    {"rich_text": [{"text": {"content": dati.get("asset", "")}}]},
        "Direzione":{"rich_text": [{"text": {"content": dati.get("direzione", "")}}]},
        "Capitale": {"number": dati.get("capitale", stato["capitale"])},
        "Data":     {"date": {"start": ora}},
    }

    # Campi opzionali
    if "size" in dati:
        props["Size (EUR)"]    = {"number": dati["size"]}
    if "rischio_eur" in dati:
        props["Rischio (EUR)"] = {"number": dati["rischio_eur"]}
    if "ingresso" in dati:
        props["Ingresso"]      = {"number": dati["ingresso"]}
    if "stop_loss" in dati:
        props["Stop Loss"]     = {"number": dati["stop_loss"]}
    if "take_profit" in dati:
        props["Take Profit"]   = {"number": dati["take_profit"]}
    if "risultato_eur" in dati:
        props["Risultato (EUR)"] = {"number": dati["risultato_eur"]}
    if "note" in dati:
        props["Note"] = {"rich_text": [{"text": {"content": dati["note"]}}]}

    payload = {"parent": {"database_id": NOTION_DB_ID}, "properties": props}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code != 200:
            log.error(f"Notion error {r.status_code}: {r.text[:200]}")
        else:
            log.info(f"Notion: log '{tipo}' scritto")
    except Exception as e:
        log.error(f"Errore Notion: {e}")

# ── TELEGRAM ─────────────────────────────────────────────────────────────────
def invia_messaggio(testo: str) -> None:
    """Broadcast a tutti gli utenti configurati."""
    if not TELEGRAM_TOKEN:
        return
    url         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    destinatari = [d for d in [CHAT_ID, CHAT_ID_2] if d]
    for chat in destinatari:
        try:
            requests.post(url, json={"chat_id": chat, "text": testo, "parse_mode": "HTML"}, timeout=10)
        except Exception as e:
            log.error(f"Errore invio a {chat}: {e}")

def invia_messaggio_a(testo: str, chat_id: str) -> None:
    """Risposta personale a un singolo utente."""
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": testo, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"Errore invio a {chat_id}: {e}")

def get_aggiornamenti(offset: int = 0) -> list:
    if not TELEGRAM_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=10)
        return r.json().get("result", [])
    except Exception as e:
        log.error(f"Errore getUpdates: {e}")
        return []

# ── TWELVE DATA ───────────────────────────────────────────────────────────────
def get_prezzi(symbol: str, interval: str = "1h", outputsize: int = 60):
    if not TWELVEDATA_KEY:
        return None
    url    = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval,
              "outputsize": outputsize, "apikey": TWELVEDATA_KEY, "format": "JSON"}
    try:
        r    = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("status") == "error":
            log.warning(f"{symbol}: {data.get('message', 'errore API')}")
            return None
        valori = data.get("values", [])
        return [float(v["close"]) for v in reversed(valori)] if valori else None
    except Exception as e:
        log.error(f"Errore fetch {symbol}: {e}")
        return None

# ── INDICATORI ────────────────────────────────────────────────────────────────
def ema(prezzi: list, periodo: int) -> list:
    if len(prezzi) < periodo:
        return []
    k      = 2 / (periodo + 1)
    result = [sum(prezzi[:periodo]) / periodo]
    for p in prezzi[periodo:]:
        result.append(p * k + result[-1] * (1 - k))
    return result

def calcola_rsi(prezzi: list, periodo: int = 14) -> float:
    if len(prezzi) < periodo + 1:
        return 50.0
    delta    = [prezzi[i] - prezzi[i - 1] for i in range(1, len(prezzi))]
    guadagni = [d if d > 0 else 0 for d in delta[-periodo:]]
    perdite  = [-d if d < 0 else 0 for d in delta[-periodo:]]
    avg_g    = sum(guadagni) / periodo
    avg_p    = sum(perdite)  / periodo
    if avg_p == 0:
        return 100.0
    return 100 - (100 / (1 + avg_g / avg_p))

def trend_direzione(e21: list, e50: list) -> str:
    if len(e21) < 2 or len(e50) < 2:
        return "laterale"
    if e21[-1] > e50[-1] and e21[-2] > e50[-2]:
        return "rialzista"
    if e21[-1] < e50[-1] and e21[-2] < e50[-2]:
        return "ribassista"
    return "laterale"

# ── STRATEGIA ─────────────────────────────────────────────────────────────────
def analizza_asset(asset: dict) -> dict | None:
    if not mercato_aperto(asset["type"]):
        log.info(f"{asset['symbol']}: mercato chiuso — skip")
        return None

    symbol = asset["symbol"]
    prezzi = get_prezzi(symbol)
    if not prezzi or len(prezzi) < 55:
        log.info(f"{symbol}: dati insufficienti")
        return None

    e21   = ema(prezzi, 21)
    e50   = ema(prezzi, 50)
    rsi   = calcola_rsi(prezzi, 14)
    if not e21 or not e50:
        return None

    trend          = trend_direzione(e21, e50)
    prezzo         = prezzi[-1]
    ema21_att      = e21[-1]
    ema21_prec     = e21[-2]

    if trend == "laterale":
        return None

    segnale = None
    if trend == "rialzista" and prezzi[-2] <= ema21_prec * 1.005 and prezzo > ema21_att and 40 < rsi < 70:
        segnale = "LONG"
    elif trend == "ribassista" and prezzi[-2] >= ema21_prec * 0.995 and prezzo < ema21_att and 30 < rsi < 60:
        segnale = "SHORT"

    if not segnale:
        return None

    if segnale == "LONG":
        stop_loss   = round(prezzo * (1 - STOP_LOSS_PERC / 100), 5)
        take_profit = round(prezzo * (1 + STOP_LOSS_PERC / 100 * TAKE_PROFIT_R), 5)
    else:
        stop_loss   = round(prezzo * (1 + STOP_LOSS_PERC / 100), 5)
        take_profit = round(prezzo * (1 - STOP_LOSS_PERC / 100 * TAKE_PROFIT_R), 5)

    distanza_sl     = abs(prezzo - stop_loss) / prezzo * 100
    fattore_rischio = RISCHIO_PERC
    if stato["perdite_consecutive"] >= 3:
        fattore_rischio = 2.0
    elif stato["perdite_consecutive"] == 2:
        fattore_rischio = 3.0

    rischio_eur = round(stato["capitale"] * fattore_rischio / 100, 2)
    size        = round(rischio_eur / (distanza_sl / 100), 2)
    size        = min(size, round(stato["capitale"] * MAX_SIZE_PERC / 100, 2))

    if rsi > 60 and trend == "rialzista":     forza = "ALTA"
    elif rsi < 40 and trend == "ribassista":  forza = "ALTA"
    elif 50 < rsi < 65 or 35 < rsi < 50:     forza = "MEDIA"
    else:                                      forza = "BASSA"

    return {
        "asset":       asset,
        "segnale":     segnale,
        "prezzo":      prezzo,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "rr":          round(TAKE_PROFIT_R, 1),
        "size":        size,
        "rischio_eur": rischio_eur,
        "forza":       forza,
        "motivo":      f"Rimbalzo su EMA21 in trend {trend}. RSI {round(rsi, 1)}.",
        "trend":       trend,
        "rsi":         round(rsi, 1),
        "distanza_sl": round(distanza_sl, 2),
    }

# ── CONTROLLI RISCHIO ─────────────────────────────────────────────────────────
def rischio_ok() -> tuple[bool, str]:
    if stato["bloccato"]:
        return False, "operativita_bloccata"
    if stato["capitale"] > 0:
        if abs(stato["perdita_giorno"]) / stato["capitale"] * 100 >= MAX_LOSS_GIORNO:
            return False, "perdita_giornaliera"
    if abs(stato["perdita_totale"]) / stato["capitale_iniziale"] * 100 >= MAX_LOSS_TOTALE:
        stato["bloccato"] = True
        salva_stato()
        return False, "perdita_totale"
    if len(stato["trade_aperti"]) >= MAX_TRADE:
        return False, "max_trade_raggiunto"
    return True, "ok"

# ── MESSAGGI ──────────────────────────────────────────────────────────────────
def msg_avvio() -> str:
    return (
        "🤖 <b>BOT TRADING ATTIVO</b>\n\n"
        f"💼 Capitale: €{stato['capitale']}\n"
        f"⚠️ Rischio per trade: {RISCHIO_PERC}%\n"
        f"📊 Max trade: {MAX_TRADE}\n\n"
        "Asset monitorati:\n"
        + "\n".join(f"  • {a['name']} ({a['symbol']})" for a in ASSETS)
        + "\n\nFasce orarie attive (CET):\n"
        "  • Forex: lun-ven 08:00–20:00\n"
        "  • Commodity: lun-ven 09:00–21:00\n"
        "  • Equity/ETF: lun-ven 15:00–22:00\n\n"
        "Comandi:\n"
        "/signals /today /risk /help"
    )

def msg_scansione(risultati: list) -> str:
    ora   = datetime.now(timezone.utc).strftime("%H:%M UTC")
    linee = [f"📡 <b>SCANSIONE — {len(risultati)} opportunità</b>  ({ora})\n"]
    for i, r in enumerate(risultati):
        freccia  = "🟢" if r["segnale"] == "LONG" else "🔴"
        priorita = "⭐ PRIORITÀ 1 (consigliato)" if i == 0 else f"{i + 1}."
        linee += [
            priorita,
            f"{freccia} <b>{r['asset']['name']}</b> — {r['segnale']}",
            f"💰 Ingresso: {r['prezzo']}",
            f"🛑 Stop: {r['stop_loss']}  (–{r['distanza_sl']}%)",
            f"🎯 Target: {r['take_profit']}  (+{round(r['distanza_sl']*r['rr'],2)}%)",
            f"📊 R/R: 1:{r['rr']}  |  Forza: {r['forza']}",
            f"📦 Size: €{r['size']}  |  Rischio: €{r['rischio_eur']}",
            f"📋 {r['motivo']}",
        ]
        if i < len(risultati) - 1:
            linee.append("────────────────")
    linee += [
        f"\n💼 Capitale: €{stato['capitale']}",
        f"⚠️ Trade aperti: {len(stato['trade_aperti'])} su {MAX_TRADE}",
        "\n👇 Rispondi con:",
    ]
    for i in range(len(risultati)):
        linee.append(f"/seguo{i+1} → {risultati[i]['asset']['name']}")
    linee.append("/nessuno → non apro nulla")
    return "\n".join(linee)

def msg_nessun_segnale() -> str:
    ora          = datetime.now(timezone.utc).strftime("%H:%M UTC")
    aperti_ora   = [a["symbol"] for a in ASSETS if mercato_aperto(a["type"])]
    chiusi_ora   = [a["symbol"] for a in ASSETS if not mercato_aperto(a["type"])]
    linee = [f"😶 <b>NESSUN SEGNALE</b>  ({ora})\n"]
    if aperti_ora:
        linee.append(f"Analizzati: {', '.join(aperti_ora)}")
    if chiusi_ora:
        linee.append(f"Mercato chiuso: {', '.join(chiusi_ora)}")
    linee.append("\nProssima scansione tra 1 ora.\nRestare fermi è una decisione valida.")
    return "\n".join(linee)

def msg_blocco(motivo: str) -> str:
    motivi = {
        "perdita_giornaliera":  f"Perdita giornaliera ≥{MAX_LOSS_GIORNO}%",
        "perdita_totale":       f"Perdita totale ≥{MAX_LOSS_TOTALE}%. Operatività bloccata.",
        "operativita_bloccata": "Operatività bloccata per perdita totale eccessiva.",
    }
    return (
        f"🚨 <b>BLOCCO OPERATIVITÀ</b>\n\n"
        f"Motivo: {motivi.get(motivo, motivo)}\n"
        f"💼 Capitale: €{stato['capitale']}\n\n"
        "❌ Nessun nuovo segnale.\n"
        "✅ Riprenderà alla prossima sessione.\n\n"
        "👉 Non aprire posizioni manualmente."
    )

# ── GESTIONE COMANDI ──────────────────────────────────────────────────────────
def gestisci_comando(testo: str, chat_id_mittente: str, segnali_correnti: list) -> tuple[str | None, str | None]:
    testo = testo.strip().lower()

    if testo == "/start":
        return msg_avvio(), chat_id_mittente

    if testo == "/help":
        return (
            "📖 <b>GUIDA COMANDI</b>\n\n"
            "/signals — segnali attivi ora\n"
            "/today   — riepilogo giornata\n"
            "/risk    — stato del rischio\n"
            "/help    — questa guida\n\n"
            "Quando ricevi opportunità:\n"
            "/seguo1  → apro il trade prioritario\n"
            "/seguo2  → apro il secondo trade\n"
            "/nessuno → non apro nulla"
        ), chat_id_mittente

    if testo == "/risk":
        pg   = round(abs(stato["perdita_giorno"]), 2)
        pt   = round(abs(stato["perdita_totale"]), 2)
        pg_p = round(pg / stato["capitale"] * 100, 1) if stato["capitale"] > 0 else 0
        pt_p = round(pt / stato["capitale_iniziale"] * 100, 1) if stato["capitale_iniziale"] > 0 else 0
        return (
            f"📊 <b>STATO RISCHIO</b>\n\n"
            f"💼 Capitale attuale: €{stato['capitale']}\n"
            f"💼 Capitale iniziale: €{stato['capitale_iniziale']}\n\n"
            f"Trade aperti: {len(stato['trade_aperti'])} su {MAX_TRADE}\n"
            f"Perdita oggi: –€{pg} ({pg_p}% su {MAX_LOSS_GIORNO}% max)\n"
            f"Perdita totale: –€{pt} ({pt_p}% su {MAX_LOSS_TOTALE}% max)\n"
            f"Perdite consecutive: {stato['perdite_consecutive']}\n"
            f"Operatività: {'🚨 BLOCCATA' if stato['bloccato'] else '✅ Attiva'}"
        ), chat_id_mittente

    if testo == "/today":
        trade_oggi = stato["trade_aperti"]
        return (
            f"📅 <b>RIEPILOGO GIORNATA</b>\n\n"
            f"💼 Capitale: €{stato['capitale']}\n"
            f"Trade aperti: {len(trade_oggi)}\n"
            f"Perdita oggi: –€{round(abs(stato['perdita_giorno']), 2)}\n\n"
            + ("Nessun trade aperto." if not trade_oggi
               else "\n".join(f"• {t['nome']} {t['segnale']} — ingresso {t['ingresso']}" for t in trade_oggi))
        ), chat_id_mittente

    if testo == "/signals":
        if not segnali_correnti:
            return "📡 Nessun segnale attivo. Il bot scansiona ogni ora.", chat_id_mittente
        return msg_scansione(segnali_correnti), chat_id_mittente

    if testo == "/nessuno":
        return "👍 Ok, nessuna posizione aperta. Il bot continua a monitorare.", chat_id_mittente

    for i, s in enumerate(segnali_correnti):
        if testo == f"/seguo{i + 1}":
            trade = {
                "id":          f"{s['asset']['symbol']}_{int(time.time())}",
                "nome":        s["asset"]["name"],
                "symbol":      s["asset"]["symbol"],
                "segnale":     s["segnale"],
                "ingresso":    s["prezzo"],
                "stop_loss":   s["stop_loss"],
                "take_profit": s["take_profit"],
                "size":        s["size"],
                "rischio_eur": s["rischio_eur"],
                "aperto_il":   datetime.now(timezone.utc).isoformat(),
            }
            stato["trade_aperti"].append(trade)
            salva_stato()

            # Log su Notion
            notion_log("Trade aperto", {
                "nome":        f"APERTO — {s['asset']['name']} {s['segnale']}",
                "asset":       s["asset"]["symbol"],
                "direzione":   s["segnale"],
                "size":        s["size"],
                "rischio_eur": s["rischio_eur"],
                "ingresso":    s["prezzo"],
                "stop_loss":   s["stop_loss"],
                "take_profit": s["take_profit"],
                "capitale":    stato["capitale"],
                "note":        s["motivo"],
            })

            return (
                f"✅ <b>Registrato — {s['asset']['name']} {s['segnale']}</b>\n\n"
                f"Ingresso: {s['prezzo']}\n"
                f"Stop Loss: {s['stop_loss']}\n"
                f"Take Profit: {s['take_profit']}\n"
                f"Size: €{s['size']}\n\n"
                f"📌 Apri ora la posizione sul tuo broker.\n"
                f"💼 Impegnato: €{s['size']} ({round(s['size']/stato['capitale']*100,1)}%)\n"
                f"⚠️ Rischio: €{s['rischio_eur']}\n\n"
                f"📒 Operazione registrata su Notion."
            ), chat_id_mittente

    return None, None

# ── LOOP PRINCIPALE ───────────────────────────────────────────────────────────
def main():
    log.info("Bot avviato")
    invia_messaggio(msg_avvio())

    # Log avvio su Notion
    notion_log("Avvio bot", {
        "nome":     "Bot avviato",
        "asset":    "—",
        "direzione":"—",
        "capitale": stato["capitale"],
        "note":     f"Capitale: €{stato['capitale']} | Trade aperti: {len(stato['trade_aperti'])}",
    })

    offset           = 0
    segnali_correnti = []
    ultima_scansione = 0
    ultimo_snapshot  = 0

    while True:
        reset_perdita_giorno()

        # 1. Gestione comandi
        aggiornamenti = get_aggiornamenti(offset)
        for upd in aggiornamenti:
            offset           = upd["update_id"] + 1
            msg              = upd.get("message", {})
            testo            = msg.get("text", "")
            chat_id_mittente = str(msg.get("chat", {}).get("id", CHAT_ID))
            if testo:
                risposta, dest = gestisci_comando(testo, chat_id_mittente, segnali_correnti)
                if risposta and dest:
                    invia_messaggio_a(risposta, dest)

        # 2. Scansione mercato
        if time.time() - ultima_scansione >= SCAN_INTERVAL:
            ultima_scansione = time.time()
            log.info("Avvio scansione...")

            ok, motivo = rischio_ok()
            if not ok:
                if motivo != "max_trade_raggiunto":
                    invia_messaggio(msg_blocco(motivo))
                log.info(f"Scansione saltata: {motivo}")
            else:
                risultati = []
                for asset in ASSETS:
                    time.sleep(1)
                    r = analizza_asset(asset)
                    if r:
                        risultati.append(r)
                        log.info(f"Segnale: {asset['symbol']} {r['segnale']}")
                    else:
                        log.info(f"Nessun segnale: {asset['symbol']}")

                risultati.sort(key=lambda x: {"ALTA": 0, "MEDIA": 1, "BASSA": 2}.get(x["forza"], 3))
                segnali_correnti = risultati

                if risultati:
                    invia_messaggio(msg_scansione(risultati))
                else:
                    invia_messaggio(msg_nessun_segnale())

        # 3. Snapshot orario su Notion
        if time.time() - ultimo_snapshot >= 3600:
            ultimo_snapshot = time.time()
            notion_log("Snapshot orario", {
                "nome":     f"Snapshot — Capitale €{stato['capitale']}",
                "asset":    "—",
                "direzione":"—",
                "capitale": stato["capitale"],
                "note":     f"Trade aperti: {len(stato['trade_aperti'])} | "
                            f"Perdita oggi: €{round(abs(stato['perdita_giorno']),2)} | "
                            f"Perdita totale: €{round(abs(stato['perdita_totale']),2)}",
            })
            salva_stato()

        time.sleep(2)

if __name__ == "__main__":
    main()
