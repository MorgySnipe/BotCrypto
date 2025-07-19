import asyncio
import requests
import numpy as np
import json
from datetime import datetime, timedelta
from telegram import Bot
import os

# === CONFIGURATION ===
TELEGRAM_TOKEN = '7831038886:AAE1kESVsdtZyJ3AtZXIUy-rMTSlDBGlkac'
CHAT_ID = 969925512
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']
INTERVAL = '1h'
LIMIT = 100
SLEEP_SECONDS = 300  # 5 minutes

# === INITIALISATION ===
bot = Bot(token=TELEGRAM_TOKEN)
TRADES_FILE = 'trades.json'

# === GESTION DES TRADES EN LOCAL ===
def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_trades(trades):
    with open(TRADES_FILE, 'w') as f:
        json.dump(trades, f)

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

# === STRATÉGIE PAR CRYPTO ===
async def process_symbol(symbol, trades):
    try:
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        buy_signal, sell_signal, price = calculate_signals(closes)

        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M')

        if buy_signal and symbol not in trades:
            trades[symbol] = {"entry": price, "time": now}
            await send_message(f"🟢 Achat détecté sur {symbol} à {price:.2f}")

        elif sell_signal and symbol in trades:
            entry = trades[symbol]['entry']
            gain_pct = ((price - entry) / entry) * 100
            message = (
                f"🔴 Vente détectée sur {symbol} à {price:.2f}\n"
                f"📊 Entrée: {entry:.2f}\n"
                f"📈 Gain/Pertes: {gain_pct:.2f}%"
            )
            await send_message(message)
            del trades[symbol]

    except Exception as e:
        print(f"Erreur {symbol} : {e}")

# === RÉSUMÉ QUOTIDIEN ===
async def send_daily_summary():
    if not os.path.exists("logs.json"):
        return

    with open("logs.json", "r") as f:
        logs = json.load(f)

    today = datetime.utcnow().date()
    gains_today = [log['gain_pct'] for log in logs if log['date'] == str(today)]
    
    if not gains_today:
        return

    total = sum(gains_today)
    await send_message(
        f"📅 Résumé du {today} :\n"
        f"Trades: {len(gains_today)}\n"
        f"Gain net : {total:.2f}%"
    )

# === SAUVEGARDE DES LOGS ===
def log_trade(symbol, entry, exit_price, gain_pct):
    log = {
        "symbol": symbol,
        "entry": entry,
        "exit": exit_price,
        "gain_pct": gain_pct,
        "date": str(datetime.utcnow().date())
    }

    logs = []
    if os.path.exists("logs.json"):
        with open("logs.json", "r") as f:
            logs = json.load(f)
    logs.append(log)
    with open("logs.json", "w") as f:
        json.dump(logs, f)

# === BOUCLE PRINCIPALE ===
async def main_loop():
    trades = load_trades()
    last_summary_sent = None

    while True:
        tasks = [process_symbol(sym, trades) for sym in SYMBOLS]
        await asyncio.gather(*tasks)

        save_trades(trades)

        now = datetime.utcnow()
        if (last_summary_sent is None) or (now.date() > last_summary_sent):
            await send_daily_summary()
            last_summary_sent = now.date()

        await asyncio.sleep(SLEEP_SECONDS)

# === LANCEMENT ===
if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("Arrêt du bot.")

