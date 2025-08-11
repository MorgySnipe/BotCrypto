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

# ====== META / HELPERS POUR MESSAGES & IDs ======
BOT_VERSION = "v1.0.0"

def utc_now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def make_trade_id(symbol: str) -> str:
    return f"{symbol}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

def st_onoff(st_bool: bool) -> str:
    return "ON" if st_bool else "OFF"

def format_entry_msg(symbol, trade_id, strategy, bot_version, entry, position_pct,
                     sl_initial, sl_dist_pct, atr,
                     rsi_1h, macd, signal, adx,
                     st_on,  # <— NOUVEAU paramètre: bool supertrend
                     ema25, ema50_4h, ema200_1h, ema200_4h,
                     vol5, vol20, vol_ratio,
                     btc_up, eth_up,
                     score, score_label,
                     reasons: list[str]):
    return (
        f"🟢 ACHAT | {symbol} | trade_id={trade_id}\n"
        f"⏱ UTC: {utc_now_str()} | Stratégie: {strategy} | Version: {bot_version}\n"
        f"🎯 Prix entrée: {entry:.4f} | Taille: {position_pct:.1f}%\n"
        f"🛡 Stop initial: {sl_initial:.4f} (dist: {sl_dist_pct:.2f}%) | ATR(1h): {atr:.4f}\n"
        f"🎯 TP1/TP2/TP3: +1.5% / +3% / +5% (dynamiques)\n\n"
        f"📊 Indicateurs 1H: RSI {rsi_1h:.2f} | MACD {macd:.4f}/{signal:.4f} | ADX {adx:.2f} | Supertrend {st_onoff(st_on)}\n"
        f"📈 Tendances: EMA25 {ema25:.4f} | EMA50(4h) {ema50_4h:.4f} | EMA200(1h) {ema200_1h:.4f} | EMA200(4h) {ema200_4h:.4f}\n"
        f"📦 Volume: MA5 {vol5:.0f} | MA20 {vol20:.0f} | Ratio {vol_ratio:.2f}x\n"
        f"🌐 Contexte marché: BTC uptrend={btc_up} | ETH uptrend={eth_up}\n"
        f"🧠 Score fiabilité: {score}/10 — {score_label}\n\n"
        f"📌 Raison d’entrée:\n- " + "\n- ".join(reasons)
    )

def format_tp_msg(n, symbol, trade_id, price, gain_pct, new_stop, stop_from_entry_pct, elapsed_h, action_after_tp):
    return (
        f"🟢 TP{n} ATTEINT | {symbol} | trade_id={trade_id}\n"
        f"⏱ UTC: {utc_now_str()} | Gain courant: {gain_pct:.2f}% | Prix: {price:.4f}\n"
        f"📌 Actions: {action_after_tp}\n"
        f"🔒 Nouveau stop: {new_stop:.4f} | Distance vs entrée: {stop_from_entry_pct:.2f}%\n"
        f"⏳ Temps depuis entrée: {elapsed_h:.2f} h"
    )

def format_hold_msg(symbol, trade_id, price, gain_pct, stop, atr_mult, rsi_1h, macd, signal, adx):
    return (
        f"ℹ️ MÀJ TRADE | {symbol} | trade_id={trade_id}\n"
        f"⏱ UTC: {utc_now_str()} | Prix: {price:.4f} | Gain: {gain_pct:.2f}%\n"
        f"🔧 Stop traînant: {stop:.4f} (méthode: ATR x {atr_mult})\n"
        f"📊 RSI {rsi_1h:.1f} | MACD {macd:.3f}/{signal:.3f} | ADX {adx:.1f}"
    )

def format_exit_msg(symbol, trade_id, exit_price, pnl_pct, stop, elapsed_h, exit_reason):
    return (
        f"🔴 SORTIE TECHNIQUE | {symbol} | trade_id={trade_id}\n"
        f"⏱ UTC: {utc_now_str()} | Prix sortie: {exit_price:.4f} | P&L: {pnl_pct:.2f}%\n"
        f"📌 Raison sortie: {exit_reason}\n"
        f"🔒 Stop final au moment de la sortie: {stop:.4f}\n"
        f"⏳ Durée du trade: {elapsed_h:.2f} h"
    )

def format_stop_msg(symbol, trade_id, stop_price, pnl_pct, rsi_1h, adx, vol_ratio):
    return (
        f"🔴 STOP TOUCHÉ | {symbol} | trade_id={trade_id}\n"
        f"⏱ UTC: {utc_now_str()} | Stop: {stop_price:.4f} | P&L: {pnl_pct:.2f}%\n"
        f"📊 Contexte à la sortie: RSI {rsi_1h:.1f} | ADX {adx:.1f} | Vol ratio {vol_ratio:.2f}x"
    )

def format_autoclose_msg(symbol, trade_id, exit_price, pnl_pct):
    return (
        f"⏰ AUTO-CLOSE 12h | {symbol} | trade_id={trade_id}\n"
        f"⏱ UTC: {utc_now_str()} | Prix: {exit_price:.4f} | P&L: {pnl_pct:.2f}%\n"
        f"📌 Raison: durée > 12h sans confirmation"
    )
# ====== /HELPERS ======


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

def compute_ema_series(prices, period):
    """Retourne une série EMA complète."""
    ema = [prices[0]]
    k = 2 / (period + 1)
    for p in prices[1:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return np.array(ema)

def compute_macd(prices, short=12, long=26, signal=9):
    """MACD avec EMA exponentielles réelles."""
    ema_short = compute_ema_series(prices, short)
    ema_long = compute_ema_series(prices, long)
    macd_line = ema_short - ema_long
    signal_line = compute_ema_series(macd_line, signal)
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

def detect_breakout_retest(closes, highs, lookback=10, tol=0.003):
    """
    Détecte une séquence Breakout + Retest en 1h.
    - Breakout: bougie -2 clôture au-dessus du plus haut des `lookback` bougies précédentes
    - Retest:   bougie -1 clôture proche (±tol) du niveau de breakout et >= level
    tol = 0.003 -> 0,3% de tolérance
    Retourne (ok: bool, level: float)
    """
    if len(highs) < lookback + 3 or len(closes) < lookback + 3:
        return False, None

    level = max(highs[-(lookback+2):-2])          # plus haut avant la bougie -2
    breakout = closes[-2] > level * 1.003         # close -2 > +0,3% au-dessus du level
    # Retest simple/robuste sans utiliser les lows (évite repaint)
    retest = (abs(closes[-1] - level) / level) <= tol and (closes[-1] >= level)

    return (breakout and retest), level

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
# ====== CSV détaillé (audit) ======
CSV_AUDIT_FILE = "trade_audit.csv"
CSV_AUDIT_FIELDS = [
    "ts_utc","trade_id","symbol","event","strategy","version",
    "entry","exit","price","pnl_pct","position_pct",
    "sl_initial","sl_final","atr_1h","atr_mult_at_entry",
    "rsi_1h","macd","signal","adx_1h","supertrend_on",
    "ema25_1h","ema200_1h","ema50_4h","ema200_4h",
    "vol_ma5","vol_ma20","vol_ratio",
    "btc_uptrend","eth_uptrend",
    "reason_entry","reason_exit"
]

def log_trade_csv(row: dict):
    """Écrit/append une ligne dans trade_audit.csv avec l'en-tête s'il manque."""
    import os, csv
    header_needed = not os.path.exists(CSV_AUDIT_FILE) or os.path.getsize(CSV_AUDIT_FILE) == 0
    with open(CSV_AUDIT_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_AUDIT_FIELDS)
        if header_needed:
            w.writeheader()
        # Ne garder que les champs attendus
        clean = {k: row.get(k, "") for k in CSV_AUDIT_FIELDS}
        w.writerow(clean)
# ====== /CSV détaillé ======

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
        # --- Auto-close après 12h si une position existe ---
        if symbol in trades:
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            elapsed_time = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            if elapsed_time > 12:
                entry = trades[symbol]['entry']
                price = get_last_price(symbol)
                pnl = ((price - entry) / entry) * 100
                trade_id = trades[symbol].get("trade_id", make_trade_id(symbol))

                msg = format_autoclose_msg(symbol, trade_id, price, pnl)
                await bot.send_message(chat_id=CHAT_ID, text=safe_message(msg))
                log_trade_csv({
                    "ts_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "event": "AUTO_CLOSE",
                    "strategy": "standard",
                    "version": BOT_VERSION,
                    "entry": entry,
                    "exit": price,
                    "price": price,
                    "pnl_pct": pnl,
                    "position_pct": trades[symbol].get("position_pct", ""),
                    "sl_initial": "",
                    "sl_final": trades[symbol].get("stop", ""),
                    "atr_1h": "",
                    "reason_exit": "timeout > 12h"
                })
                log_trade(symbol, "SELL", price, pnl)
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

        if not is_market_bullish(): return
        if price < ema200 or closes_4h[-1] < ema200_4h or closes_4h[-1] < ema50_4h: return
        if rsi_4h < 50: return
        if is_market_range(closes_4h):
            await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Marché en range sur {symbol} → Trade bloqué")
            return
        if detect_rsi_divergence(closes, rsis): return

        volatility = get_volatility(atr, price)
        if volatility < 0.005: return

        adx_value = compute_adx(get_klines(symbol))
        supertrend_signal = compute_supertrend(get_klines(symbol))
        if adx_value < 20: return
        if not supertrend_signal: return

        if symbol in last_trade_time:
            cooldown_left = COOLDOWN_HOURS - (datetime.now() - last_trade_time[symbol]).total_seconds() / 3600
            if cooldown_left > 0: return
        if len(trades) >= MAX_TRADES: return
        if not in_active_session(): return
        if price > ema25 * 1.02: return

        buy = False
        position_pct = 5
        indicators = {
            "rsi": rsi,
            "macd": macd,
            "signal": signal,
            "supertrend": supertrend_signal,
            "adx": adx_value,
            "volume_ok": np.mean(volumes[-5:]) > np.mean(volumes[-20:]),
            "above_ema200": price > ema200,
        }
        confidence = compute_confidence_score(indicators)
        label_conf = label_confidence(confidence)

        volume_ok = np.mean(volumes[-5:]) > np.mean(volumes[-20:])
        trend_ok = (price > ema200) and supertrend_signal and (adx_value >= 22)
        momentum_ok = (macd > signal) and (rsi >= 55)

        brk_ok, _ = detect_breakout_retest(closes, highs, lookback=10, tol=0.003)
        last3_change = (closes[-1] - closes[-4]) / closes[-4]
        if last3_change > 0.022: brk_ok = False

        if brk_ok and trend_ok and momentum_ok and volume_ok:
            buy = True
            label = "⚡ Breakout + Retest validé (1h) + Confluence"
            position_pct = 7
        elif trend_ok and momentum_ok and volume_ok:
            near_ema25 = price <= ema25 * 1.01
            candle_ok = (abs(highs[-1] - lows[-1]) / max(lows[-1], 1e-9)) <= 0.03
            if near_ema25 and candle_ok:
                buy = True
                label = "✅ Pullback EMA25 propre + Confluence"
                position_pct = 6

        # === GESTION TP / HOLD / SELL ===
        sell = False
        if symbol in trades:
            entry = trades[symbol]['entry']
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            elapsed_time = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            gain = ((price - entry) / entry) * 100
            stop = trades[symbol].get("stop", entry - 0.6 * atr)

            if volatility < 0.008:
                stop = max(stop, price - atr * 0.5)
            elif volatility < 0.015:
                stop = max(stop, price - atr * 0.8)
            else:
                stop = max(stop, price - atr * 1.2)

            if rsi < 45 or macd < signal:
                msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, stop, elapsed_time, "RSI bas ou MACD croisé à la baisse")
                await bot.send_message(chat_id=CHAT_ID, text=safe_message(msg))
                log_trade(symbol, "SELL", price, gain)
                del trades[symbol]
                return

            tp_levels = {1: 1.5, 2: 3.0, 3: 5.0}
            if "tp_times" not in trades[symbol]:
                trades[symbol]["tp_times"] = {}
            for tp_num, tp_pct in tp_levels.items():
                if gain >= tp_pct and not trades[symbol].get(f"tp{tp_num}", False):
                    last_tp_time = trades[symbol]["tp_times"].get(f"tp{tp_num-1}") if tp_num > 1 else None
                    if not last_tp_time or (datetime.now() - last_tp_time).total_seconds() >= 120:
                        trades[symbol][f"tp{tp_num}"] = True
                        trades[symbol]["tp_times"][f"tp{tp_num}"] = datetime.now()
                        new_stop = entry * (1 + (tp_pct - 0.5) / 100) if tp_num > 1 else entry
                        trades[symbol]["stop"] = max(stop, new_stop)
                        msg = format_tp_msg(tp_num, symbol, trades[symbol]["trade_id"], price, gain, trades[symbol]["stop"], ((trades[symbol]["stop"] - entry) / entry) * 100, elapsed_time, "Stop ajusté")
                        await bot.send_message(chat_id=CHAT_ID, text=safe_message(msg))

            if trades[symbol].get("tp1", False) and gain < 1:
                sell = True
            if price < trades[symbol]["stop"] or gain <= -1.5:
                msg = format_stop_msg(symbol, trades[symbol]["trade_id"], trades[symbol]["stop"], gain, rsi, adx_value, np.mean(volumes[-5:]) / np.mean(volumes[-20:]))
                await bot.send_message(chat_id=CHAT_ID, text=safe_message(msg))
                sell = True
            if not sell:
                log_trade(symbol, "HOLD", price)

        if buy and symbol not in trades:
            trade_id = make_trade_id(symbol)
            trades[symbol] = {
                "entry": price,
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "confidence": confidence,
                "stop": price - 0.6 * atr,
                "position_pct": position_pct,
                "trade_id": trade_id
            }
            last_trade_time[symbol] = datetime.now()
            reasons = [label, f"ADX {adx_value:.1f} >= 22", f"MACD {macd:.3f} > Signal {signal:.3f}"]
            msg = format_entry_msg(
                symbol, trade_id, "standard", BOT_VERSION, price, position_pct,
                price - 0.6 * atr, ((price - (price - 0.6 * atr)) / price) * 100, atr,
                rsi, macd, signal, adx_value, supertrend_signal,
                ema25, ema50_4h, ema200, ema200_4h,
                np.mean(volumes[-5:]), np.mean(volumes[-20:]),
                np.mean(volumes[-5:]) / np.mean(volumes[-20:]),
                is_uptrend([float(k[4]) for k in get_klines("BTCUSDT")]),
                is_uptrend([float(k[4]) for k in get_klines("ETHUSDT")]),
                confidence, label_conf, reasons
            )
            await bot.send_message(chat_id=CHAT_ID, text=safe_message(msg))
            log_trade(symbol, "BUY", price)

        elif sell and symbol in trades:
            entry = trades[symbol]['entry']
            gain = ((price - entry) / entry) * 100
            stop_used = trades[symbol].get("stop", entry - 0.6 * atr)
            await bot.send_message(chat_id=CHAT_ID, text=f"🔴 Vente {symbol} à {price:.4f} | Gain {gain:.2f}% | Stop final: {stop_used:.4f}")
            log_trade(symbol, "SELL", price, gain)
            del trades[symbol]

    except Exception as e:
        print(f"❌ Erreur {symbol}: {e}", flush=True)
        traceback.print_exc()

async def process_symbol_aggressive(symbol):
    try:
        # --- Auto-close après 12h si une position existe ---
        if symbol in trades:
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            elapsed_time = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            if elapsed_time > 12:
                entry = trades[symbol]['entry']
                price = get_last_price(symbol)
                pnl = ((price - entry) / entry) * 100
                trade_id = trades[symbol].get("trade_id", make_trade_id(symbol))

                # Message auto-close
                msg = format_autoclose_msg(symbol, trade_id, price, pnl)
                await bot.send_message(chat_id=CHAT_ID, text=safe_message(msg))

                # Log CSV
                log_trade_csv({
                    "ts_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "event": "AUTO_CLOSE",
                    "strategy": "aggressive",
                    "version": BOT_VERSION,
                    "entry": entry,
                    "exit": price,
                    "price": price,
                    "pnl_pct": pnl,
                    "position_pct": trades[symbol].get("position_pct", ""),
                    "sl_initial": "",
                    "sl_final": trades[symbol].get("stop", ""),
                    "atr_1h": "",
                    "atr_mult_at_entry": "",
                    "rsi_1h": "",
                    "macd": "",
                    "signal": "",
                    "adx_1h": "",
                    "supertrend_on": "",
                    "ema25_1h": "",
                    "ema200_1h": "",
                    "ema50_4h": "",
                    "ema200_4h": "",
                    "vol_ma5": "",
                    "vol_ma20": "",
                    "vol_ratio": "",
                    "btc_uptrend": "",
                    "eth_uptrend": "",
                    "reason_entry": "",
                    "reason_exit": "timeout > 12h"
                })

                log_trade(symbol, "SELL", price, pnl)
                del trades[symbol]
                return

        # ---- Analyse agressive ----
        klines = get_klines(symbol)               # 1h
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        price = get_last_price(symbol)

        # ---- Garde-fous ----
        if len(trades) >= MAX_TRADES:
            return
        if not in_active_session():
            return
        if not is_market_bullish():
            return
        if symbol in last_trade_time:
            cooldown_left = COOLDOWN_HOURS - (datetime.now() - last_trade_time[symbol]).total_seconds() / 3600
            if cooldown_left > 0:
                return

        # ---- Confluence 4h ----
        k4 = get_klines_4h(symbol)
        c4 = [float(k[4]) for k in k4]
        ema50_4h = compute_ema(c4, 50)
        ema200_4h = compute_ema(c4, 200)
        if c4[-1] < ema50_4h or ema50_4h < ema200_4h:
            return

        # ---- Breakout + retest ----
        last10_high = max(highs[-10:])
        breakout = price > last10_high * 1.008
        if not breakout:
            return

        last3_change = (closes[-1] - closes[-4]) / closes[-4]
        if last3_change > 0.022:
            return

        ema25 = compute_ema(closes, 25)
        if price >= ema25 * 1.02:
            return
        retest_tolerance = 0.003
        if abs(price - last10_high) / last10_high > retest_tolerance and abs(price - ema25) / ema25 > retest_tolerance:
            return

        # ---- Indicateurs ----
        macd_line, macd_signal = compute_macd(closes)
        macd_line_prev, macd_signal_prev = compute_macd(closes[:-1])
        supertrend_ok = compute_supertrend(klines)
        adx_val = compute_adx(klines)
        above_ema200 = price > compute_ema(closes, 200)
        vol5 = np.mean(volumes[-5:])
        vol20 = np.mean(volumes[-20:])
        volume_ok = (vol5 > vol20 * 1.2) and (vol5 > MIN_VOLUME)

        if not (supertrend_ok and adx_val >= 22 and above_ema200 and volume_ok):
            return
        if not (macd_line > macd_signal and macd_line - macd_signal > macd_line_prev - macd_signal_prev):
            return

        rsi_now = compute_rsi(closes)
        if not (55 <= rsi_now < 80):
            return

        indicators = {
            "rsi": rsi_now,
            "macd": macd_line,
            "signal": macd_signal,
            "supertrend": supertrend_ok,
            "adx": adx_val,
            "volume_ok": volume_ok,
            "above_ema200": above_ema200,
        }
        score = compute_confidence_score(indicators)
        label_conf = label_confidence(score)
        if score < 6:
            return

        # ---- Entrée ----
        atr_val = compute_atr(klines)
        trade_id = make_trade_id(symbol)
        trades[symbol] = {
            "entry": price,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "confidence": score,
            "stop": price - 0.6 * atr_val,
            "position_pct": 5,
            "trade_id": trade_id,
            "tp_times": {}
        }
        last_trade_time[symbol] = datetime.now()

        reasons = [
            "Breakout+Retest validé",
            f"ADX {adx_val:.1f} >= 22",
            f"MACD {macd_line:.3f} > Signal {macd_signal:.3f}"
        ]

        msg = format_entry_msg(
            symbol, trade_id, "aggressive", BOT_VERSION, price, 5,
            price - 0.6 * atr_val, ((price - (price - 0.6 * atr_val)) / price) * 100, atr_val,
            rsi_now, macd_line, macd_signal, adx_val,
            supertrend_ok, ema25, ema50_4h, above_ema200, ema200_4h,
            vol5, vol20, vol5 / vol20,
            is_uptrend([float(k[4]) for k in get_klines("BTCUSDT")]),
            is_uptrend([float(k[4]) for k in get_klines("ETHUSDT")]),
            score, label_conf, reasons
        )
        await bot.send_message(chat_id=CHAT_ID, text=safe_message(msg))

        log_trade_csv({
            "ts_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "trade_id": trade_id,
            "symbol": symbol,
            "event": "BUY",
            "strategy": "aggressive",
            "version": BOT_VERSION,
            "entry": price,
            "exit": "",
            "price": price,
            "pnl_pct": "",
            "position_pct": 5,
            "sl_initial": price - 0.6 * atr_val,
            "sl_final": "",
            "atr_1h": atr_val,
            "atr_mult_at_entry": 0.6,
            "rsi_1h": rsi_now,
            "macd": macd_line,
            "signal": macd_signal,
            "adx_1h": adx_val,
            "supertrend_on": supertrend_ok,
            "ema25_1h": ema25,
            "ema200_1h": above_ema200,
            "ema50_4h": ema50_4h,
            "ema200_4h": ema200_4h,
            "vol_ma5": vol5,
            "vol_ma20": vol20,
            "vol_ratio": vol5 / vol20,
            "btc_uptrend": is_uptrend([float(k[4]) for k in get_klines("BTCUSDT")]),
            "eth_uptrend": is_uptrend([float(k[4]) for k in get_klines("ETHUSDT")]),
            "reason_entry": "; ".join(reasons),
            "reason_exit": ""
        })
        log_trade(symbol, "BUY", price)

        # ---- Gestion TP / HOLD / SELL ----
        gain = ((price - trades[symbol]['entry']) / trades[symbol]['entry']) * 100
        stop = trades[symbol].get("stop", trades[symbol]['entry'] - 0.6 * atr_val)

        if gain >= 1.5 and not trades[symbol].get("tp1", False):
            trades[symbol]["tp1"] = True
            trades[symbol]["tp_times"]["tp1"] = datetime.now()
            await bot.send_message(chat_id=CHAT_ID, text=f"🟢 TP1 atteint sur {symbol} | Gain +{gain:.2f}%")

        if gain >= 3.0 and not trades[symbol].get("tp2", False):
            last_tp1_time = trades[symbol]["tp_times"].get("tp1")
            if not last_tp1_time or (datetime.now() - last_tp1_time).total_seconds() >= 120:
                trades[symbol]["tp2"] = True
                trades[symbol]["tp_times"]["tp2"] = datetime.now()
                await bot.send_message(chat_id=CHAT_ID, text=f"🟢 TP2 atteint sur {symbol} | Gain +{gain:.2f}%")

        if gain >= 5.0 and not trades[symbol].get("tp3", False):
            last_tp2_time = trades[symbol]["tp_times"].get("tp2")
            if not last_tp2_time or (datetime.now() - last_tp2_time).total_seconds() >= 120:
                trades[symbol]["tp3"] = True
                trades[symbol]["tp_times"]["tp3"] = datetime.now()
                await bot.send_message(chat_id=CHAT_ID, text=f"🟢 TP3 atteint sur {symbol} | Gain +{gain:.2f}%")
                log_trade(symbol, "SELL", price, gain)
                del trades[symbol]
                return

        if trades[symbol].get("tp1", False) and gain < 1:
            log_trade(symbol, "SELL", price, gain)
            del trades[symbol]
            return

        if price < stop or gain <= -1.5:
            log_trade(symbol, "SELL", price, gain)
            del trades[symbol]
            return

        trailing_stop_advanced(symbol, trades[symbol].get("last_price", trades[symbol]["entry"]))
        log_trade(symbol, "HOLD", trades[symbol]["entry"])

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
            await asyncio.gather(*(process_symbol_aggressive(s) for s in SYMBOLS if s not in trades))
            print("✔️ Itération terminée", flush=True)

        except Exception as e:
            await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Erreur : {e}")

        await asyncio.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_loop())
