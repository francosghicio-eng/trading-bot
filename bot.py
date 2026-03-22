import os
import time
import logging
import requests
from datetime import datetime, timezone

# ── CONFIGURAZIONE ───────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TWELVEDATA_KEY  = os.environ.get("TWELVEDATA_KEY", "")
CHAT_ID         = os.environ.get("CHAT_ID", "351600461")

CAPITALE        = float(os.environ.get("CAPITALE", "500"))
RISCHIO_PERC    = float(os.environ.get("RISCHIO_PERC", "4"))   # % rischio per trade
MAX_TRADE       = int(os.environ.get("MAX_TRADE", "6"))
MAX_SIZE_PERC   = float(os.environ.get("MAX_SIZE_PERC", "20")) # % max per trade
STOP_LOSS_PERC  = float(os.environ.get("STOP_LOSS_PERC", "2")) # % stop loss
TAKE_PROFIT_R   = float(os.environ.get("TAKE_PROFIT_R", "2"))  # moltiplicatore R/R
MAX_LOSS_GIORNO = float(os.environ.get("MAX_LOSS_GIORNO", "10"))# % perdita max giornaliera
MAX_LOSS_TOTALE = float(os.environ.get("MAX_LOSS_TOTALE", "25"))# % perdita max totale

SCAN_INTERVAL   = int(os.environ.get("SCAN_INTERVAL", "3600")) # secondi tra scansioni

ASSETS = [
    {"symbol": "EUR/USD",  "name": "Euro / Dollaro",   "type": "forex"},
    {"symbol": "GBP/USD",  "name": "Sterlina / Dollaro","type": "forex"},
    {"symbol": "XAU/USD",  "name": "Oro",               "type": "commodity"},
    {"symbol": "WTI/USD",  "name": "Petrolio WTI",      "type": "commodity"},
    {"symbol": "SPY",      "name": "ETF S&P 500",        "type": "equity"},
    {"symbol": "QQQ",      "name": "ETF Nasdaq",         "type": "equity"},
]

# ── STATO INTERNO ────────────────────────────────────────────────────────────
stato = {
    "capitale":         CAPITALE,
    "capitale_iniziale":CAPITALE,
    "trade_aperti":     [],
    "perdita_giorno":   0.0,
    "perdita_totale":   0.0,
    "perdite_consecutive": 0,
    "bloccato":         False,
    "ultimo_blocco":    None,
    "segnali_inviati":  [],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── TELEGRAM ─────────────────────────────────────────────────────────────────
def invia_messaggio(testo: str) -> bool:
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN non impostato")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": testo, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Errore invio Telegram: {e}")
        return False

# ── TWELVE DATA ───────────────────────────────────────────────────────────────
def get_prezzi(symbol: str, interval: str = "1h", outputsize: int = 50):
    if not TWELVEDATA_KEY:
        log.warning("TWELVEDATA_KEY non impostato")
        return None
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     symbol,
        "interval":   interval,
        "outputsize": outputsize,
        "apikey":     TWELVEDATA_KEY,
        "format":     "JSON",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("status") == "error":
            log.warning(f"{symbol}: {data.get('message','errore API')}")
            return None
        valori = data.get("values", [])
        if not valori:
            return None
        prezzi = [float(v["close"]) for v in reversed(valori)]
        return prezzi
    except Exception as e:
        log.error(f"Errore fetch {symbol}: {e}")
        return None

# ── INDICATORI ────────────────────────────────────────────────────────────────
def ema(prezzi: list, periodo: int) -> list:
    if len(prezzi) < periodo:
        return []
    k = 2 / (periodo + 1)
    result = [sum(prezzi[:periodo]) / periodo]
    for p in prezzi[periodo:]:
        result.append(p * k + result[-1] * (1 - k))
    return result

def calcola_rsi(prezzi: list, periodo: int = 14) -> float:
    if len(prezzi) < periodo + 1:
        return 50.0
    delta = [prezzi[i] - prezzi[i-1] for i in range(1, len(prezzi))]
    guadagni = [d if d > 0 else 0 for d in delta[-periodo:]]
    perdite  = [-d if d < 0 else 0 for d in delta[-periodo:]]
    avg_g = sum(guadagni) / periodo
    avg_p = sum(perdite)  / periodo
    if avg_p == 0:
        return 100.0
    rs = avg_g / avg_p
    return 100 - (100 / (1 + rs))

def trend_direzione(ema_21: list, ema_50: list) -> str:
    if len(ema_21) < 2 or len(ema_50) < 2:
        return "laterale"
    if ema_21[-1] > ema_50[-1] and ema_21[-2] > ema_50[-2]:
        return "rialzista"
    if ema_21[-1] < ema_50[-1] and ema_21[-2] < ema_50[-2]:
        return "ribassista"
    return "laterale"

# ── STRATEGIA: TREND FOLLOWING EMA 21 ────────────────────────────────────────
def analizza_asset(asset: dict) -> dict | None:
    symbol = asset["symbol"]
    prezzi = get_prezzi(symbol, interval="1h", outputsize=60)
    if not prezzi or len(prezzi) < 55:
        log.info(f"{symbol}: dati insufficienti")
        return None

    e21 = ema(prezzi, 21)
    e50 = ema(prezzi, 50)
    rsi = calcola_rsi(prezzi, 14)

    if not e21 or not e50:
        return None

    trend = trend_direzione(e21, e50)
    prezzo_attuale = prezzi[-1]
    ema21_attuale  = e21[-1]
    ema21_prec     = e21[-2]

    if trend == "laterale":
        return None

    segnale = None

    # LONG: trend rialzista, prezzo rimbalza su EMA21
    if (trend == "rialzista"
            and prezzi[-2] <= ema21_prec * 1.005
            and prezzo_attuale > ema21_attuale
            and 40 < rsi < 70):
        segnale = "LONG"

    # SHORT: trend ribassista, prezzo rimbalza sotto EMA21
    elif (trend == "ribassista"
            and prezzi[-2] >= ema21_prec * 0.995
            and prezzo_attuale < ema21_attuale
            and 30 < rsi < 60):
        segnale = "SHORT"

    if not segnale:
        return None

    # Calcolo livelli
    if segnale == "LONG":
        stop_loss   = round(prezzo_attuale * (1 - STOP_LOSS_PERC / 100), 5)
        take_profit = round(prezzo_attuale * (1 + STOP_LOSS_PERC / 100 * TAKE_PROFIT_R), 5)
    else:
        stop_loss   = round(prezzo_attuale * (1 + STOP_LOSS_PERC / 100), 5)
        take_profit = round(prezzo_attuale * (1 - STOP_LOSS_PERC / 100 * TAKE_PROFIT_R), 5)

    distanza_sl  = abs(prezzo_attuale - stop_loss) / prezzo_attuale * 100
    rr           = round(TAKE_PROFIT_R, 1)

    # Position sizing
    fattore_rischio = RISCHIO_PERC
    if stato["perdite_consecutive"] >= 3:
        fattore_rischio = 2.0
    elif stato["perdite_consecutive"] == 2:
        fattore_rischio = 3.0

    rischio_eur = round(stato["capitale"] * fattore_rischio / 100, 2)
    size        = round(rischio_eur / (distanza_sl / 100), 2)
    size_max    = round(stato["capitale"] * MAX_SIZE_PERC / 100, 2)
    size        = min(size, size_max)

    # Forza segnale
    if rsi > 60 and trend == "rialzista":
        forza = "ALTA"
    elif rsi < 40 and trend == "ribassista":
        forza = "ALTA"
    elif 50 < rsi < 65 or 35 < rsi < 50:
        forza = "MEDIA"
    else:
        forza = "BASSA"

    motivo = f"Rimbalzo su EMA21 in trend {trend}. RSI {round(rsi,1)} — nessun segnale di esaurimento."

    return {
        "asset":          asset,
        "segnale":        segnale,
        "prezzo":         prezzo_attuale,
        "stop_loss":      stop_loss,
        "take_profit":    take_profit,
        "rr":             rr,
        "size":           size,
        "rischio_eur":    rischio_eur,
        "forza":          forza,
        "motivo":         motivo,
        "trend":          trend,
        "rsi":            round(rsi, 1),
        "distanza_sl":    round(distanza_sl, 2),
    }

# ── CONTROLLI RISCHIO ─────────────────────────────────────────────────────────
def rischio_ok() -> tuple[bool, str]:
    if stato["bloccato"]:
        return False, "operativita_bloccata"
    p_giorno = abs(stato["perdita_giorno"]) / stato["capitale"] * 100
    if p_giorno >= MAX_LOSS_GIORNO:
        return False, "perdita_giornaliera"
    p_totale = abs(stato["perdita_totale"]) / stato["capitale_iniziale"] * 100
    if p_totale >= MAX_LOSS_TOTALE:
        stato["bloccato"] = True
        return False, "perdita_totale"
    if len(stato["trade_aperti"]) >= MAX_TRADE:
        return False, "max_trade_raggiunto"
    return True, "ok"

# ── MESSAGGI TELEGRAM ─────────────────────────────────────────────────────────
def msg_scansione(risultati: list) -> str:
    ora = datetime.now(timezone.utc).strftime("%H:%M UTC")
    n = len(risultati)
    linee = [f"📡 <b>SCANSIONE — {n} opportunità trovate</b>  ({ora})\n"]

    for i, r in enumerate(risultati):
        freccia  = "🟢" if r["segnale"] == "LONG" else "🔴"
        priorita = "⭐ PRIORITÀ 1 (consigliato)" if i == 0 else f"{i+1}."
        linee.append(f"{priorita}")
        linee.append(f"{freccia} <b>{r['asset']['name']} ({r['asset']['symbol']})</b> — {r['segnale']}")
        linee.append(f"💰 Ingresso: {r['prezzo']}")
        linee.append(f"🛑 Stop Loss: {r['stop_loss']}  (–{r['distanza_sl']}%)")
        linee.append(f"🎯 Take Profit: {r['take_profit']}  (+{round(r['distanza_sl']*r['rr'],2)}%)")
        linee.append(f"📊 R/R: 1:{r['rr']}  |  Forza: {r['forza']}")
        linee.append(f"📦 Size consigliata: €{r['size']}  |  Rischio: €{r['rischio_eur']}")
        linee.append(f"📋 {r['motivo']}")
        if i < len(risultati) - 1:
            linee.append("────────────────")

    linee.append(f"\n💼 Capitale: €{stato['capitale']}")
    linee.append(f"⚠️ Trade aperti: {len(stato['trade_aperti'])} su {MAX_TRADE} max")
    perdita_g_perc = round(abs(stato['perdita_giorno']) / stato['capitale'] * 100, 1)
    linee.append(f"📉 Perdita giornaliera: {perdita_g_perc}% su {MAX_LOSS_GIORNO}% max")
    linee.append("\n👇 Rispondi con:")
    for i in range(len(risultati)):
        linee.append(f"/seguo{i+1} → apro {risultati[i]['asset']['name']}")
    linee.append("/nessuno → non apro nulla oggi")
    return "\n".join(linee)

def msg_nessun_segnale() -> str:
    ora  = datetime.now(timezone.utc).strftime("%H:%M UTC")
    return (
        f"😶 <b>NESSUN SEGNALE</b>  ({ora})\n\n"
        "Motivo: nessun asset ha completato un setup valido.\n"
        "Il mercato è laterale o i filtri di rischio non sono soddisfatti.\n\n"
        f"Asset monitorati: {', '.join(a['symbol'] for a in ASSETS)}\n"
        "Prossima scansione tra 1 ora.\n\n"
        "Restare fermi è una decisione operativa valida."
    )

def msg_blocco(motivo: str) -> str:
    p_giorno = round(abs(stato['perdita_giorno']), 2)
    p_totale = round(abs(stato['perdita_totale']), 2)
    motivi = {
        "perdita_giornaliera": f"Perdita giornaliera ≥{MAX_LOSS_GIORNO}% (–€{p_giorno})",
        "perdita_totale":      f"Perdita totale ≥{MAX_LOSS_TOTALE}% (–€{p_totale}). Operatività bloccata.",
        "max_trade_raggiunto": f"Già {MAX_TRADE} trade aperti — limite raggiunto.",
        "operativita_bloccata":"Operatività bloccata per perdita totale eccessiva.",
    }
    return (
        f"🚨 <b>BLOCCO OPERATIVITÀ</b>\n\n"
        f"Motivo: {motivi.get(motivo, motivo)}\n"
        f"💼 Capitale attuale: €{stato['capitale']}\n\n"
        "❌ Nessun nuovo segnale verrà inviato.\n"
        "✅ Riprenderà alla prossima sessione.\n\n"
        "👉 Non aprire nessuna posizione manualmente."
    )

def msg_avvio() -> str:
    return (
        "🤖 <b>BOT TRADING ATTIVO</b>\n\n"
        f"💼 Capitale iniziale: €{CAPITALE}\n"
        f"⚠️ Rischio per trade: {RISCHIO_PERC}%\n"
        f"📊 Max trade contemporanei: {MAX_TRADE}\n"
        f"📦 Size massima: {MAX_SIZE_PERC}% del capitale\n\n"
        "Asset monitorati:\n"
        + "\n".join(f"  • {a['name']} ({a['symbol']})" for a in ASSETS)
        + "\n\nStrategia: Trend Following EMA 21\n"
        "Scansione ogni ora durante le ore di mercato.\n\n"
        "Comandi disponibili:\n"
        "/signals — segnali attivi ora\n"
        "/today   — riepilogo giornata\n"
        "/risk    — stato del rischio\n"
        "/help    — guida comandi\n\n"
        "Il bot inizia a scansionare. Ti avviso solo quando trova qualcosa di buono."
    )

# ── GESTIONE COMANDI TELEGRAM ─────────────────────────────────────────────────
def get_aggiornamenti(offset: int = 0) -> list:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=10)
        return r.json().get("result", [])
    except Exception as e:
        log.error(f"Errore getUpdates: {e}")
        return []

def gestisci_comando(testo: str, segnali_correnti: list) -> str | None:
    testo = testo.strip().lower()

    if testo == "/start":
        return msg_avvio()

    if testo == "/help":
        return (
            "📖 <b>GUIDA COMANDI</b>\n\n"
            "/signals — mostra i segnali attivi in questo momento\n"
            "/today   — riepilogo trade e P&L della giornata\n"
            "/risk    — capitale, rischio attivo, limiti\n"
            "/help    — questa guida\n\n"
            "Quando ricevi una scansione con opportunità:\n"
            "/seguo1  → confermi il trade prioritario\n"
            "/seguo2  → confermi il secondo trade\n"
            "/nessuno → non apri nulla\n\n"
            "Il bot ti avvisa automaticamente quando trova segnali validi."
        )

    if testo == "/risk":
        p_giorno = round(abs(stato['perdita_giorno']), 2)
        p_totale = round(abs(stato['perdita_totale']), 2)
        p_giorno_perc = round(abs(stato['perdita_giorno']) / stato['capitale'] * 100, 1) if stato['capitale'] > 0 else 0
        p_totale_perc = round(abs(stato['perdita_totale']) / stato['capitale_iniziale'] * 100, 1) if stato['capitale_iniziale'] > 0 else 0
        return (
            f"📊 <b>STATO RISCHIO</b>\n\n"
            f"💼 Capitale attuale: €{stato['capitale']}\n"
            f"💼 Capitale iniziale: €{stato['capitale_iniziale']}\n\n"
            f"Trade aperti: {len(stato['trade_aperti'])} su {MAX_TRADE} max\n"
            f"Perdita giornaliera: –€{p_giorno} ({p_giorno_perc}% su {MAX_LOSS_GIORNO}% max)\n"
            f"Perdita totale: –€{p_totale} ({p_totale_perc}% su {MAX_LOSS_TOTALE}% max)\n"
            f"Perdite consecutive: {stato['perdite_consecutive']}\n"
            f"Operatività: {'🚨 BLOCCATA' if stato['bloccato'] else '✅ Attiva'}"
        )

    if testo == "/today":
        trade_oggi = [t for t in stato["trade_aperti"]]
        return (
            f"📅 <b>RIEPILOGO GIORNATA</b>\n\n"
            f"💼 Capitale: €{stato['capitale']}\n"
            f"Trade aperti oggi: {len(trade_oggi)}\n"
            f"Perdita giornaliera: –€{round(abs(stato['perdita_giorno']),2)}\n\n"
            + ("Nessun trade aperto al momento." if not trade_oggi
               else "\n".join(f"• {t['nome']} — ingresso €{t['ingresso']}" for t in trade_oggi))
        )

    if testo == "/signals":
        if not segnali_correnti:
            return "📡 Nessun segnale attivo in questo momento. Il bot scansiona ogni ora."
        return msg_scansione(segnali_correnti)

    if testo == "/nessuno":
        return "👍 Ok, nessuna posizione aperta. Il bot continua a monitorare."

    for i, s in enumerate(segnali_correnti):
        if testo == f"/seguo{i+1}":
            stato["trade_aperti"].append({
                "nome":        s["asset"]["name"],
                "symbol":      s["asset"]["symbol"],
                "segnale":     s["segnale"],
                "ingresso":    s["prezzo"],
                "stop_loss":   s["stop_loss"],
                "take_profit": s["take_profit"],
                "size":        s["size"],
                "rischio_eur": s["rischio_eur"],
            })
            return (
                f"✅ <b>Registrato — {s['asset']['name']} {s['segnale']}</b>\n\n"
                f"Ingresso: {s['prezzo']}\n"
                f"Stop Loss: {s['stop_loss']}\n"
                f"Take Profit: {s['take_profit']}\n"
                f"Size: €{s['size']}\n\n"
                f"📌 Apri ora la posizione sul tuo broker.\n"
                f"💼 Capitale impegnato: €{s['size']} ({round(s['size']/stato['capitale']*100,1)}%)\n"
                f"⚠️ Rischio attivo: €{s['rischio_eur']} ({round(s['rischio_eur']/stato['capitale']*100,1)}%)"
            )

    return None

# ── LOOP PRINCIPALE ───────────────────────────────────────────────────────────
def main():
    log.info("Bot avviato")
    invia_messaggio(msg_avvio())

    offset          = 0
    segnali_correnti= []
    ultima_scansione= 0

    while True:
        # Gestione comandi in arrivo
        aggiornamenti = get_aggiornamenti(offset)
        for upd in aggiornamenti:
            offset = upd["update_id"] + 1
            msg    = upd.get("message", {})
            testo  = msg.get("text", "")
            if testo:
                risposta = gestisci_comando(testo, segnali_correnti)
                if risposta:
                    invia_messaggio(risposta)

        # Scansione mercato ogni SCAN_INTERVAL secondi
        ora_attuale = time.time()
        if ora_attuale - ultima_scansione >= SCAN_INTERVAL:
            ultima_scansione = ora_attuale
            log.info("Avvio scansione mercato...")

            ok, motivo = rischio_ok()
            if not ok:
                if motivo != "max_trade_raggiunto":
                    invia_messaggio(msg_blocco(motivo))
                log.info(f"Scansione saltata: {motivo}")
            else:
                risultati = []
                for asset in ASSETS:
                    time.sleep(1)  # rispetta rate limit API
                    r = analizza_asset(asset)
                    if r:
                        risultati.append(r)
                        log.info(f"Segnale trovato: {asset['symbol']} {r['segnale']}")
                    else:
                        log.info(f"Nessun segnale: {asset['symbol']}")

                # Ordina per forza segnale
                ordine_forza = {"ALTA": 0, "MEDIA": 1, "BASSA": 2}
                risultati.sort(key=lambda x: ordine_forza.get(x["forza"], 3))

                segnali_correnti = risultati

                if risultati:
                    invia_messaggio(msg_scansione(risultati))
                else:
                    invia_messaggio(msg_nessun_segnale())

        time.sleep(2)

if __name__ == "__main__":
    main()
