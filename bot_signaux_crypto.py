import asyncio
import requests
import numpy as np
from datetime import datetime, timezone
from telegram import Bot
from pymongo import MongoClient
import nest_asyncio
import traceback
import sys

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
sys_logs_col = db['system_logs']

bot = Bot(token=TELEGRAM_TOKEN)

def log_system(event, level="INFO", details=None):
    sys_logs_col.insert_one({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "level": level,
        "details": details
    })

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

async def process_symbol(symbol):
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 Analyse de {symbol}")
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        price = closes[-1]
        rsi = compute_rsi(closes)
        macd, signal = compute_macd(closes)

        log_system("analyse_symbol", "DEBUG", {
            "symbol": symbol,
            "price": price,
            "RSI": rsi,
            "MACD": macd,
            "Signal": signal
        })

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
            log_system("buy_signal", "INFO", {"symbol": symbol, "price": price})

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
            log_system("sell_signal", "INFO", {
                "symbol": symbol,
                "entry": entry,
                "exit": price,
                "gain_pct": gain_pct
            })

    except Exception as e:
        log_system("error_process_symbol", "ERROR", {
            "symbol": symbol,
            "error": str(e),
            "trace": traceback.format_exc()
        })
        print(f"❌ Erreur {symbol}: {e}")

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
        log_system("daily_summary", "INFO", {"date": today, "total_gain": total, "trades": len(logs)})
    else:
        log_system("daily_summary", "INFO", {"date": today, "message": "Aucun trade enregistré"})

async def main_loop():
    start_time = datetime.now().strftime('%H:%M:%S')
    await bot.send_message(chat_id=CHAT_ID, text=f"🚀 Bot démarré à {start_time} (heure serveur)")
    log_system("startup", "INFO", {"start_time": start_time})

    last_summary_sent = None
    while True:
        try:
            now = datetime.now()
            if now.minute == 0:  # Log de vie toutes les heures
                print(f"🟢 Bot actif à {now.strftime('%H:%M:%S')}")
                log_system("heartbeat", "INFO", {"time": now.isoformat()})

            log_system("iteration_start", "DEBUG", {"time": now.isoformat()})
            await asyncio.gather(*(process_symbol(sym) for sym in SYMBOLS))
            if last_summary_sent != now.date():
                await send_daily_summary()
                last_summary_sent = now.date()
            log_system("iteration_complete", "DEBUG", {"status": "OK"})
        except Exception as loop_error:
            err_trace = traceback.format_exc()
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"⚠️ Erreur dans main_loop : {loop_error}\n\n{err_trace}"
            )
            log_system("main_loop_error", "CRITICAL", {"error": str(loop_error), "trace": err_trace})
        await asyncio.sleep(SLEEP_SECONDS)

# === EXECUTION ===
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main_loop())
    except Exception as e:
        err = traceback.format_exc()
        log_system("fatal_crash", "CRITICAL", {"error": str(e), "trace": err})
        loop.run_until_complete(bot.send_message(
            chat_id=CHAT_ID,
            text=f"❌ Le bot a crashé avec l'erreur suivante :\n{e}\n\nTraceback:\n{err}"
        ))
    finally:
        log_system("shutdown", "WARNING", {"reason": "Fin du programme"})
        loop.run_until_complete(bot.send_message(
            chat_id=CHAT_ID,
            text="⚠️ Le bot s’est arrêté."
        ))


