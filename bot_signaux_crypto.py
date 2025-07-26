import asyncio
import requests
import numpy as np
from datetime import datetime, timezone
from telegram import Bot
import nest_asyncio
import traceback

nest_asyncio.apply()

# === CONFIGURATION ===
TELEGRAM_TOKEN = '7831038886:AAE1kESVsdtZyJ3AtZXIUy-rMTSlDBGlkac'
CHAT_ID = 969925512
SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
    'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'MATICUSDT', 'DOTUSDT',
    'ARBUSDT', 'OPUSDT', 'LTCUSDT', 'LINKUSDT', 'INJUSDT',
    'WLDUSDT', 'RUNEUSDT', 'APTUSDT', 'SEIUSDT', 'SUIUSDT',
    'TIAUSDT', 'PYTHUSDT', 'FETUSDT', 'RNDRUSDT', 'GALAUSDT'
]
INTERVAL = '1h'
LIMIT = 100
SLEEP_SECONDS = 300
MAX_TRADES = 7
bot = Bot(token=TELEGRAM_TOKEN)

trades = {}
history = []

def safe_message(text):
    return text if len(text) < 4000 else text[:3900] + "\n... (tronqué)"

def get_klines(symbol, interval='1h', limit=100):
    url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
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

def is_uptrend(prices, period=50):
    ma = np.mean(prices[-period:])
    return prices[-1] > ma

def is_volume_increasing(klines):
    volumes = [float(k[5]) for k in klines]
    return np.mean(volumes[-5:]) > np.mean(volumes[-10:-5])

def is_market_bullish():
    try:
        btc_prices = [float(k[4]) for k in get_klines('BTCUSDT')]
        eth_prices = [float(k[4]) for k in get_klines('ETHUSDT')]
        return is_uptrend(btc_prices) and is_uptrend(eth_prices)
    except:
        return False

async def process_symbol(symbol):
    try:
        if len(trades) >= MAX_TRADES:
            return

        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 Analyse de {symbol}", flush=True)
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        price = closes[-1]
        rsi = compute_rsi(closes)
        macd, signal = compute_macd(closes)

        klines_15m = get_klines(symbol, interval='15m', limit=50)
        closes_15m = [float(k[4]) for k in klines_15m]
        rsi_15m = compute_rsi(closes_15m)
        macd_15m, signal_15m = compute_macd(closes_15m)

        buy = False
        confidence = None
        label = ""
        position_pct = 0

        if not is_market_bullish():
            return

        # === CONDITIONS D'ACHAT ===
        if (rsi > 30 and compute_rsi(closes[:-1]) < 30 and macd > signal and is_uptrend(closes)) and (rsi_15m > 50 and macd_15m > signal_15m):
            buy = True
            confidence = 9
            label = "💎 RSI rebond + MACD + Uptrend (1h & 15m confirmés)"
            position_pct = 7

        elif rsi < 25 and macd > signal and is_uptrend(closes) and rsi_15m > 50:
            buy = True
            confidence = 8
            label = "🔥 RSI < 25 + MACD positif + confirm 15m"
            position_pct = 5

        elif 45 < rsi < 55 and macd > signal and is_uptrend(closes) and rsi_15m > 50:
            buy = True
            confidence = 7
            label = "📊 RSI neutre + MACD + Uptrend + 15m OK"
            position_pct = 5

        elif rsi > 70 and rsi_15m < 70 and macd > signal and is_uptrend(closes):
            buy = True
            confidence = 6
            label = "⚠️ RSI >70 → confirm 15m (retour sous 70)"
            position_pct = 3

        elif is_volume_increasing(klines) and macd > signal and is_uptrend(closes):
            buy = True
            confidence = 6
            label = "📈 Volume en hausse + MACD positif + Uptrend"
            position_pct = 4

        # === CONDITIONS DE SORTIE ===
        sell = False

        if symbol in trades:
            entry = trades[symbol]['entry']
            gain_pct = ((price - entry) / entry) * 100
            print(f"{symbol} | Position ouverte à {entry:.2f} | PnL: {gain_pct:.2f}%", flush=True)

            # TP partiel à +2%
            if gain_pct >= 2 and not trades[symbol].get('partial', False):
                trades[symbol]['partial'] = True
                await bot.send_message(chat_id=CHAT_ID, text=safe_message(f"🔵 Prise de bénéfices partiels sur {symbol} à {price:.2f} (+2%)"))

            # TP total ou SL
            if gain_pct >= 3 or gain_pct <= -1.5:
                sell = True

        # === ENTRÉE TRADE ===
        if buy and symbol not in trades:
            trades[symbol] = {
                "entry": price,
                "time": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'),
                "confidence": confidence,
                "partial": False
            }
            await bot.send_message(chat_id=CHAT_ID, text=safe_message(f"🟢 Achat {symbol} à {price:.2f}\n{label}\n💰 Suggéré : {position_pct}% du capital"))

        # === SORTIE TRADE ===
        elif sell and symbol in trades:
            entry = trades[symbol]['entry']
            gain_pct = ((price - entry) / entry) * 100
            confidence = trades[symbol].get("confidence", "?")
            emoji = "💎" if confidence >= 8 else "⚠️"
            history.append({"symbol": symbol, "entry": entry, "exit": price, "result": gain_pct, "confidence": confidence})
            await bot.send_message(chat_id=CHAT_ID, text=safe_message(f"🔴 Vente {symbol} à {price:.2f}\n📈 Entrée: {entry:.2f}\n📊 Gain: {'+' if gain_pct >= 0 else ''}{gain_pct:.2f}%\n{emoji} Fiabilité: {confidence}/10"))
            del trades[symbol]

    except Exception as e:
        print(f"❌ Erreur {symbol}: {e}", flush=True)
        traceback.print_exc()

# === DAILY SUMMARY ===
async def send_daily_summary():
    if not history:
        return
    lines = ["🌟 Récapitulatif des trades (24h) :"]
    for h in history[-50:]:
        emoji = "📈" if h["result"] > 0 else "📉"
        lines.append(f"{emoji} {h['symbol']} | Entrée: {h['entry']:.2f} | Sortie: {h['exit']:.2f} | Gain: {h['result']:.2f}%")
    await bot.send_message(chat_id=CHAT_ID, text=safe_message("\n".join(lines)))

# === MAIN LOOP ===
async def main_loop():
    await bot.send_message(chat_id=CHAT_ID, text=safe_message(f"🚀 Bot démarré à {datetime.now().strftime('%H:%M:%S')}"))
    last_heartbeat_hour = None
    last_daily_summary_sent = False
    while True:
        try:
            now = datetime.now()
            print(f"🔁 Nouvelle itération à {now.strftime('%H:%M:%S')}", flush=True)

            if last_heartbeat_hour != now.hour:
                last_heartbeat_hour = now.hour
                await bot.send_message(chat_id=CHAT_ID, text=f"✅ Bot actif à {now.strftime('%H:%M')} (heartbeat)")

            if now.hour == 10 and not last_daily_summary_sent:
                await send_daily_summary()
                last_daily_summary_sent = True
            elif now.hour != 10:
                last_daily_summary_sent = False

            await asyncio.gather(*(process_symbol(sym) for sym in SYMBOLS))
            print("✔️ Itération terminée", flush=True)

        except Exception as e:
            err = traceback.format_exc()
            print(f"⚠️ Erreur dans main_loop : {e}", flush=True)
            await bot.send_message(chat_id=CHAT_ID, text=safe_message(f"⚠️ Erreur :\n{err}"))

        await asyncio.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main_loop())
    except Exception as e:
        err = traceback.format_exc()
        print(f"❌ Crash fatal : {e}", flush=True)
        loop.run_until_complete(bot.send_message(chat_id=CHAT_ID, text=safe_message(f"❌ Le bot a crashé :\n{err}")))
    finally:
        loop.run_until_complete(bot.send_message(chat_id=CHAT_ID, text="⚠️ Le bot s’est arrêté."))

