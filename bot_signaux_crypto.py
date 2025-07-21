import asyncio
import requests
import numpy as np
import json
from datetime import datetime
from telegram import Bot
from pymongo import MongoClient
import os
import nest_asyncio

# === CONFIGURATION ===
TELEGRAM_TOKEN = '7831038886:AAE1kESVsdtZyJ3AtZXIUy-rMTSlDBGlkac'
CHAT_ID = 969925512
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']
INTERVAL = '1h'
LIMIT = 100
SLEEP_SECONDS = 300  # 5 minutes

# === TELEGRAM ===
bot = Bot(token=TELEGRAM_TOKEN)

# === MONGODB ===
MONGO_URI = "mongodb+srv://morgysnipe:ZSJ3LI214eyEuyGW@cluster0.e1imbsb.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
client = MongoClient(MONGO_URI)
db = client["botcrypto"]
trades_col = db["trades"]
logs_col = db["logs"]

# === API BINANCE ===
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

# === TELEGRAM MESSAGE ===
async def send_message(text):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as e:
        print(f"Erreur Telegram : {e}")

# === TRAITEMENT D'UNE CRYPTO ===
async def process_symbol(symbol):
    try:
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        price = closes[-1]
        rsi = compute_rsi(closes)
        macd, signal = compute_macd(closes)

        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M')

        trade = trades_col.find_one({"symbol": symbol})
        buy = rsi < 30 and macd > signal
        sell = False

        if trade:
            entry = trade["entry"]
            gain_pct = ((price - entry) / entry) * 100
            if gain_pct >= 3 or gain_pct <= -1.5:
                sell = True

        if buy and not trade:
            trades_col.insert_one({"symbol": symbol, "entry": price, "time": now})
            await send_message(f"🟢 Achat détecté sur {symbol} à {price:.2f}")

        elif sell and trade:
            entry = trade["entry"]
            gain_pct = ((price - entry) / entry) * 100
            await send_message(
                f"🔴 Vente sur {symbol} à {price:.2f}\n"
                f"📈 Entrée: {entry:.2f}\n"
                f"📊 Résultat: {'+' if gain_pct >= 0 else ''}{gain_pct:.2f}%"
            )
            logs_col.insert_one({
                "symbol": symbol,
                "entry": entry,
                "exit": price,
                "gain_pct": gain_pct,
                "date": str(datetime.utcnow().date())
            })
            trades_col.delete_one({"symbol": symbol})

    except Exception as e:
        print(f"Erreur {symbol}: {e}")

# === RÉSUMÉ QUOTIDIEN ===
async def send_daily_summary():
    today = str(datetime.utcnow().date())
    gains_today = [log["gain_pct"] for log in logs_col.find({"date": today})]
    if not gains_today:
        return
    total = sum(gains_today)
    await send_message(
        f"📅 Résumé du {today} :\n"
        f"Trades : {len(gains_today)}\n"
        f"Gain net : {total:.2f}%"
    )

# === BOUCLE PRINCIPALE ===
async def main_loop():
    last_summary = None
    while True:
        await asyncio.gather(*(process_symbol(s) for s in SYMBOLS))
        now = datetime.utcnow().date()
        if last_summary != now:
            await send_daily_summary()
            last_summary = now
        await asyncio.sleep(SLEEP_SECONDS)

# === LANCEMENT ===
async def runner():
    await send_message("✅ Bot crypto lancé avec succès.")
    await main_loop()

if __name__ == "__main__":
    nest_asyncio.apply()
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(runner())
    except KeyboardInterrupt:
        print("Arrêt du bot.")


