import asyncio
import requests
import numpy as np
from datetime import datetime, timezone, timedelta
from telegram import Bot
import nest_asyncio
import traceback
import csv

nest_asyncio.apply()

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
LOG_FILE = "trade_log.csv"

def safe_message(text):
    return text if len(text) < 4000 else text[:3900] + "\n... (tronqué)"

def get_klines(symbol, interval='1h', limit=100):
    url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def get_last_price(symbol):
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    response = requests.get(url)
    response.raise_for_status()
    return float(response.json()['price'])

def is_top_performer(symbol, top_n=5):
    try:
        url = f"https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url).json()
        perf = {d["symbol"]: float(d["priceChangePercent"]) for d in data if "USDT" in d["symbol"]}
        sorted_perf = sorted(perf.items(), key=lambda x: x[1], reverse=True)
        top_symbols = [s[0] for s in sorted_perf[:top_n]]
        return symbol in top_symbols
    except:
        return False

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
    return not (0 <= hour < 6)

def get_klines_4h(symbol, limit=100):
    return get_klines(symbol, interval='4h', limit=limit)

def is_market_range(prices, threshold=0.01):
    return (max(prices[-20:]) - min(prices[-20:])) / min(prices[-20:]) < threshold

def get_volatility(atr, price):
    return atr / price

def compute_adx(klines, period=14):
    highs = np.array([float(k[2]) for k in klines])
    lows = np.array([float(k[3]) for k in klines])
    closes = np.array([float(k[4]) for k in klines])
    plus_dm = highs[1:] - highs[:-1]
    minus_dm = lows[:-1] - lows[1:]
    plus_dm = np.where((plus_dm > minus_dm) & (plus_dm > 0), plus_dm, 0)
    minus_dm = np.where((minus_dm > plus_dm) & (minus_dm > 0), minus_dm, 0)
    tr = np.maximum(highs[1:], closes[:-1]) - np.minimum(lows[1:], closes[:-1])
    atr = np.convolve(tr, np.ones(period)/period, mode='valid')
    plus_di = 100 * np.convolve(plus_dm, np.ones(period)/period, mode='valid') / atr
    minus_di = 100 * np.convolve(minus_dm, np.ones(period)/period, mode='valid') / atr
    dx = (np.abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
    adx = np.convolve(dx, np.ones(period)/period, mode='valid')
    return adx[-1] if len(adx) > 0 else 0

def compute_supertrend(klines, period=10, multiplier=3):
    atr = compute_atr(klines, period)
    highs = np.array([float(k[2]) for k in klines])
    lows = np.array([float(k[3]) for k in klines])
    closes = np.array([float(k[4]) for k in klines])
    hl2 = (highs + lows) / 2
    lowerband = hl2[-1] - multiplier * atr
    return closes[-1] > lowerband

def log_trade(symbol, side, price, gain=0):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, side, price, gain])

def trailing_stop_advanced(symbol, current_price):
    if symbol in trades:
        entry = trades[symbol]['entry']
        gain = ((current_price - entry) / entry) * 100
        atr_val = compute_atr(get_klines(symbol))
        if gain > 2:
            trades[symbol]["stop"] = max(trades[symbol]["stop"], current_price - 0.7 * atr_val)
        if gain > 4:
            trades[symbol]["stop"] = max(trades[symbol]["stop"], current_price - 0.5 * atr_val)
        if gain > 7:
            trades[symbol]["stop"] = max(trades[symbol]["stop"], current_price - 0.3 * atr_val)

def compute_confidence_score(indicators):
    score = 0
    if indicators["rsi"] > 50 and indicators["rsi"] < 70: score += 2
    if indicators["macd"] > indicators["signal"]: score += 2
    if indicators["supertrend"]: score += 2
    if indicators["adx"] > 25: score += 2
    if indicators["volume_ok"]: score += 1
    if indicators["above_ema200"]: score += 1
    return min(score, 10)

def label_confidence(score):
    if score >= 8: return f"📊 Fiabilité : {score}/10 (Très Fiable)"
    elif score >= 5: return f"📊 Fiabilité : {score}/10 (Fiable)"
    elif score >= 3: return f"📊 Fiabilité : {score}/10 (Risque)"
    else: return f"📊 Fiabilité : {score}/10 (Très Risqué)"
async def process_symbol(symbol):
    try:
        adx_value = compute_adx(get_klines(symbol))
        supertrend_signal = compute_supertrend(get_klines(symbol))
        if adx_value < 20:
            print(f"{symbol} ❌ ADX < 20 → marché plat", flush=True)
            return
        if not supertrend_signal:
            print(f"{symbol} ❌ SuperTrend non haussier", flush=True)
            return

        if symbol in trades:
            trailing_stop_advanced(symbol, trades[symbol].get("last_price", trades[symbol]["entry"]))
            log_trade(symbol, "HOLD", trades[symbol]["entry"])

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

        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        price = get_last_price(symbol)  # ✅ Ajout

        rsi = compute_rsi(closes)
        macd, signal = compute_macd(closes)
        ema200 = compute_ema(closes, 200)
        atr = compute_atr(klines)
        rsis = [compute_rsi(closes[i-14:i]) for i in range(14, len(closes))]
        ema25 = compute_ema(closes, 25)

        klines_4h = get_klines_4h(symbol)
        closes_4h = [float(k[4]) for k in klines_4h]
        ema200_4h = compute_ema(closes_4h, 200)
        ema50_4h = compute_ema(closes_4h, 50)
        rsi_4h = compute_rsi(closes_4h)

        # ✅ Ajouts recommandés
        if not is_top_performer(symbol):
            print(f"{symbol} ❌ Pas dans le top performers du jour", flush=True)
            return

        breakout_1h = price > max(closes[-10:]) * 1.01
        breakout_4h = closes_4h[-1] > max(closes_4h[-10:]) * 1.01
        if not (breakout_1h and breakout_4h):
            print(f"{symbol} ❌ Breakout non confirmé sur plusieurs TF", flush=True)
            return

        if np.mean(volumes[-5:]) < 1.3 * np.mean(volumes[-20:]):
            print(f"{symbol} ❌ Volume pas assez explosif (pas de momentum fort)", flush=True)
            return

        expected_tp = price * 1.03
        rr_ratio = (expected_tp - price) / (price - (price - atr))
        if rr_ratio < 1.5:
            print(f"{symbol} ❌ R:R trop faible (<1.5), pas de trade", flush=True)
            return

        # Conditions standards du bot
        if not is_market_bullish(): return
        if price < ema200 or closes_4h[-1] < ema200_4h or closes_4h[-1] < ema50_4h: return
        if rsi_4h < 50: return
        if is_market_range(closes_4h): return
        if detect_rsi_divergence(closes, rsis): return
        if (highs[-1] - lows[-1]) / lows[-1] > 0.05: return
        if price > min(lows[-5:]) * 1.03: return
        if np.mean(volumes[-5:]) < 0.8 * np.mean(volumes[-20:]): return
        if rsi > 80 or rsi_4h > 75: return
        if price > ema25 * 1.03: return
        if get_volatility(atr, price) < 0.005: return

        buy = False
        label = ""
        position_pct = 5
        indicators = {
            "rsi": rsi,
            "macd": macd,
            "signal": signal,
            "supertrend": supertrend_signal,
            "adx": adx_value,
            "volume_ok": np.mean(volumes[-5:]) > np.mean(volumes[-20:]),
            "above_ema200": price > ema200
        }
        confidence = compute_confidence_score(indicators)
        label_conf = label_confidence(confidence)

        if is_uptrend(closes) and macd > signal and rsi > 50:
            buy = True
            label = "💎 Trend EMA200/50 + MACD + RSI confirmé (1h/4h)"
            position_pct = 7

        sell = False
        if symbol in trades:
            entry = trades[symbol]['entry']
            gain = ((price - entry) / entry) * 100
            stop = trades[symbol].get("stop", entry - atr)

            # ✅ Sortie chandelle explosive
            last_candle_body = closes[-1] - closes[-2]
            if last_candle_body / closes[-2] > 0.04 and gain > 3:
                await bot.send_message(chat_id=CHAT_ID, text=f"🚀 Chandelle explosive détectée sur {symbol} | Vente agressive +{gain:.2f}%")
                log_trade(symbol, "SELL", price, gain)
                del trades[symbol]
                return

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
                f"🟢 Achat {symbol} à {price:.4f}\n{label}\n{label_conf}\n"
                f"📊 RSI1h: {rsi:.2f} | RSI4h: {rsi_4h:.2f}\n"
                f"📈 MACD: {macd:.4f} / Signal: {signal:.4f}\n"
                f"📦 Volatilité ATR: {get_volatility(atr, price):.4%}\n📉 SL ATR: {price - atr:.4f}"
            ))
            log_trade(symbol, "BUY", price)

async def process_symbol_aggressive(symbol):
    try:
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        price = get_last_price(symbol)  # ✅ temps réel

        breakout = price > max(highs[-10:]) * 1.005
        if breakout:
            indicators = {
                "rsi": compute_rsi(closes),
                "macd": compute_macd(closes)[0],
                "signal": compute_macd(closes)[1],
                "supertrend": compute_supertrend(klines),
                "adx": compute_adx(klines),
                "volume_ok": np.mean([float(k[5]) for k in klines][-5:]) > np.mean([float(k[5]) for k in klines][-20:]),
                "above_ema200": price > compute_ema(closes, 200)
            }
            score = compute_confidence_score(indicators)
            rsi_now = indicators["rsi"]
            atr_val = compute_atr(klines)
            if score >= 3 and rsi_now < 85:
                trades[symbol] = {
                    "entry": price,
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "confidence": score,
                    "stop": price - 0.8 * atr_val,
                    "position_pct": 5
                }
                await bot.send_message(chat_id=CHAT_ID, text=(
                    f"⚡ **Signal AGRESSIF** {symbol} à {price:.4f}\n🔍 Breakout anticipé\n{label_confidence(score)}\n"
                    f"RSI: {rsi_now:.2f} | MACD: {indicators['macd']:.2f} / Signal: {indicators['signal']:.2f}\n"
                    f"ADX: {indicators['adx']:.2f} | SL: {price - 0.8 * atr_val:.4f}"
                ))
    except Exception as e:
        print(f"❌ Erreur stratégie agressive {symbol}: {e}")
        traceback.print_exc()

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
            await asyncio.gather(*(process_symbol_aggressive(s) for s in SYMBOLS))
            print("✔️ Itération terminée", flush=True)
        except Exception as e:
            await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Erreur : {e}")
        await asyncio.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_loop())


