import asyncio
import requests
import numpy as np
from datetime import datetime, timezone, time as dtime
from telegram import Bot
import nest_asyncio
import traceback
import sys

nest_asyncio.apply()

# === CONFIGURATION ===
TELEGRAM_TOKEN = '7831038886:AAE1kESVsdtZyJ3AtZXIUy-rMTSlDBGlkac'
CHAT_ID = 969925512
CAPITAL_TOTAL = 1000
SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
    'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'MATICUSDT', 'DOTUSDT',
    'ARBUSDT', 'OPUSDT', 'SHIBUSDT', 'LTCUSDT', 'LINKUSDT',
    'PEPEUSDT', 'INJUSDT', 'WLDUSDT', 'RUNEUSDT', 'APTUSDT'
]
INTERVAL = '1h'
LIMIT = 100
SLEEP_SECONDS = 300
bot = Bot(token=TELEGRAM_TOKEN)
trades = {}
history = []

def get_klines(symbol):
    url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={INTERVAL}&limit={LIMIT}'
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def compute_rsi(prices, period=14):
    deltas = np.diff(prices)
    gains = np.maximum(deltas, 0)
    losses = np.maximum(-deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    rs = avg_gain / avg_loss if avg_loss != 0 else 0
    return 100 - (100 / (1 + rs))

def compute_macd(prices, short=12, long=26, signal=9):
    ema_short = np.convolve(prices, np.ones(short)/short, mode='valid')
    ema_long = np.convolve(prices, np.ones(long)/long, mode='valid')
    macd_line = ema_short[-len(ema_long):] - ema_long
    signal_line = np.convolve(macd_line, np.ones(signal)/signal, mode='valid')
    return macd_line[-1], signal_line[-1]

def is_uptrend(prices, period=50):
    ma = np.mean(prices[-period:])
    return prices[-1] > ma

async def process_symbol(symbol):
    try:
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        price = closes[-1]
        rsi = compute_rsi(closes)
        macd, signal = compute_macd(closes)
        buy = False
        confidence = None
        label = ""
        position_size = 0

        if (rsi > 30 and compute_rsi(closes[:-1]) < 30 and macd > signal and is_uptrend(closes)):
            buy = True
            confidence = 9
            label = "\ud83d\udc8e Signal tr\u00e8s fiable \u2013 Fiabilit\u00e9 9/10"
            position_size = CAPITAL_TOTAL * 0.07

        sell = False
        if symbol in trades:
            entry = trades[symbol]['entry']
            gain_pct = ((price - entry) / entry) * 100
            if gain_pct >= 3 or gain_pct <= -1.5:
                sell = True

        if buy and symbol not in trades:
            trades[symbol] = {
                "entry": price,
                "time": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'),
                "confidence": confidence
            }
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"\ud83d\udfe2 Achat sur {symbol} \u00e0 {price:.2f}\n{label}\n\ud83d\udcb0 Capital sugg\u00e9r\u00e9 : {position_size:.2f} €"
            )

        elif sell and symbol in trades:
            entry = trades[symbol]['entry']
            gain_pct = ((price - entry) / entry) * 100
            confidence = trades[symbol].get("confidence", "?")
            emoji = "\ud83d\udc8e" if confidence >= 8 else "\u26a0\ufe0f"
            history.append({"symbol": symbol, "entry": entry, "exit": price, "result": gain_pct, "confidence": confidence})
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"\ud83d\udd34 Vente {symbol} \u00e0 {price:.2f}\nEntr\u00e9e: {entry:.2f}\nR\u00e9sultat: {'+' if gain_pct >= 0 else ''}{gain_pct:.2f}%\n{emoji} Fiabilit\u00e9: {confidence}/10"
            )
            del trades[symbol]

    except Exception as e:
        traceback.print_exc()

async def send_daily_summary():
    if not history:
        return
    lines = ["\ud83c\udf1f R\u00e9capitulatif des trades (24h) :"]
    for h in history[-50:]:
        emoji = "\ud83d\udcc8" if h["result"] > 0 else "\ud83d\udcc9"
        lines.append(f"{emoji} {h['symbol']} | Entr\u00e9e: {h['entry']:.2f} | Sortie: {h['exit']:.2f} | Gain: {h['result']:.2f}%")
    await bot.send_message(chat_id=CHAT_ID, text="\n".join(lines))

async def main_loop():
    await bot.send_message(chat_id=CHAT_ID, text=f"\ud83d\ude80 Bot d\u00e9marr\u00e9 \u00e0 {datetime.now().strftime('%H:%M:%S')}")
    last_heartbeat_hour = None
    last_daily_summary_sent = False

    while True:
        try:
            now = datetime.now()
            if last_heartbeat_hour != now.hour:
                last_heartbeat_hour = now.hour
                await bot.send_message(chat_id=CHAT_ID, text=f"\u2705 Bot actif \u00e0 {now.strftime('%H:%M')} (heartbeat)")

            # Résumé quotidien à 10h00
            if now.hour == 10 and not last_daily_summary_sent:
                await send_daily_summary()
                last_daily_summary_sent = True
            elif now.hour != 10:
                last_daily_summary_sent = False

            await asyncio.gather(*(process_symbol(sym) for sym in SYMBOLS))

        except Exception as e:
            err = traceback.format_exc()
            await bot.send_message(chat_id=CHAT_ID, text=f"\u26a0\ufe0f Erreur :\n{err}")

        await asyncio.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main_loop())
    except Exception as e:
        err = traceback.format_exc()
        print(f"\u274c Crash : {e}", flush=True)
        loop.run_until_complete(bot.send_message(
            chat_id=CHAT_ID,
            text=f"\u274c Le bot a crash\u00e9 :\n{err}"
        ))
    finally:
        loop.run_until_complete(bot.send_message(
            chat_id=CHAT_ID,
            text="\u26a0\ufe0f Le bot s'est arr\u00eat\u00e9."
        ))
