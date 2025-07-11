import os
import time
import requests
import numpy as np
from telegram import Bot

# Récupération des variables d'environnement
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))

bot = Bot(token=TELEGRAM_TOKEN)

# Liste des cryptos à surveiller
CRYPTOS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

def get_prices(symbol, limit=50):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit={limit}"
    response = requests.get(url)
    data = response.json()
    closes = [float(candle[4]) for candle in data]
    return np.array(closes)

def moving_average(data, window):
    return np.convolve(data, np.ones(window)/window, mode='valid')

# On garde en mémoire les derniers signaux pour éviter les doublons
last_signals = {}

while True:
    for crypto in CRYPTOS:
        prices = get_prices(crypto)
        ma7 = moving_average(prices, 7)
        ma25 = moving_average(prices, 25)

        # On compare les dernières valeurs des moyennes mobiles
        if ma7[-1] > ma25[-1] and (last_signals.get(crypto) != "BUY"):
            bot.send_message(chat_id=CHAT_ID, text=f"📈 Signal ACHAT pour {crypto} (MA7 > MA25)")
            last_signals[crypto] = "BUY"
        elif ma7[-1] < ma25[-1] and (last_signals.get(crypto) != "SELL"):
            bot.send_message(chat_id=CHAT_ID, text=f"📉 Signal VENTE pour {crypto} (MA7 < MA25)")
            last_signals[crypto] = "SELL"
        else:
            print(f"{crypto} - Aucun nouveau signal ({last_signals.get(crypto)})")

    time.sleep(60*60)  # Pause 1 heure
