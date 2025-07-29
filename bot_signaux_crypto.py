import asyncio
import requests
import numpy as np
from datetime import datetime, timezone, timedelta
from telegram import Bot
import nest_asyncio
import traceback

nest_asyncio.apply()

# === CONFIGURATION ===
TELEGRAM_TOKEN = '7831038886:AAE1kESVsdtZyJ3AtZXIUy-rMTSlDBGlkac'
CHAT_ID = 969925512
SYMBOLS = [
    'BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT',
    'ADAUSDT','DOGEUSDT','AVAXUSDT','MATICUSDT','DOTUSDT',
    'ARBUSDT','OPUSDT','LTCUSDT','LINKUSDT','INJUSDT',
    'WLDUSDT','RUNEUSDT','APTUSDT','SEIUSDT','SUIUSDT',
    'TIAUSDT','PYTHUSDT','FETUSDT','RNDRUSDT','GALAUSDT'
]
INTERVAL = '1h'
LIMIT = 100
SLEEP_SECONDS = 300
MAX_TRADES = 7
MIN_VOLUME = 1000000
COOLDOWN_HOURS = 4

bot = Bot(token=TELEGRAM_TOKEN)
trades = {}
history = []
last_trade_time = {}

# === UTILITAIRES ===
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

def compute_ema(prices, period=200):
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    ema = np.convolve(prices, weights, mode='full')[:len(prices)]
    return ema[-1]

def compute_atr(klines, period=14):
    highs = np.array([float(k[2]) for k in klines])
    lows = np.array([float(k[3]) for k in klines])
    closes = np.array([float(k[4]) for k in klines])
    tr = np.maximum(highs[1:], closes[:-1]) - np.minimum(lows[1:], closes[:-1])
    return np.mean(tr[-period:])

def detect_rsi_divergence(prices, rsis):
    return prices[-1] > prices[-2] and rsis[-1] < rsis[-2]

def is_uptrend(prices, period=50):
    return prices[-1] > np.mean(prices[-period:])

def is_volume_increasing(klines):
    volumes = [float(k[5]) for k in klines]
    return np.mean(volumes[-5:]) > np.mean(volumes[-10:-5]) and np.mean(volumes[-5:]) > MIN_VOLUME

def is_market_bullish():
    try:
        btc_prices = [float(k[4]) for k in get_klines('BTCUSDT')]
        eth_prices = [float(k[4]) for k in get_klines('ETHUSDT')]
        return is_uptrend(btc_prices) and is_uptrend(eth_prices)
    except:
        return False

def in_active_session():
    hour = datetime.now(timezone.utc).hour
    return not (0 <= hour < 6)  # Pas de trade entre 00h et 06h UTC

# === STRATEGIE ===
async def process_symbol(symbol):
    try:
        # === COOLDOWN ===
        if symbol in last_trade_time:
            cooldown_left = COOLDOWN_HOURS - (datetime.now() - last_trade_time[symbol]).total_seconds()/3600
            if cooldown_left > 0:
                print(f"{symbol} ⏳ Cooldown actif: {cooldown_left:.1f}h", flush=True)
                return

        if len(trades) >= MAX_TRADES:
            print(f"🚫 Trop de trades ouverts ({MAX_TRADES}), {symbol} ignoré", flush=True)
            return

        if not in_active_session():
            print(f"{symbol} 🛑 Hors session active (UTC 00-06)", flush=True)
            return

        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 Analyse de {symbol}", flush=True)

        # === RECUPERATION DONNEES ===
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        volumes = [float(k[5]) for k in klines]

        price = closes[-1]
        rsi = compute_rsi(closes)
        macd, signal = compute_macd(closes)
        ema200 = compute_ema(closes, 200)
        atr = compute_atr(klines)
        rsis = [compute_rsi(closes[i-14:i]) for i in range(14, len(closes))]

        # === CONFIRMATION 15m ===
        klines_15m = get_klines(symbol, interval='15m', limit=50)
        closes_15m = [float(k[4]) for k in klines_15m]
        rsi_15m = compute_rsi(closes_15m)
        macd_15m, signal_15m = compute_macd(closes_15m)

        # === FILTRES AVANCES ===
        if not is_market_bullish():
            print(f"{symbol} ❌ Marché global baissier", flush=True)
            return
        if price < ema200:
            print(f"{symbol} ❌ Sous EMA200 (trend long terme baissier)", flush=True)
            return
        if detect_rsi_divergence(closes, rsis):
            print(f"{symbol} ❌ Divergence RSI détectée", flush=True)
            return
        if (highs[-1] - lows[-1]) / lows[-1] > 0.05:
            print(f"{symbol} ❌ Bougie >5% range, achat bloqué", flush=True)
            return
        if price > min(lows[-5:]) * 1.03:
            print(f"{symbol} ❌ Prix > +3% du plus bas récent, anti-pump", flush=True)
            return
        if np.mean(volumes[-5:]) < 0.8 * np.mean(volumes[-20:]):
            print(f"{symbol} ❌ Volume trop faible (<80% moyenne)", flush=True)
            return

        # === SIGNAL ACHAT ===
        buy = False
        confidence = 0
        label = ""
        position_pct = 5
        if is_uptrend(closes) and macd > signal and rsi_15m > 50:
            buy = True
            confidence = 9
            label = "💎 Trend EMA200 + MACD + RSI confirmé"
            position_pct = 7

        # === POSITION OUVERTE ===
        sell = False
        if symbol in trades:
            entry = trades[symbol]['entry']
            gain = ((price - entry) / entry) * 100
            stop = trades[symbol].get("stop", entry - atr)
            if gain > 1.5: stop = max(stop, entry)
            if gain > 3: stop = max(stop, entry * 1.01)
            if gain > 5: stop = max(stop, entry * 1.03)
            trades[symbol]["stop"] = stop

            if gain >= 1.5 and not trades[symbol].get("tp1", False):
                trades[symbol]["tp1"] = True
                await bot.send_message(chat_id=CHAT_ID, text=f"🟢 TP1 +1.5% atteint sur {symbol} | Stop {stop:.4f}")
            if gain >= 3 and not trades[symbol].get("tp2", False):
                trades[symbol]["tp2"] = True
                await bot.send_message(chat_id=CHAT_ID, text=f"🟢 TP2 +3% atteint sur {symbol} | Stop {stop:.4f}")
            if gain >= 5:
                await bot.send_message(chat_id=CHAT_ID, text=f"🟢 TP3 +5% atteint sur {symbol} | Clôture finale")
                sell = True
            if trades[symbol].get("tp1", False) and gain < 1:
                sell = True
            if price < stop or gain <= -1.5:
                sell = True

        # === ENTREE ===
        if buy and symbol not in trades:
            trades[symbol] = {
                "entry": price,
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "confidence": confidence,
                "stop": price - atr,
                "position_pct": position_pct
            }
            last_trade_time[symbol] = datetime.now()
            await bot.send_message(chat_id=CHAT_ID, text=(
                f"🟢 Achat {symbol} à {price:.4f}\n"
                f"{label}\n"
                f"📊 RSI1h: {rsi:.2f} | RSI15m: {rsi_15m:.2f}\n"
                f"📈 MACD: {macd:.4f} / Signal: {signal:.4f}\n"
                f"📦 Volume: {np.mean(volumes[-5:]):.0f} vs {np.mean(volumes[-20:]):.0f}\n"
                f"📉 SL ATR: {price - atr:.4f}\n"
                f"💰 Capital conseillé : {position_pct}%"
            ))

        # === SORTIE ===
        elif sell and symbol in trades:
            entry = trades[symbol]['entry']
            gain = ((price - entry) / entry) * 100
            stop_used = trades[symbol].get("stop", entry - atr)
            tp_status = []
            if trades[symbol].get("tp1", False): tp_status.append("TP1 ✅")
            if trades[symbol].get("tp2", False): tp_status.append("TP2 ✅")
            if gain >= 5: tp_status.append("TP3 ✅")
            tp_info = " | ".join(tp_status) if tp_status else "Aucun TP atteint"
            await bot.send_message(chat_id=CHAT_ID, text=(
                f"🔴 Vente {symbol} à {price:.4f}\n"
                f"📈 Entrée: {entry:.4f}\n"
                f"📊 Gain: {gain:.2f}%\n"
                f"🛑 Stop final: {stop_used:.4f}\n"
                f"🎯 {tp_info}"
            ))
            del trades[symbol]

    except Exception as e:
        print(f"❌ Erreur {symbol}: {e}", flush=True)
        traceback.print_exc()

# === DAILY SUMMARY ===
async def send_daily_summary():
    if not history: return
    msg = "🌟 Récapitulatif des trades (24h) :\n"
    for h in history[-50:]:
        msg += f"📈 {h['symbol']} | Entrée {h['entry']:.2f} | Sortie {h['exit']:.2f} | {h['result']:.2f}%\n"
    await bot.send_message(chat_id=CHAT_ID, text=safe_message(msg))

# === MAIN LOOP ===
async def main_loop():
    await bot.send_message(chat_id=CHAT_ID, text=f"🚀 Bot démarré {datetime.now().strftime('%H:%M:%S')}")
    last_heartbeat = None
    while True:
        try:
            now = datetime.now()
            if last_heartbeat != now.hour:
                await bot.send_message(chat_id=CHAT_ID, text=f"✅ Bot actif {now.strftime('%H:%M')}")
                last_heartbeat = now.hour
            await asyncio.gather(*(process_symbol(s) for s in SYMBOLS))
            print("✔️ Itération terminée", flush=True)
        except Exception as e:
            await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Erreur : {e}")
        await asyncio.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_loop())




