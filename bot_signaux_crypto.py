import os
import requests
import numpy as np
import asyncio
from telegram import Bot
import time

# Récupérer les variables d'environnement
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))

bot = Bot(token=TOKEN)

# Fonction d'envoi de message Telegram
async def send_signal(message: str):
    await bot.send_message(chat_id=CHAT_ID, text=message)

# Fonction pour récupérer les données de prix d'une crypto
def get_price_history(crypto):
    url = f"https://api.binance.com/api/v3/klines?symbol={crypto}USDT&interval=1h&limit=30"
    try:
        response = requests.get(url)
        data = response.json()
        closes = [float(candle[4]) for candle in data]
        return closes
    except Exception as e:
        print(f"Erreur récupération prix pour {crypto} : {e}")
        return []

# Dictionnaire pour garder en mémoire le prix d'achat par crypto
last_buy_price = {}

# Fonction de détection des signaux avec calcul de gain/perte
def detect_signal(crypto):
    prices = get_price_history(crypto)
    if len(prices) < 25:
        return

    ma7 = np.mean(prices[-7:])
    ma25 = np.mean(prices[-25:])
    current_price = prices[-1]

    # ACHAT : on mémorise le prix d'achat
    if ma7 > ma25:
        # Si pas déjà en position achat, on envoie le signal
        if crypto not in last_buy_price:
            message = f"📈 Signal ACHAT pour {crypto} (MA7 > MA25) - Prix: {current_price:.2f} USDT"
            asyncio.run(send_signal(message))
            last_buy_price[crypto] = current_price

    # VENTE : calcul du % gain/perte si on a un prix d'achat mémorisé
    elif ma7 < ma25:
        if crypto in last_buy_price:
            buy_price = last_buy_price[crypto]
            pct_gain = ((current_price - buy_price) / buy_price) * 100
            message = (f"📉 Signal VENTE pour {crypto} (MA7 < MA25) - Prix: {current_price:.2f} USDT\n"
                       f"Résultat: {pct_gain:+.2f}% par rapport au prix d'achat à {buy_price:.2f} USDT")
            asyncio.run(send_signal(message))
            # On supprime le prix d'achat, on est plus en position
            del last_buy_price[crypto]

# Liste des cryptos à surveiller
cryptos = ["BTC", "ETH", "BNB", "SOL", "XRP"]

# Boucle principale
if __name__ == "__main__":
    while True:
        for crypto in cryptos:
            detect_signal(crypto)
        time.sleep(300)  # pause 5 minutes

