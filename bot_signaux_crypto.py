import asyncio
import requests
import numpy as np
from datetime import datetime, timezone
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from pymongo import MongoClient
import os
import nest_asyncio

nest_asyncio.apply()

# === CONFIGURATION ===
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID"))
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']
INTERVAL = '1h'
LIMIT = 100
SLEEP_SECONDS = 300  # 5 minutes

MONGO_URI = "mongodb+srv://morgysnipe:ZSJ3LI214eyEuyGW@cluster0.e1imbsb.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
client = MongoClient(MONGO_URI)
db = client['crypto_bot']
trades_col = db['trades']
logs_col = db['logs']

bot = Bot(token=TELEGRAM_TOKEN)

# === INDICATEURS TECHNIQUES ===
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

# === TRAITEMENT CRYPTOS ===
async def process_symbol(symbol):
    try:
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        price = closes[-1]
        rsi = compute_rsi(closes)
        macd, signal = compute_macd(closes)

        buy = rsi < 30 and macd > signal
        sell = False

        trade = trades_col.find_one({"symbol": symbol})
        if trade:
            entry = trade['entry']
            gain_pct = ((price - entry) / entry) * 100
            if gain_pct >= 3 or gain_pct <= -1.5:
                sell = True

        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')

        if buy and not trade:
            trades_col.insert_one({"symbol": symbol, "entry": price, "time": now})
            await bot.send_message(chat_id=CHAT_ID, text=f"🟢 Achat détecté sur {symbol} à {price:.2f}")

        elif sell and trade:
            entry = trade['entry']
            gain_pct = ((price - entry) / entry) * 100
            await bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"🔴 Vente sur {symbol} à {price:.2f}\n"
                    f"📈 Entrée: {entry:.2f}\n"
                    f"📊 Résultat: {'+' if gain_pct >= 0 else ''}{gain_pct:.2f}%"
                )
            )
            logs_col.insert_one({
                "symbol": symbol,
                "entry": entry,
                "exit": price,
                "gain_pct": gain_pct,
                "date": datetime.now(timezone.utc).date().isoformat()
            })
            trades_col.delete_one({"symbol": symbol})

    except Exception as e:
        print(f"Erreur {symbol}: {e}")

# === RÉSUMÉ JOURNALIER ===
async def send_daily_summary():
    today = str(datetime.now(timezone.utc).date())
    logs = list(logs_col.find({"date": today}))
    if logs:
        total = sum(log['gain_pct'] for log in logs)
        await bot.send_message(
            chat_id=CHAT_ID,
            text=(
                f"📅 Résumé du {today} :\n"
                f"Trades : {len(logs)}\n"
                f"Gain net : {total:.2f}%"
            )
        )

# === COMMANDE /stats ===
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = str(datetime.now(timezone.utc).date())
    logs = list(logs_col.find({"date": today}))
    if logs:
        total = sum(log['gain_pct'] for log in logs)
        msg = (
            f"📅 Résumé du {today} :\n"
            f"Trades : {len(logs)}\n"
            f"Gain net : {total:.2f}%"
        )
    else:
        msg = f"📅 Aucun trade enregistré aujourd’hui ({today})"
    await update.message.reply_text(msg)

# === LANCEMENT AVEC WEBHOOK ===
if __name__ == "__main__":
    async def main():
        print("🚀 Lancement du bot Telegram avec Webhook")
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("stats", stats))

        asyncio.create_task(bot.send_message(chat_id=CHAT_ID, text="✅ Bot activé et en attente de signaux..."))
        asyncio.create_task(main_loop())

        base_url = os.environ.get("RENDER_EXTERNAL_URL")
        if not base_url:
            raise Exception("⚠️ Variable RENDER_EXTERNAL_URL manquante dans Render (vérifie son nom exact et sa présence)")

        await app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 10000)),
            webhook_url=f"{base_url}/telegram"
        )

    asyncio.run(main())


