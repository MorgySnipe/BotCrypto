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
pending_retest = {}  

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
    if side == "SELL":
        history.append({
            "symbol": symbol,
            "exit": price,
            "result": gain,
            "entry": trades.get(symbol, {}).get("entry", 0),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

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

def get_last_price(symbol):
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    response = requests.get(url)
    response.raise_for_status()
    return float(response.json()['price'])

async def process_symbol(symbol):
    try:
        if symbol in trades:
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M")
            elapsed_time = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            if elapsed_time > 12:
                entry = trades[symbol]['entry']
                price = get_last_price(symbol)
                gain = ((price - entry) / entry) * 100
                await bot.send_message(chat_id=CHAT_ID, text=(
                    f"⏰ Trade {symbol} clôturé automatiquement après 12h\n"
                    f"🔚 Prix de sortie : {price:.4f} | Gain : {gain:.2f}%"
                ))
                log_trade(symbol, "SELL", price, gain)
                del trades[symbol]
                return
           
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 Analyse de {symbol}", flush=True)

        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        price = get_last_price(symbol)
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

        if not is_market_bullish():
            print(f"{symbol} ❌ Marché global baissier", flush=True)
            return
        if price < ema200 or closes_4h[-1] < ema200_4h or closes_4h[-1] < ema50_4h:
            print(f"{symbol} ❌ Sous EMA50/EMA200 (4h ou 1h)", flush=True)
            return
        if rsi_4h < 50:
            print(f"{symbol} ❌ RSI 4H < 50 (faible momentum)", flush=True)
            return
        if is_market_range(closes_4h):
            print(f"{symbol} ⚠️ Marché en range détecté, trade bloqué", flush=True)
            await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Marché en range sur {symbol} → Trade bloqué")
            return
        if detect_rsi_divergence(closes, rsis):
            print(f"{symbol} ❌ Divergence RSI détectée", flush=True)
            return
        
        volatility = get_volatility(atr, price)
        if volatility < 0.005:
            print(f"{symbol} ❌ Volatilité trop faible, blocage", flush=True)
            return

        adx_value = compute_adx(get_klines(symbol))
        supertrend_signal = compute_supertrend(get_klines(symbol))

        if adx_value < 20:
            print(f"{symbol} ❌ ADX < 20 = marché plat", flush=True)
            return

        if not supertrend_signal:
            print(f"{symbol} ❌ SuperTrend non haussier", flush=True)
            return

        if symbol in last_trade_time:
            cooldown_left = COOLDOWN_HOURS - (datetime.now() - last_trade_time[symbol]).total_seconds() / 3600
            if cooldown_left > 0:
                print(f"{symbol} ⏳ Cooldown actif: {cooldown_left:.1f}h", flush=True)
                return

        if len(trades) >= MAX_TRADES:
            print(f"{symbol} ❌ Trop de trades ouverts ({MAX_TRADES}), {symbol} ignoré", flush=True)
            return

        if not in_active_session():
            print(f"{symbol} ❌ Hors session active (UTC 00-06)", flush=True)
            return

        # --- Anti-chase / anti-pump plus strict ---
            last3_change = (closes[-1] - closes[-4]) / closes[-4]
        if last3_change > 0.022:
            print(f"{symbol} ❌ Impulsion récente trop forte (+{last3_change*100:.2f}%), on n'entre pas", flush=True)
            return

        ema25 = compute_ema(closes, 25)  # (si déjà plus haut, garde juste la ligne de contrôle)
        if price > ema25 * 1.02:
           print(f"{symbol} ❌ Prix trop éloigné de l'EMA25 (>2%), risque de chase", flush=True)
           return

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
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M")
            elapsed_time = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            if elapsed_time > 12:
                gain = ((price - entry) / entry) * 100
                await bot.send_message(chat_id=CHAT_ID, text=(
                    f"⏰ Trade {symbol} clôturé automatiquement après 12h\n"
                    f"🔚 Prix de sortie : {price:.4f} | Gain : {gain:.2f}%"
                ))
                log_trade(symbol, "SELL", price, gain)
                del trades[symbol]
                return

            gain = ((price - entry) / entry) * 100
            stop = trades[symbol].get("stop", entry - atr)

            if volatility < 0.008:
                stop = max(stop, price - atr * 0.5)
            elif volatility < 0.015:
                stop = max(stop, price - atr * 0.8)
            else:
                stop = max(stop, price - atr * 1.2)

            if rsi < 45 or macd < signal:
                await bot.send_message(chat_id=CHAT_ID, text=(
                    f"🔴 Sortie intelligente {symbol} | RSI: {rsi:.2f} | MACD < Signal\n"
                    f"💰 Prix: {price:.4f} | Gain: {gain:.2f}%"
                ))
                log_trade(symbol, "SELL", price, gain)
                del trades[symbol]
                return

            if gain > 1.5:
                stop = max(stop, entry)
            if gain > 3:
                stop = max(stop, entry * 1.01)
            if gain > 5:
                stop = max(stop, entry * 1.03)

            trades[symbol]["stop"] = stop

           # Niveaux TP
            tp1_level = 1.5 * atr
            tp2_level = 3 * atr
            tp3_level = 5 * atr

           # Initialiser historique TP
            if "tp_times" not in trades[symbol]:
               trades[symbol]["tp_times"] = {}

           # TP1
            if gain >= tp1_level / entry * 100 and not trades[symbol].get("tp1", False):
                trades[symbol]["tp1"] = True
                trades[symbol]["tp_times"]["tp1"] = datetime.now()
                await bot.send_message(chat_id=CHAT_ID, text=f"🟢 TP1 atteint sur {symbol} | Gain +{gain:.2f}%")

           # TP2 (attendre au moins 2 min après TP1)
            if gain >= tp2_level / entry * 100 and not trades[symbol].get("tp2", False):
                last_tp1_time = trades[symbol]["tp_times"].get("tp1")
                if not last_tp1_time or (datetime.now() - last_tp1_time).total_seconds() >= 120:
                    trades[symbol]["tp2"] = True
                    trades[symbol]["tp_times"]["tp2"] = datetime.now()
                    await bot.send_message(chat_id=CHAT_ID, text=f"🟢 TP2 atteint sur {symbol} | Gain +{gain:.2f}%")

           # TP3 (attendre au moins 2 min après TP2)
            if gain >= tp3_level / entry * 100 and not trades[symbol].get("tp3", False):
                last_tp2_time = trades[symbol]["tp_times"].get("tp2")
                if not last_tp2_time or (datetime.now() - last_tp2_time).total_seconds() >= 120:
                    trades[symbol]["tp3"] = True
                    trades[symbol]["tp_times"]["tp3"] = datetime.now()
                    await bot.send_message(chat_id=CHAT_ID, text=f"🟢 TP3 atteint sur {symbol} | Gain +{gain:.2f}%")
                    sell = True

            if trades[symbol].get("tp1", False) and gain < 1:
                sell = True

            if price < stop or gain <= -1.5:
                sell = True
        # === Gestion des stops dynamiques et log HOLD ===
        if symbol in trades:
            trailing_stop_advanced(symbol, trades[symbol].get("last_price", trades[symbol]["entry"]))
            log_trade(symbol, "HOLD", trades[symbol]["entry"])

        # === Achat si conditions remplies ===
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
                f"🟢 Achat {symbol} à {price:.4f} (📍 Prix Binance)\n"
                f"{label}\n{label_conf}\n"
                f"📊 RSI1h: {rsi:.2f} | RSI4h: {rsi_4h:.2f}\n"
                f"📈 MACD: {macd:.4f} / Signal: {signal:.4f}\n"
                f"📦 Volatilité ATR: {volatility:.4%}\n"
                f"📉 SL ATR: {price - atr:.4f}\n"
                f"💰 Capital conseillé : {position_pct:.0f}% du portefeuille"
            ))
            log_trade(symbol, "BUY", price)

        # === Vente si conditions remplies ===
        elif sell and symbol in trades:
            entry = trades[symbol]['entry']
            gain = ((price - entry) / entry) * 100
            stop_used = trades[symbol].get("stop", entry - atr)
            await bot.send_message(chat_id=CHAT_ID, text=(
                f"🔴 Vente {symbol} à {price:.4f} | Gain {gain:.2f}% | Stop final: {stop_used:.4f}"
            ))
            log_trade(symbol, "SELL", price, gain)
            del trades[symbol]


    except Exception as e:
        print(f"❌ Erreur {symbol}: {e}", flush=True)
        traceback.print_exc()
async def process_symbol_aggressive(symbol):
    try:
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        price = get_last_price(symbol)

        # Breakout léger
        breakout = price > max(highs[-10:]) * 1.005
        if breakout:
            # --- Anti-chase / anti-pump (aggressive) ---
            last3_change = (closes[-1] - closes[-4]) / closes[-4]
            if last3_change > 0.022:
                print(f"{symbol} ❌ Impulsion récente trop forte (+{last3_change*100:.2f}%), on n'entre pas (aggr.)", flush=True)
                return

            ema25 = compute_ema(closes, 25)
            if price >= ema25 * 1.02:
                print(f"{symbol} ❌ Prix trop éloigné de l'EMA25 (>2%), risque de chase (aggr.)", flush=True)
                return

        # Indicateurs
        macd_line, macd_signal = compute_macd(closes)
        indicators = {
            "rsi": compute_rsi(closes),
            "macd": macd_line,
            "signal": macd_signal,
            "supertrend": compute_supertrend(klines),
            "adx": compute_adx(klines),
            "volume_ok": np.mean([float(k[5]) for k in klines][-5:]) > np.mean([float(k[5]) for k in klines][-20:]),
            "above_ema200": price > compute_ema(closes, 200),
        }
        score = compute_confidence_score(indicators)
        rsi_now = indicators["rsi"]
        atr_val = compute_atr(klines)

        # Entrée agressive
        if score >= 3 and rsi_now < 85:
            trades[symbol] = {
                "entry": price,
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "confidence": score,
                "stop": price - 0.8 * atr_val,  # Stop loss dynamique
                "position_pct": 5,
            }
            await bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"⚡ **Signal AGRESSIF** {symbol} à {price:.4f} (🎯 Prix Binance)\n"
                    f"🔍 Stratégie : Breakout anticipé + Retest\n{label_confidence(score)}\n"
                    f"RSI: {rsi_now:.2f} | MACD: {indicators['macd']:.2f} / Signal: {indicators['signal']:.2f}\n"
                    f"ADX: {indicators['adx']:.2f} | SL initial: {price - 0.8 * atr_val:.4f}\n"
                    f"⚠️ **Risque accru, entrée anticipée**"
                )
            )

    except Exception as e:
        print(f"❌ Erreur stratégie agressive {symbol}: {e}")
        traceback.print_exc()


def is_recent(ts_str):
    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    return (datetime.now() - ts).total_seconds() <= 86400

async def send_daily_summary():
    if not history: return
    recent = [h for h in history if is_recent(h.get("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))]
    if not recent:
        await bot.send_message(chat_id=CHAT_ID, text="ℹ️ Aucun trade clôturé dans les dernières 24h.")
        return
    msg = "🌟 Récapitulatif des trades (24h) :\n"
    for h in recent:
        msg += f"📈 {h['symbol']} | Entrée {h['entry']:.2f} | Sortie {h['exit']:.2f} | {h['result']:.2f}%\n"
    await bot.send_message(chat_id=CHAT_ID, text=safe_message(msg))

async def main_loop():
    await bot.send_message(chat_id=CHAT_ID, text=f"🚀 Bot démarré {datetime.now().strftime('%H:%M:%S')}")
    last_heartbeat = None
    last_summary_day = None  # 🆕 Ajout pour le résumé journalier

    while True:
        try:
            now = datetime.now()

            # ✅ Message de vie toutes les heures
            if last_heartbeat != now.hour:
                await bot.send_message(chat_id=CHAT_ID, text=f"✅ Bot actif {now.strftime('%H:%M')}")
                last_heartbeat = now.hour

            # ✅ Résumé quotidien à 23h UTC
            if now.hour == 23 and (last_summary_day is None or last_summary_day != now.date()):
                await send_daily_summary()
                last_summary_day = now.date()

            await asyncio.gather(*(process_symbol(s) for s in SYMBOLS))
            await asyncio.gather(*(process_symbol_aggressive(s) for s in SYMBOLS))
            print("✔️ Itération terminée", flush=True)

        except Exception as e:
            await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Erreur : {e}")

        await asyncio.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_loop())
