import asyncio
import requests
import numpy as np
from datetime import datetime, timezone
from telegram import Bot
from pymongo import MongoClient
import nest_asyncio

nest_asyncio.apply()

# === CONFIGURATION ===
TELEGRAM_TOKEN = '7831038886:AAE1kESVsdtZyJ3AtZXIUy-rMTSlDBGlkac'
CHAT_ID = 969925512
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
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 Analyse de {symbol}")
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        price = closes[-1]
        rsi = compute_rsi(closes)
        macd, signal = compute_macd(closes)

        print(f"{symbol} | Price: {price:.2f} | RSI: {rsi:.2f} | MACD: {macd:.4f} | Signal: {signal:.4f}")

        buy = rsi < 30 and macd > signal
        sell = False

        trade = trades_col.find_one({"symbol": symbol})
        if trade:
            entry = trade['entry']
            gain_pct = ((price - entry) / entry) * 100
            print(f"{symbol} | Position ouverte à {entry:.2f} | PnL: {gain_pct:.2f}%")
            if gain_pct >= 3 or gain_pct <= -1.5:
                sell = True

        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')

        if buy and not trade:
            print(f"✅ Signal d'achat détecté sur {symbol} à {price:.2f}")
            trades_col.insert_one({"symbol": symbol, "entry": price, "time": now})
            await bot.send_message(chat_id=CHAT_ID, text=f"🟢 Achat détecté sur {symbol} à {price:.2f}")

        elif sell and trade:
            print(f"🔴 Vente déclenchée sur {symbol} à {price:.2f}")
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
        print(f"❌ Erreur {symbol}: {e}")

# === RÉSUMÉ JOURNALIER ===
async def send_daily_summary():
    today = str(datetime.now(timezone.utc).date())
    logs = list(logs_col.find({"date": today}))
    if logs:
        total = sum(log['gain_pct'] for log in logs)
        print(f"📊 Résumé du {today} | Trades: {len(logs)} | Gain net: {total:.2f}%")
        await bot.send_message(
            chat_id=CHAT_ID,
            text=(
                f"📅 Résumé du {today} :\n"
                f"Trades : {len(logs)}\n"
                f"Gain net : {total:.2f}%"
            )
        )
    else:
        print(f"📊 Aucun trade enregistré aujourd’hui ({today})")

# === BOUCLE PRINCIPALE ===
async def main_loop():
    print("🚀 Boucle principale démarrée.")
    last_summary_sent = None
    while True:
        print(f"🔁 Nouvelle itération à {datetime.now().strftime('%H:%M:%S')}")
        await asyncio.gather(*(process_symbol(sym) for sym in SYMBOLS))
        now = datetime.now(timezone.utc).date()
        if last_summary_sent != now:
            await send_daily_summary()
            last_summary_sent = now
        await asyncio.sleep(SLEEP_SECONDS)

# === LANCEMENT FINAL POUR RENDER ===
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(main_loop())
    loop.run_until_complete(bot.send_message(chat_id=CHAT_ID, text="✅ Bot Crypto lancé (mode worker sans polling)"))
    loop.run_forever()
