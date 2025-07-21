import asyncio
import requests
import numpy as np
import json
from datetime import datetime, timezone
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from pymongo import MongoClient
import os

# === CONFIGURATION ===
TELEGRAM_TOKEN = '7831038886:AAE1kESVsdtZyJ3AtZXIUy-rMTSlDBGlkac'
CHAT_ID = 969925512
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']
INTERVAL = '1h'
LIMIT = 100
SLEEP_SECONDS = 300  # 5 minutes
MONGO_URI = 'mongodb+srv://morgysnipe:ZSJ3LI214eyEuyGW@cluster0.e1imbsb.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0'

# === INITIALISATION ===
bot = Bot(token=TELEGRAM_TOKEN)
client = MongoClient(MONGO_URI)
db = client['crypto_bot']
trades_col = db['trades']
logs_col = db['logs']

# === ENVOI DE MESSAGES ===
async def send_message(text):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as e:
        print(f"Erreur Telegram: {e}")

# === API BINANCE ===
def get_klines(symbol):
    url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={INTERVAL}&limit={LIMIT}'
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def calculate_signals(prices):
    ma7 = np.mean(prices[-7:])
    ma25 = np.mean(prices[-25:])
    last = prices[-1]
    return ma7 > ma25, ma7 < ma25, last

# === STRATEGIE PAR CRYPTO ===
async def process_symbol(symbol):
    try:
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        buy_signal, sell_signal, price = calculate_signals(closes)
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')

        trade = trades_col.find_one({"symbol": symbol})

        if buy_signal and not trade:
            trades_col.insert_one({"symbol": symbol, "entry": price, "time": now})
            await send_message(f"🟢 Achat détecté sur {symbol} à {price:.2f}")

        elif sell_signal and trade:
            entry = trade['entry']
            gain_pct = ((price - entry) / entry) * 100
            message = (
                f"🔴 Vente détectée sur {symbol} à {price:.2f}\n"
                f"📊 Entrée: {entry:.2f}\n"
                f"📈 Gain/Pertes: {gain_pct:.2f}%"
            )
            await send_message(message)
            trades_col.delete_one({"symbol": symbol})
            logs_col.insert_one({"symbol": symbol, "entry": entry, "exit": price, "gain_pct": gain_pct, "date": now})

    except Exception as e:
        print(f"Erreur {symbol} : {e}")

# === RESUME /stats ===
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    logs = list(logs_col.find({"date": {"$regex": today}}))

    if not logs:
        await update.message.reply_text("Aucun trade effectué aujourd'hui.")
        return

    total_gain = sum(log['gain_pct'] for log in logs)
    message = f"📅 Résumé du {today} :\nTrades: {len(logs)}\nGain net : {total_gain:.2f}%"
    await update.message.reply_text(message)

# === BOUCLE PRINCIPALE ===
async def trading_loop():
    while True:
        tasks = [process_symbol(sym) for sym in SYMBOLS]
        await asyncio.gather(*tasks)
        await asyncio.sleep(SLEEP_SECONDS)

# === LANCEMENT ===
if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("stats", stats_command))

    async def runner():
        await send_message("✅ Bot crypto lancé avec succès.")
        asyncio.create_task(trading_loop())
        await app.run_polling()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Bot arrêté.")
