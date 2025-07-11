import requests
import time
import numpy as np
from telegram import Bot

TELEGRAM_TOKEN = '7831038886:AAE1kESVsdtZyJ3AtZXIUy-rMTSlDBGlkac'
CHAT_ID = 969925512

bot = Bot(token=TELEGRAM_TOKEN)

CRYPTO = "BTCUSDT"
INTERVAL = "1h"
LIMIT = 30

def get_klines():
    url = f"https://api.binance.com/api/v3/klines?symbol={CRYPTO}&interval={INTERVAL}&limit={LIMIT}"
    response = requests.get(url).json()
    closes = [float(candle[4]) for candle in response]  # prix de clôture
    return closes

def moving_average(data, period):
    return np.convolve(data, np.ones(period)/period, mode='valid')

def send_crypto_alert(last_signal):
    closes = get_klines()
    if len(closes) < 25:
        print("Pas assez de données")
        return last_signal

    ma_short = moving_average(closes, 7)
    ma_long = moving_average(closes, 25)

    if ma_short[-1] > ma_long[-1] and last_signal != "BUY":
        bot.send_message(chat_id=CHAT_ID, text=f"📈 Signal ACHAT pour {CRYPTO} (MA7 > MA25)")
        last_signal = "BUY"
    elif ma_short[-1] < ma_long[-1] and last_signal != "SELL":
        bot.send_message(chat_id=CHAT_ID, text=f"📉 Signal VENTE pour {CRYPTO} (MA7 < MA25)")
        last_signal = "SELL"
    else:
        print(f"MA7: {ma_short[-1]:.2f}, MA25: {ma_long[-1]:.2f}, dernier signal: {last_signal}")

    return last_signal

def main():
    last_signal = None
    while True:
        last_signal = send_crypto_alert(last_signal)
        time.sleep(300)  # 5 minutes

if __name__ == "__main__":
    main()

