import asyncio
import requests
import numpy as np
import json
from datetime import datetime
from telegram import Bot
from pymongo import MongoClient
import os

# === CONFIGURATION ===
TELEGRAM_TOKEN = '7831038886:AAE1kESVsdtZyJ3AtZXIUy-rMTSlDBGlkac'
CHAT_ID = 969925512
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']
INTERVAL = '1h'
LIMIT = 100
SLEEP_SECONDS = 300  # 5 minutes

# MongoDB
MONGO_URI = "mongodb+srv://morgysnipe:ZSJ3LI214eyEuyGW@cluster0.e1imbsb.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "cryptoBot"
COLLECTION_NAME = "trade_logs"

bot = Bot(token=TELEGRAM_TOKEN)
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
collection = db[COLLECTION_NAME]

TRADES_FILE = 'trades.json'

# === GESTION DES TRADES ===
def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_trades(trades):
    with open(TRADES_FILE, 'w') as f:
        json.dump(trades, f)

def log_trade(symbol, entry, exit_price, gain_pct):
    log = {
        "symbol": symbol,
        "entry": entry,
        "exit": exit_price,
        "gain_pct": gain_pct,
        "date": str(datetime.utcnow().date())
    }
    collection.insert_one(log)

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

# === ENVOI MESSAGE TELEGRAM ===
async def send_message(text):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as e:
        print(f"Erreur Telegram : {e}")

# === TRAITEMENT D'UNE CRYPTO ===
async def process_symbol(symbol, trades):
    try:
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        price = closes[-1]
        rsi = compute_rsi(closes)
        macd, signal = compute_macd(closes)

        buy = rsi < 30 and macd > signal
        sell = False

        if symbol in trades:
            entry = trades[symbol]['entry']
            gain_pct = ((price - entry) / entry) * 100
            if gain_pct >= 3 or gain_pct <= -1.5:
                sell = True

        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M')

        if buy and symbol not in trades:
            trades[symbol] = {"entry": price, "time": now}
            await send_message(f"🟢 Achat détecté sur {symbol} à {price:.2f}")

        elif sell and symbol in trades:
            entry = trades[symbol]['entry']
            gain_pct = ((price - entry) / entry) * 100
            await send_message(
                f"🔴 Vente sur {symbol} à {price:.2f}\n"
                f"📈 Entrée: {entry:.2f}\n"
                f"📊 Résultat: {'+' if gain_pct >= 0 else ''}{gain_pct:.2f}%"
            )
            log_trade(symbol, entry, price, gain_pct)
            del trades[symbol]

    except Exception as e:
        print(f"Erreur {symbol}: {e}")

# === BOUCLE PRINCIPALE ===
async def main_loop():
    trades = load_trades()
    while True:
        tasks = [process_symbol(sym, trades) for sym in SYMBOLS]
        await asyncio.gather(*tasks)
        save_trades(trades)
        await asyncio.sleep(SLEEP_SECONDS)

# === LANCEMENT ===
if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("Arrêt du bot.")

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Bot arrêté.")
