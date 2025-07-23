import asyncio
import requests
import numpy as np
from datetime import datetime, timezone
from telegram import Bot
import nest_asyncio
import traceback
import sys

nest_asyncio.apply()

# === CONFIGURATION ===
TELEGRAM_TOKEN = '7831038886:AAE1kESVsdtZyJ3AtZXIUy-rMTSlDBGlkac'
CHAT_ID = 969925512
SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
    'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'MATICUSDT', 'DOTUSDT',
    'ARBUSDT', 'OPUSDT', 'SHIBUSDT', 'LTCUSDT', 'LINKUSDT',
    'PEPEUSDT', 'INJUSDT', 'WLDUSDT', 'RUNEUSDT', 'APTUSDT'
]
INTERVAL = '1h'
LIMIT = 100
SLEEP_SECONDS = 300  # 5 minutes

bot = Bot(token=TELEGRAM_TOKEN)
trades = {}  # Position mémoire temporaire

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

async def process_symbol(symbol):
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 Analyse de {symbol}", flush=True)
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        price = closes[-1]
        rsi = compute_rsi(closes)
        macd, signal = compute_macd(closes)

        print(f"{symbol} | Price: {price:.2f} | RSI: {rsi:.2f} | MACD: {macd:.4f} | Signal: {signal:.4f}", flush=True)

        buy = rsi < 30 and macd > signal
        sell = False

        if symbol in trades:
            entry = trades[symbol]['entry']
            gain_pct = ((price - entry) / entry) * 100
            print(f"{symbol} | Position ouverte à {entry:.2f} | PnL: {gain_pct:.2f}%", flush=True)
            if gain_pct >= 3 or gain_pct <= -1.5:
                sell = True

        if buy and symbol not in trades:
            trades[symbol] = {"entry": price, "time": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}
            await bot.send_message(chat_id=CHAT_ID, text=f"🟢 Achat détecté sur {symbol} à {price:.2f}")

        elif sell and symbol in trades:
            entry = trades[symbol]['entry']
            gain_pct = ((price - entry) / entry) * 100
            await bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"🔴 Vente sur {symbol} à {price:.2f}\n"
                    f"📈 Entrée: {entry:.2f}\n"
                    f"📊 Résultat: {'+' if gain_pct >= 0 else ''}{gain_pct:.2f}%"
                )
            )
            del trades[symbol]

    except Exception as e:
        print(f"❌ Erreur {symbol}: {e}", flush=True)
        traceback.print_exc()

async def main_loop():
    await bot.send_message(chat_id=CHAT_ID, text=f"🚀 Bot démarré à {datetime.now().strftime('%H:%M:%S')}")
    last_heartbeat_hour = None

    while True:
        try:
            now = datetime.now()
            print(f"🔁 Nouvelle itération à {now.strftime('%H:%M:%S')}", flush=True)

            # Heartbeat Telegram 1x par heure
            if last_heartbeat_hour != now.hour:
                last_heartbeat_hour = now.hour
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"✅ Bot actif à {now.strftime('%H:%M')} (heartbeat automatique)"
                )

            await asyncio.gather(*(process_symbol(sym) for sym in SYMBOLS))
            print("✔️ itération terminée", flush=True)

        except Exception as loop_error:
            err_trace = traceback.format_exc()
            print(f"⚠️ Erreur dans main_loop : {loop_error}", flush=True)
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"⚠️ Erreur dans main_loop : {loop_error}\n\n{err_trace}"
            )
        await asyncio.sleep(SLEEP_SECONDS)

# === EXECUTION ===
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main_loop())
    except Exception as e:
        err = traceback.format_exc()
        print(f"❌ Crash fatal : {e}", flush=True)
        loop.run_until_complete(bot.send_message(
            chat_id=CHAT_ID,
            text=f"❌ Le bot a crashé avec l'erreur suivante :\n{e}\n\nTraceback:\n{err}"
        ))
    finally:
        loop.run_until_complete(bot.send_message(
            chat_id=CHAT_ID,
            text="⚠️ Le bot s’est arrêté."
        ))
