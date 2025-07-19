import os
import requests
import numpy as np
import asyncio
from telegram import Bot

# Récupérer les variables d'environnement
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))

bot = Bot(token=TOKEN)

async def send_signal(message):
    await bot.send_message(chat_id=CHAT_ID, text=message)

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

async def detect_signal(crypto):
    prices = get_price_history(crypto)
    if len(prices) < 25:
        return
    ma7 = np.mean(prices[-7:])
    ma25 = np.mean(prices[-25:])
    if ma7 > ma25:
        await send_signal(f"📈 Signal ACHAT pour {crypto} (MA7 > MA25)")
    elif ma7 < ma25:
        await send_signal(f"📉 Signal VENTE pour {crypto} (MA7 < MA25)")

cryptos = ["BTC", "ETH", "BNB", "SOL", "XRP"]

async def main():
    while True:
        for crypto in cryptos:
            await detect_signal(crypto)
        await asyncio.sleep(300)  # pause 5 minutes

if __name__ == "__main__":
    asyncio.run(main())

