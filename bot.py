"""
ForexSignalBot — Bot Sinyal Forex via Telegram
Strategi : Breakout S/R + EMA Crossover
Data     : Alpha Vantage (gratis)
Deploy   : Railway.app / Render.com (GRATIS, tanpa PC)
"""

import os, asyncio, logging
from datetime import datetime
import requests
import pandas as pd
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

# ─── Logging ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── Config dari Environment Variable ─────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "ISI_TOKEN_KAMU")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "ISI_CHAT_ID_KAMU")
AV_API_KEY       = os.environ.get("AV_API_KEY",       "ISI_API_KEY_KAMU")

# ─── Pairs & Strategi ─────────────────────────────────────
MAJOR_PAIRS = [
    ("EUR", "USD"), ("GBP", "USD"), ("USD", "JPY"),
    ("USD", "CHF"), ("AUD", "USD"), ("NZD", "USD"),
    ("USD", "CAD"), ("EUR", "GBP"),
]
FAST_MA         = 20
SLOW_MA         = 50
SR_LOOKBACK     = 50
BREAKOUT_BUFFER = 0.0005   # ~5 pips untuk pair non-JPY

# ─── State ────────────────────────────────────────────────
bot_active    = False
signal_history = []

# ══════════════════════════════════════════════════════════
# DATA HARGA — Alpha Vantage (Gratis 25 req/hari)
# ══════════════════════════════════════════════════════════

def fetch_candles(from_sym: str, to_sym: str, interval: str = "60min") -> pd.DataFrame:
    """Ambil data OHLC dari Alpha Vantage."""
    url = (
        f"https://www.alphavantage.co/query"
        f"?function=FX_INTRADAY"
        f"&from_symbol={from_sym}"
        f"&to_symbol={to_sym}"
        f"&interval={interval}"
        f"&outputsize=compact"
        f"&apikey={AV_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        key = f"Time Series FX ({interval})"
        if key not in data:
            log.warning(f"No data for {from_sym}{to_sym}: {data.get('Note','')}")
            return pd.DataFrame()

        rows = []
        for ts, vals in data[key].items():
            rows.append({
                "time":  pd.to_datetime(ts),
                "open":  float(vals["1. open"]),
                "high":  float(vals["2. high"]),
                "low":   float(vals["3. low"]),
                "close": float(vals["4. close"]),
            })
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        return df
    except Exception as e:
        log.error(f"fetch_candles error {from_sym}{to_sym}: {e}")
        return pd.DataFrame()

def fetch_price(from_sym: str, to_sym: str) -> float | None:
    """Ambil harga terkini."""
    url = (
        f"https://www.alphavantage.co/query"
        f"?function=CURRENCY_EXCHANGE_RATE"
        f"&from_currency={from_sym}"
        f"&to_currency={to_sym}"
        f"&apikey={AV_API_KEY}"
    )
    try:
        r    = requests.get(url, timeout=10)
        data = r.json()
        rate = data["Realtime Currency Exchange Rate"]["5. Exchange Rate"]
        return float(rate)
    except Exception as e:
        log.error(f"fetch_price error {from_sym}{to_sym}: {e}")
        return None

# ══════════════════════════════════════════════════════════
# ANALISA STRATEGI
# ══════════════════════════════════════════════════════════

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def analyze_pair(from_sym: str, to_sym: str) -> dict | None:
    symbol = f"{from_sym}{to_sym}"
    df = fetch_candles(from_sym, to_sym)
    if df.empty or len(df) < SLOW_MA + 5:
        return None

    # EMA Crossover
    fast = calc_ema(df["close"], FAST_MA)
    slow = calc_ema(df["close"], SLOW_MA)

    ma_bull = (fast.iloc[-3] < slow.iloc[-3]) and (fast.iloc[-2] > slow.iloc[-2])
    ma_bear = (fast.iloc[-3] > slow.iloc[-3]) and (fast.iloc[-2] < slow.iloc[-2])

    # Support & Resistance
    recent     = df.iloc[-SR_LOOKBACK-1:-1]
    resistance = recent["high"].max()
    support    = recent["low"].min()

    current_price = df["close"].iloc[-1]
    buf = BREAKOUT_BUFFER if "JPY" not in symbol else BREAKOUT_BUFFER * 100

    breakout_up   = current_price > resistance + buf
    breakout_down = current_price < support    - buf

    signal = None
    if ma_bull and breakout_up:
        signal = "BUY"
    elif ma_bear and breakout_down:
        signal = "SELL"

    # Hitung SL & TP
    pip = 0.0001 if "JPY" not in symbol else 0.01
    sl_dist = 30 * pip
    tp_dist = 60 * pip

    result = {
        "symbol":     symbol,
        "signal":     signal,
        "price":      current_price,
        "fast_ema":   round(fast.iloc[-2], 5),
        "slow_ema":   round(slow.iloc[-2], 5),
        "support":    round(support, 5),
        "resistance": round(resistance, 5),
        "sl_buy":     round(current_price - sl_dist, 5),
        "tp_buy":     round(current_price + tp_dist, 5),
        "sl_sell":    round(current_price + sl_dist, 5),
        "tp_sell":    round(current_price - tp_dist, 5),
        "ma_trend":   "🟢 Bullish" if fast.iloc[-2] > slow.iloc[-2] else "🔴 Bearish",
        "time":       datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }
    return result

def is_trading_session() -> bool:
    now = datetime.utcnow()
    if now.weekday() >= 5:
        return False
    h = now.hour
    return (7 <= h < 16) or (13 <= h < 20)

# ══════════════════════════════════════════════════════════
# TELEGRAM BOT
# ══════════════════════════════════════════════════════════

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Aktifkan Bot",  callback_data="start"),
         InlineKeyboardButton("⏹ Hentikan Bot",  callback_data="stop")],
        [InlineKeyboardButton("🔍 Scan Sinyal",   callback_data="scan"),
         InlineKeyboardButton("📊 Status",        callback_data="status")],
        [InlineKeyboardButton("💹 Harga Sekarang",callback_data="prices")],
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    await update.message.reply_text(
        "🤖 *ForexSignalBot* aktif!\n\n"
        "Strategi: *Breakout S/R + EMA Crossover*\n"
        "Pairs: EURUSD, GBPUSD, USDJPY, USDCHF,\n"
        "          AUDUSD, NZDUSD, USDCAD, EURGBP\n\n"
        "📌 Bot akan kirim sinyal BUY/SELL otomatis.\n"
        "Kamu eksekusi manual di app FBS kamu.",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    query = update.callback_query
    await query.answer()

    if str(query.message.chat_id) != str(TELEGRAM_CHAT_ID):
        return

    data = query.data

    # ── AKTIFKAN ──
    if data == "start":
        bot_active = True
        await query.edit_message_text(
            "✅ *Bot AKTIF!*\n\n"
            "Sinyal akan dikirim otomatis setiap jam.\n"
            "Pastikan kamu buka app FBS untuk eksekusi.",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    # ── HENTIKAN ──
    elif data == "stop":
        bot_active = False
        await query.edit_message_text(
            "⏹ *Bot dihentikan.*\n\nKetuk ▶️ untuk aktifkan kembali.",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    # ── STATUS ──
    elif data == "status":
        state   = "🟢 AKTIF" if bot_active else "🔴 BERHENTI"
        session = "🟢 Dalam sesi" if is_trading_session() else "🔴 Di luar sesi"
        total_signals = len(signal_history)
        buys  = sum(1 for s in signal_history if s["signal"] == "BUY")
        sells = sum(1 for s in signal_history if s["signal"] == "SELL")
        await query.edit_message_text(
            f"📊 *Status Bot*\n\n"
            f"Bot     : {state}\n"
            f"Sesi    : {session}\n"
            f"Sinyal  : {total_signals} total ({buys} BUY / {sells} SELL)\n"
            f"Pairs   : {len(MAJOR_PAIRS)} major pair\n"
            f"Strategi: EMA {FAST_MA}/{SLOW_MA} + Breakout S/R",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    # ── SCAN MANUAL ──
    elif data == "scan":
        await query.edit_message_text(
            "🔍 *Scanning semua pair...*\n\nMohon tunggu ~30 detik.",
            parse_mode="Markdown"
        )
        results = []
        for from_sym, to_sym in MAJOR_PAIRS:
            res = analyze_pair(from_sym, to_sym)
            if res:
                results.append(res)
            await asyncio.sleep(2)   # hindari rate limit API

        found = [r for r in results if r["signal"]]
        if not found:
            msg = "🔍 *Hasil Scan*\n\nTidak ada sinyal kuat saat ini.\nPasar sedang konsolidasi."
        else:
            msg = "🔍 *Hasil Scan — Sinyal Ditemukan!*\n\n"
            for r in found:
                emoji = "🟢" if r["signal"] == "BUY" else "🔴"
                sl  = r["sl_buy"]  if r["signal"] == "BUY" else r["sl_sell"]
                tp  = r["tp_buy"]  if r["signal"] == "BUY" else r["tp_sell"]
                msg += (
                    f"{emoji} *{r['symbol']}* — {r['signal']}\n"
                    f"   Harga : `{r['price']}`\n"
                    f"   SL    : `{sl}`\n"
                    f"   TP    : `{tp}`\n"
                    f"   Trend : {r['ma_trend']}\n\n"
                )
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=msg,
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    # ── HARGA SEKARANG ──
    elif data == "prices":
        await query.edit_message_text(
            "💹 *Mengambil harga terkini...*", parse_mode="Markdown"
        )
        msg = "💹 *Harga Terkini*\n\n"
        for from_sym, to_sym in MAJOR_PAIRS:
            price = fetch_price(from_sym, to_sym)
            sym   = f"{from_sym}{to_sym}"
            if price:
                msg += f"`{sym:<10}` : `{price}`\n"
            else:
                msg += f"`{sym:<10}` : _-_\n"
            await asyncio.sleep(1)
        msg += f"\n🕐 {datetime.utcnow().strftime('%H:%M UTC')}"
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=msg,
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

# ══════════════════════════════════════════════════════════
# JOB: AUTO SCAN SETIAP JAM
# ══════════════════════════════════════════════════════════

async def auto_scan(context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if not bot_active:
        return
    if not is_trading_session():
        return

    log.info("Auto-scan dimulai...")
    for from_sym, to_sym in MAJOR_PAIRS:
        res = analyze_pair(from_sym, to_sym)
        await asyncio.sleep(3)
        if not res or not res["signal"]:
            continue

        signal_history.append(res)
        emoji = "🟢" if res["signal"] == "BUY" else "🔴"
        sl  = res["sl_buy"]  if res["signal"] == "BUY" else res["sl_sell"]
        tp  = res["tp_buy"]  if res["signal"] == "BUY" else res["tp_sell"]

        msg = (
            f"{emoji} *SINYAL {res['signal']} — {res['symbol']}*\n\n"
            f"📌 *Aksi di FBS kamu:*\n"
            f"   Buka posisi *{res['signal']}* pada `{res['price']}`\n"
            f"   Set Stop Loss  : `{sl}`\n"
            f"   Set Take Profit: `{tp}`\n\n"
            f"📊 *Detail Analisa:*\n"
            f"   EMA {FAST_MA}: `{res['fast_ema']}`\n"
            f"   EMA {SLOW_MA}: `{res['slow_ema']}`\n"
            f"   Support   : `{res['support']}`\n"
            f"   Resistance: `{res['resistance']}`\n"
            f"   Tren MA   : {res['ma_trend']}\n\n"
            f"🕐 {res['time']}\n"
            f"⚠️ _Eksekusi manual di app FBS_"
        )
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown"
        )
        log.info(f"Sinyal dikirim: {res['signal']} {res['symbol']}")

# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════

def main():
    log.info("ForexSignalBot starting...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_start))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Auto scan setiap 1 jam
    app.job_queue.run_repeating(auto_scan, interval=3600, first=30)

    log.info("Bot berjalan. Kirim /start di Telegram!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
