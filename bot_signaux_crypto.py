import asyncio
import requests
import numpy as np
import json
from datetime import datetime
from telegram import Bot
import os

# === CONFIGURATION ===
TELEGRAM_TOKEN = '7831038886:AAE1kESVsdtZyJ3AtZXIUy-rMTSlDBGlkac'
CHAT_ID = 969925512
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']
INTERVAL = '1h'
LIMIT = 100
SLEEP_SECONDS = 300  # 5 minutes
STOP_LOSS = -1.5  # %

# === INITIALISATION ===
bot = Bot(token=TELEGRAM_TOKEN)
TRADES_FILE = 'trades.json'

def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_trades(trades):
    with open(TRADES_FILE, 'w') as f:
        json.dump(trades, f)

async def send_message(text):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as e:
        print(f"Erreur Telegram: {e}")

def get_klines(symbol):
    url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={INTERVAL}&limit={LIMIT}'
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def calculate_rsi(prices, period=14):
    deltas = np.diff(prices)
    seed = deltas[:period]
    up = seed[seed > 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / down if down != 0 else 0
    rsi = np.zeros_like(prices)
    rsi[:period] = 100. - 100. / (1. + rs)

    for i in range(period, len(prices)):
        delta = deltas[i - 1]
        gain = max(delta, 0)
        loss = -min(delta, 0)
        up = (up * (period - 1) + gain) / period
        down = (down * (period - 1) + loss) / period
        rs = up / down if down != 0 else 0
        rsi[i] = 100. - 100. / (1. + rs)

    return rsi

async def process_symbol(symbol, trades):
    try:
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        current_price = closes[-1]
        rsi = calculate_rsi(np.array(closes))[-1]

        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M')

        if symbol not in trades:
            # Signal achat : RSI < 30 et tendance haussière
            if rsi < 30 and closes[-1] > closes[-2]:
                trades[symbol] = {"entry": current_price, "time": now}
                await send_message(f"🟢 Achat détecté sur {symbol} à {current_price:.2f} (RSI: {rsi:.2f})")
        else:
            entry = trades[symbol]["entry"]
            gain_pct = ((current_price - entry) / entry) * 100

            # Vente si RSI > 70 ou Stop Loss atteint
            if rsi > 70 or gain_pct <= STOP_LOSS:
                msg = (
                    f"🔴 Vente détectée sur {symbol} à {current_price:.2f}\n"
                    f"📊 Entrée: {entry:.2f}\n"
                    f"📈 Gain/Pertes: {gain_pct:.2f}% (RSI: {rsi:.2f})"
                )
                await send_message(msg)
                log_trade(symbol, entry, current_price, gain_pct)
                del trades[symbol]

    except Exception as e:
        print(f"Erreur {symbol} : {e}")

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

async def send_daily_summary():
    if not os.path.exists("logs.json"):
        return

    with open("logs.json", "r") as f:
        logs = json.load(f)

    today = str(datetime.utcnow().date())
    gains_today = [log['gain_pct'] for log in logs if log['date'] == today]

    if gains_today:
        total = sum(gains_today)
        await send_message(
            f"📅 Résumé du {today} :\n"
            f"Trades: {len(gains_today)}\n"
            f"Gain net : {total:.2f}%"
        )

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

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("Arrêt du bot.")

