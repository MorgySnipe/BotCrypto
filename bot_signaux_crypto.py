import asyncio
import requests
import numpy as np
import json
from datetime import datetime, timezone
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
import os
from pymongo import MongoClient

# === CONFIGURATION ===
TELEGRAM_TOKEN = '7831038886:AAE1kESVsdtZyJ3AtZXIUy-rMTSlDBGlkac'
CHAT_ID = 969925512
MONGO_URI = "mongodb+srv://morgysnipe:ZSJ3LI214eyEuyGW@cluster0.e1imbsb.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']
INTERVAL = '1h'
LIMIT = 100
SLEEP_SECONDS = 300  # 5 minutes

bot = Bot(token=TELEGRAM_TOKEN)
client = MongoClient(MONGO_URI)
db = client.crypto_bot
trades_col = db.trades
logs_col = db.logs

# === ANALYSE TECHNIQUE ===
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

# === TELEGRAM ===
async def send_message(text):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as e:
        print(f"Erreur Telegram : {e}")

# === TRAITEMENT PAR SYMBOL ===
async def process_symbol(symbol):
    try:
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        price = closes[-1]
        rsi = compute_rsi(closes)
        macd, signal = compute_macd(closes)
        buy = rsi < 30 and macd > signal
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')

        trade = trades_col.find_one({"symbol": symbol})

        if buy and not trade:
            trades_col.insert_one({"symbol": symbol, "entry": price, "time": now})
            await send_message(f"🟢 Achat détecté sur {symbol} à {price:.2f}")

        elif trade:
            entry = trade['entry']
            gain_pct = ((price - entry) / entry) * 100
            if gain_pct >= 3 or gain_pct <= -1.5:
                trades_col.delete_one({"symbol": symbol})
                logs_col.insert_one({"symbol": symbol, "entry": entry, "exit": price, "gain_pct": gain_pct, "date": str(datetime.utcnow().date())})
                await send_message(f"🔴 Vente sur {symbol} à {price:.2f}\n📈 Entrée: {entry:.2f}\n📊 Résultat: {'+' if gain_pct >= 0 else ''}{gain_pct:.2f}%")

    except Exception as e:
        print(f"Erreur {symbol} : {e}")

# === STATISTIQUES ===
async def send_daily_summary():
    today = str(datetime.utcnow().date())
    logs = list(logs_col.find({"date": today}))
    if logs:
        total = sum([log['gain_pct'] for log in logs])
        await send_message(f"📅 Résumé du {today} :\nTrades : {len(logs)}\nGain net : {total:.2f}%")

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = str(datetime.utcnow().date())
    logs = list(logs_col.find({"date": today}))
    if logs:
        total = sum([log['gain_pct'] for log in logs])
        await update.message.reply_text(f"📊 Stats du jour :\nTrades : {len(logs)}\nGain net : {total:.2f}%")
    else:
        await update.message.reply_text("Aucun trade aujourd'hui.")

# === MAIN LOOP ===
async def main_loop():
    while True:
        tasks = [process_symbol(sym) for sym in SYMBOLS]
        await asyncio.gather(*tasks)
        await asyncio.sleep(SLEEP_SECONDS)

# === LANCEMENT ===
async def runner():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("stats", show_stats))
    asyncio.create_task(main_loop())
    await app.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Bot arrêté.")

