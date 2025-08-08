import pandas as pd
import requests
from datetime import datetime
import numpy as np

# === PARAMÈTRES DU BACKTEST ===
SYMBOL = "SOLUSDT"
INTERVAL = "1h"
LIMIT = 500  # Plus = plus de données testées
INITIAL_CAPITAL = 1000
TRADES = []

# === CHARGEMENT HISTORIQUE BINANCE ===
def get_klines(symbol, interval='1h', limit=500):
    url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    response = requests.get(url)
    data = response.json()
    return pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

# === INDICATEURS TECHNIQUES ===
def compute_rsi(prices, period=14):
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

# === STRATÉGIE DE TRADING SIMPLIFIÉE ===
def apply_strategy(df):
    position = None
    entry_price = 0
    capital = INITIAL_CAPITAL
    for i in range(20, len(df)):
        price = df["close"].iloc[i]
        rsi = df["rsi"].iloc[i]
        if position is None and 50 < rsi < 65:
            # Signal d'achat
            position = capital / price
            entry_price = price
            TRADES.append(("BUY", df.index[i], price))
        elif position is not None and rsi > 70:
            # Signal de vente
            capital = position * price
            TRADES.append(("SELL", df.index[i], price))
            position = None
    return capital

# === MAIN ===
def backtest():
    df = get_klines(SYMBOL, INTERVAL, LIMIT)
    df["close"] = df["close"].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit='ms')
    df.set_index("open_time", inplace=True)
    df["rsi"] = compute_rsi(df["close"])

    final = apply_strategy(df)
    print(f"\n💰 Capital final : {final:.2f} USDT")
    print(f"📈 Nombre de trades : {len(TRADES)//2}")
    for t in TRADES:
        print(t)

if __name__ == "__main__":
    backtest()
