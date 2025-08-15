import asyncio
import requests
import numpy as np
from datetime import datetime, timezone, timedelta
from telegram import Bot
import nest_asyncio
import traceback
import csv
import os, json
# [#imports-retry]
# [#imports-ratelimit]
import time
import threading
from telegram.error import RetryAfter, TimedOut, NetworkError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

nest_asyncio.apply()

# [#http-session]
REQUEST_TIMEOUT = (5, 15)  # (connexion, lecture) en secondes

SESSION = requests.Session()
_retry = Retry(
    total=5,                # 5 tentatives max
    backoff_factor=0.5,     # 0.5s, 1s, 2s, 4s...
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods = frozenset(["GET"]),  # ne retry que GET
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=100, pool_maxsize=100)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

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
ANTI_SPIKE_UP_STD = 1.0   # % max d'extension de la bougie d'entrée (stratégie standard)
ANTI_SPIKE_UP_AGR = 1.2   # % max d'extension (stratégie agressive)


bot = Bot(token=TELEGRAM_TOKEN)
trades = {}
history = []
market_cache = {}
last_trade_time = {}
LOG_FILE = "trade_log.csv" 
# --- Persistance des positions (survit aux redémarrages) ---
PERSIST_FILE = "trades_state.json"

def load_trades():
    try:
        with open(PERSIST_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_trades():
    try:
        # on ne sérialise que des types simples
        serializable = {}
        for sym, t in trades.items():
            d = dict(t)
            # tp_times contient des datetimes -> on peut les omettre ou str()
            if "tp_times" in d:
                d["tp_times"] = {k: str(v) for k, v in d["tp_times"].items()}
            serializable[sym] = d
        with open(PERSIST_FILE, "w") as f:
            json.dump(serializable, f)
    except Exception as e:
        print(f"⚠️ save_trades: {e}")


# === Cache par itération pour limiter les requêtes ===
symbol_cache = {}    # {"BTCUSDT": {"1h": [...], "4h": [...]}, ...}

def _delete_trade(symbol):
    if symbol in trades:
        del trades[symbol]
        save_trades()

def get_cached(symbol, tf="1h", limit=LIMIT):
    """Retourne les klines depuis le cache local de l'itération."""
    if symbol not in symbol_cache:
        symbol_cache[symbol] = {}
    if tf not in symbol_cache[symbol]:
        symbol_cache[symbol][tf] = get_klines(symbol, interval=tf, limit=limit)
    return symbol_cache[symbol][tf]


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
        
# [#volume-helpers]
def kline_vol_quote(k):  # k[7] = quote asset volume (ex: USDT)
    return float(k[7])

def volumes_series(klines, quote=True):
    return [kline_vol_quote(k) for k in klines] if quote else [float(k[5]) for k in klines]
# ====== /HELPERS ======

def safe_message(text):
    return text if len(text) < 4000 else text[:3900] + "\n... (tronqué)"

# === Anti-flood Telegram (1 msg/s + RetryAfter/Timeout) ===
_tg_lock = asyncio.Lock()
_tg_last_send_ts = 0.0  # horloge monotonic, ~1 msg/s par chat

async def tg_send(text: str, chat_id: int = CHAT_ID):
    """Envoi Telegram avec anti-flood + gestion RetryAfter/Timeout."""
    global _tg_last_send_ts
    async with _tg_lock:
        # 1) respecter ~1 msg / seconde
        now = time.monotonic()
        wait = max(0.0, 1.05 - (now - _tg_last_send_ts))  # petite marge
        if wait > 0:
            await asyncio.sleep(wait)

        # 2) envoi + reprises simples
        try:
            await bot.send_message(chat_id=chat_id, text=safe_message(text))
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            await bot.send_message(chat_id=chat_id, text=safe_message(text))
        except (TimedOut, NetworkError):
            await asyncio.sleep(2)
            await bot.send_message(chat_id=chat_id, text=safe_message(text))

        _tg_last_send_ts = time.monotonic()
# --- Envoi de fichier Telegram (anti-flood + retry) ---
async def tg_send_doc(path: str, caption: str = "", chat_id: int = CHAT_ID):
    import os
    global _tg_last_send_ts

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        await tg_send(f"ℹ️ Fichier introuvable ou vide: {path}")
        return

    async with _tg_lock:
        # respecter ~1 msg/s comme pour tg_send
        now = time.monotonic()
        wait = max(0.0, 1.05 - (now - _tg_last_send_ts))
        if wait > 0:
            await asyncio.sleep(wait)

        try:
            with open(path, "rb") as f:
                await bot.send_document(chat_id=chat_id, document=f, caption=safe_message(caption)[:1024])
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            with open(path, "rb") as f:
                await bot.send_document(chat_id=chat_id, document=f, caption=safe_message(caption)[:1024])
        except (TimedOut, NetworkError):
            await asyncio.sleep(2)
            with open(path, "rb") as f:
                await bot.send_document(chat_id=chat_id, document=f, caption=safe_message(caption)[:1024])

        _tg_last_send_ts = time.monotonic()

def get_klines(symbol, interval='1h', limit=100):
    url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    try:
        response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"❌ Erreur réseau get_klines({symbol}): {e}")
        return []

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

def ema_tv(prices, period):
    """EMA finale façon TradingView (dernière valeur de la série EMA récursive)."""
    if not prices:
        return 0.0
    return float(compute_ema_series(prices, period)[-1])

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
    volumes = volumes_series(klines, quote=True)
    return np.mean(volumes[-5:]) > np.mean(volumes[-10:-5]) and np.mean(volumes[-5:]) > MIN_VOLUME

def is_market_bullish():
    try:
        btc_prices = [float(k[4]) for k in market_cache.get('BTCUSDT', [])]
        eth_prices = [float(k[4]) for k in market_cache.get('ETHUSDT', [])]
        if not btc_prices or not eth_prices:
            return False
        return is_uptrend(btc_prices) and is_uptrend(eth_prices)
    except Exception:
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

def compute_supertrend(klines, period=10, multiplier=3):
    atr = atr_tv(klines, period)          # <- au lieu de compute_atr
    highs  = np.array([float(k[2]) for k in klines])
    lows   = np.array([float(k[3]) for k in klines])
    closes = np.array([float(k[4]) for k in klines])
    hl2 = (highs + lows) / 2
    lowerband = hl2[-1] - multiplier * atr
    return closes[-1] > lowerband

# ====== Versions "TradingView-like" (RMA/Wilder) ======

# [#rma-nansafe]
def _rma(values, period):
    """
    Wilder's RMA version NaN-safe:
      - Seed = moyenne SANS NaN sur la 1ère fenêtre 'period'
      - Si value[i] est NaN, on réutilise r[i-1] (carry-forward)
    """
    v = np.asarray(values, dtype=float)

    if len(v) < period:
        return np.array([])

    r = np.empty_like(v)
    r[:] = np.nan

    # seed sur la 1ère fenêtre, sans NaN
    first = v[:period]
    seed = np.nanmean(first)

    if np.isnan(seed):
        # si la 1ère fenêtre est toute NaN, on glisse jusqu’à trouver une fenêtre valide
        found = False
        for start in range(0, len(v) - period + 1):
            win = v[start:start + period]
            m = np.nanmean(win)
            if not np.isnan(m):
                r[start + period - 1] = m
                # itération à partir de cette seed
                for i in range(start + period, len(v)):
                    val = v[i]
                    prev = r[i - 1]
                    if np.isnan(prev):
                        prev = m
                    if np.isnan(val):
                        r[i] = prev
                    else:
                        r[i] = (prev * (period - 1) + val) / period
                found = True
                break
        if not found:
            return r  # tout NaN → on renvoie NaN
        return r

    # seed standard
    r[period - 1] = seed
    for i in range(period, len(v)):
        val = v[i]
        prev = r[i - 1]
        if np.isnan(val):
            r[i] = prev  # carry-forward si NaN
        else:
            r[i] = (prev * (period - 1) + val) / period
    return r


def rsi_tv(closes, period=14):
    """RSI version TV (gains/pertes lissés avec RMA)."""
    c = np.asarray(closes, dtype=float)
    if len(c) < period+1:
        return 50.0
    deltas = np.diff(c)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = _rma(gains, period)
    avg_loss = _rma(losses, period)
    rs = avg_gain / np.where(avg_loss == 0, np.nan, avg_loss)
    rsi_series = 100.0 - (100.0 / (1.0 + rs))
    # Prend la dernière valeur finie, sinon 50
    last = rsi_series[~np.isnan(rsi_series)]
    return float(last[-1]) if len(last) else 50.0


def rsi_tv_series(closes, period=14):
    c = np.asarray(closes, dtype=float)
    if len(c) < period + 1:
        return np.full(len(c), np.nan)

    deltas = np.diff(c)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = _rma(gains, period)
    avg_loss = _rma(losses, period)

    rs = avg_gain / np.where(avg_loss == 0, np.nan, avg_loss)
    rsi_vals = 100.0 - (100.0 / (1.0 + rs))

    # réaligne sur la longueur des closes
    rsi_full = np.full(len(c), np.nan)
    rsi_full[1:] = rsi_vals
    return rsi_full

def atr_tv(klines, period=14):
    """ATR version TV: TR lissé par RMA (Wilder)."""
    highs = np.array([float(k[2]) for k in klines], dtype=float)
    lows  = np.array([float(k[3]) for k in klines], dtype=float)
    closes= np.array([float(k[4]) for k in klines], dtype=float)
    if len(closes) < period+1:
        return 0.0
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum(highs - lows, np.maximum(abs(highs - prev_close), abs(lows - prev_close)))
    r = _rma(tr[1:], period)  # on skip le tout premier TR (alignement TV)
    last = r[~np.isnan(r)]
    return float(last[-1]) if len(last) else 0.0

# [#adx-tv-nansafe]
def adx_tv(klines, period=14):
    """ADX version TV (Wilder/RMA) NaN-safe."""
    highs  = np.array([float(k[2]) for k in klines], dtype=float)
    lows   = np.array([float(k[3]) for k in klines], dtype=float)
    closes = np.array([float(k[4]) for k in klines], dtype=float)
    n = len(closes)
    if n < period + 1:
        return 0.0

    up_move   = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]
    plus_dm   = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    prev_close = closes[:-1]
    tr = np.maximum(highs[1:] - lows[1:],
                    np.maximum(np.abs(highs[1:] - prev_close), np.abs(lows[1:] - prev_close)))

    atr = _rma(tr, period)
    atr_nozero = np.where(atr == 0, np.nan, atr)

    pdi = 100.0 * (_rma(plus_dm, period) / atr_nozero)
    mdi = 100.0 * (_rma(minus_dm, period) / atr_nozero)

    denom = pdi + mdi
    denom = np.where(denom == 0, np.nan, denom)
    dx = 100.0 * (np.abs(pdi - mdi) / denom)

    adx_series = _rma(dx, period)
    finite = adx_series[~np.isnan(adx_series)]
    return float(finite[-1]) if finite.size else 0.0


# [#fast-exit-5m]
def _last_two_finite(values):
    arr = np.asarray(values, dtype=float)
    finite = arr[~np.isnan(arr)]
    if finite.size >= 2:
        return float(finite[-2]), float(finite[-1])
    return float('nan'), float('nan')

def fast_exit_5m_trigger(symbol: str, entry: float, current_price: float):
    """
    Sortie dynamique (timeframe 5m) :
    - Si gain >= +1% ET (RSI(5m) chute > 5 pts OU MACD(5m) croise à la baisse) -> True
    Retourne (trigger: bool, info: dict)
    """
    try:
        if entry <= 0 or current_price is None:
            return False, {}
        gain_pct = ((current_price - entry) / entry) * 100.0
        if gain_pct < 1.0:
            return False, {"gain_pct": gain_pct}

        k5 = get_cached(symbol, '5m', limit=60)
        if not k5 or len(k5) < 20:
            return False, {"gain_pct": gain_pct}

        closes5 = [float(k[4]) for k in k5]

        # RSI(5m) : chute entre les 2 dernières clôtures
        rsi5_series = rsi_tv_series(closes5, period=14)
        rsi_prev, rsi_now = _last_two_finite(rsi5_series)
        rsi_drop = (not np.isnan(rsi_prev) and not np.isnan(rsi_now) and (rsi_prev - rsi_now) > 5.0)

        # MACD(5m) : croisement baissier récent
        macd_now,  signal_now  = compute_macd(closes5)
        macd_prev, signal_prev = compute_macd(closes5[:-1])
        macd_cross_down = (macd_prev >= signal_prev) and (macd_now < signal_now)

        trigger = gain_pct >= 1.0 and (rsi_drop or macd_cross_down)
        return trigger, {
            "gain_pct": gain_pct,
            "rsi5_prev": rsi_prev, "rsi5_now": rsi_now,
            "rsi_drop": (rsi_prev - rsi_now) if (not np.isnan(rsi_prev) and not np.isnan(rsi_now)) else None,
            "macd5": macd_now, "signal5": signal_now,
            "macd5_prev": macd_prev, "signal5_prev": signal_prev,
            "macd_cross_down": macd_cross_down
        }
    except Exception:
        return False, {}    


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

# ====== CSV refus d'entrée (diagnostic) ======
REFUSAL_LOG_FILE = "refusal_log.csv"
REFUSAL_FIELDS = ["ts_utc", "symbol", "reason"]

def log_refusal(symbol: str, reason: str):
    """Append une ligne dans refusal_log.csv (diagnostic des refus)."""
    import os, csv
    header_needed = (not os.path.exists(REFUSAL_LOG_FILE)
                     or os.path.getsize(REFUSAL_LOG_FILE) == 0)
    with open(REFUSAL_LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REFUSAL_FIELDS)
        if header_needed:
            w.writeheader()
        w.writerow({
            "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "reason": reason,
        })

def log_trade_csv(row: dict):
    """Écrit/append une ligne dans trade_audit.csv avec l'en-tête s'il manque."""
    import os, csv
    header_needed = (not os.path.exists(CSV_AUDIT_FILE)
                     or os.path.getsize(CSV_AUDIT_FILE) == 0)
    with open(CSV_AUDIT_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_AUDIT_FIELDS)
        if header_needed:
            w.writeheader()
        clean = {k: row.get(k, "") for k in CSV_AUDIT_FIELDS}
        # si l'appelant n'a pas fourni ts_utc, on le remplit proprement en UTC
        if not clean.get("ts_utc"):
            clean["ts_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        w.writerow(clean)
# ====== /CSV détaillé ======

def trailing_stop_advanced(symbol, current_price):
    if symbol not in trades:
        return
    entry = trades[symbol]['entry']
    gain = ((current_price - entry) / entry) * 100
    atr_val = compute_atr(get_cached(symbol, '1h'))
    prev_stop = trades[symbol].get("stop", entry - 0.6 * atr_val)

    new_stop = prev_stop
    if gain > 2:
        new_stop = max(new_stop, current_price - 0.7 * atr_val)
    if gain > 4:
        new_stop = max(new_stop, current_price - 0.5 * atr_val)
    if gain > 7:
        new_stop = max(new_stop, current_price - 0.3 * atr_val)

    if new_stop != prev_stop:
        trades[symbol]["stop"] = new_stop
        save_trades()
        
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
    url = "https://api.binance.com/api/v3/ticker/price"
    try:
        resp = SESSION.get(url, params={"symbol": symbol}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except requests.RequestException as e:
        print(f"⚠️ Erreur réseau get_last_price({symbol}): {e}")
        return None

async def process_symbol(symbol):
    try:
        # --- Auto-close après 12h si une position existe ---
        if symbol in trades:
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            elapsed_time = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            if elapsed_time > 12:
                entry = trades[symbol]['entry']
                price = get_last_price(symbol)
                if price is None:
                    log_refusal(symbol, "API prix indisponible")
                    return
                pnl = ((price - entry) / entry) * 100
                trade_id = trades[symbol].get("trade_id", make_trade_id(symbol))

                # message + log (on utilise maintenant tg_send au lieu de bot.send_message)
                msg = format_autoclose_msg(symbol, trade_id, price, pnl)
                await tg_send(msg)

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
                    "sl_initial": trades[symbol].get("sl_initial", ""),
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
                    "reason_entry": trades[symbol].get("reason_entry", ""),
                    "reason_exit": "timeout > 12h"
                })

                log_trade(symbol, "SELL", price, pnl)
                _delete_trade(symbol)
                return

        # ---------- Analyse standard ----------
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 Analyse de {symbol}", flush=True)

        klines = get_cached(symbol, '1h')# 1h
        if not klines or len(klines) < 50:
            log_refusal(symbol, "Données 1h insuffisantes")
            return
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        volumes = volumes_series(klines, quote=True)
        price = get_last_price(symbol)
        if price is None:
            log_refusal(symbol, "API prix indisponible")
            return
        # --- Indicateurs (versions TradingView) ---
        rsi = rsi_tv(closes, period=14)
        rsi_series = rsi_tv_series(closes, period=14)
        macd, signal = compute_macd(closes)      # MACD EMA/EMA
        ema200 = ema_tv(closes, 200)
        atr = atr_tv(klines, period=14)
        adx_value = adx_tv(klines, period=14)
        ema25 = ema_tv(closes, 25)

        # --- 4h ---
        klines_4h = get_cached(symbol, '4h')
        if not klines_4h or len(klines_4h) < 50:
            log_refusal(symbol, "Données 4h insuffisantes")
            return
        closes_4h = [float(k[4]) for k in klines_4h]
        ema200_4h = ema_tv(closes_4h, 200)
        ema50_4h  = ema_tv(closes_4h, 50)
        rsi_4h    = rsi_tv(closes_4h, period=14)

        # Contexte marché via cache
        btc_up = is_uptrend([float(k[4]) for k in market_cache.get('BTCUSDT', [])]) if market_cache.get('BTCUSDT') else False
        eth_up = is_uptrend([float(k[4]) for k in market_cache.get('ETHUSDT', [])]) if market_cache.get('ETHUSDT') else False

        if not is_market_bullish():
            log_refusal(symbol, "Marché global baissier (BTC/ETH pas en uptrend)")
            return
        # Filtres de tendance avec log    
        if price < ema200:
           log_refusal(symbol, "Prix < EMA200 (1h)")
           return
        if closes_4h[-1] < ema200_4h:
           log_refusal(symbol, "Clôture 4h < EMA200(4h)")
           return
        if closes_4h[-1] < ema50_4h:
           log_refusal(symbol, "Clôture 4h < EMA50(4h)")
           return
        if rsi_4h < 50:
            log_refusal(symbol, f"RSI 4h {rsi_4h:.1f} < 50")
            return
        if is_market_range(closes_4h):
            await tg_send(f"⚠️ Marché en range sur {symbol} → Trade bloqué")
            return
        if detect_rsi_divergence(closes, rsi_series): return

        volatility = get_volatility(atr, price)
        if volatility < 0.005:
           log_refusal(symbol, f"Volatilité trop faible (ATR/price={volatility:.4f} < 0.005)")
           return

        supertrend_signal = compute_supertrend(klines)
        if adx_value < 20:
           log_refusal(symbol, f"ADX 1h trop faible (adx={adx_value:.1f} < 20)")
           return

        if not supertrend_signal:
           log_refusal(symbol, "Supertrend 1h non haussier (signal=False)")
           return

        if symbol in last_trade_time:
            cooldown_left = COOLDOWN_HOURS - (datetime.now() - last_trade_time[symbol]).total_seconds() / 3600
            if cooldown_left > 0:
                log_refusal(symbol, f"Cooldown actif ({cooldown_left:.2f}h restantes)")
                return

        if len(trades) >= MAX_TRADES:
            log_refusal(symbol, f"Nombre max de trades atteint ({len(trades)}/{MAX_TRADES})")
            return

        if not in_active_session():
            log_refusal(symbol, "Hors plage horaire de trading")
            return

        if price > ema25 * 1.02:
           log_refusal(symbol, f"Prix trop éloigné de EMA25 (> +2%) (prix={price}, ema25={ema25})")
           return

        # [#anti-spike-standard]
        open_now = float(klines[-1][1])
        high_now = float(klines[-1][2])
        # on prend le pire des deux: extension jusqu'au plus haut OU jusqu'au prix actuel
        spike_up_pct = ((max(high_now, price) - open_now) / max(open_now, 1e-9)) * 100.0
        if spike_up_pct > ANTI_SPIKE_UP_STD:
            log_refusal(symbol, f"Anti-spike: bougie 1h déjà +{spike_up_pct:.2f}% > {ANTI_SPIKE_UP_STD:.1f}%")
            return

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
        if last3_change > 0.022:
            brk_ok = False

        # raisons (on remplit si on a un setup)
        reasons = []
        if brk_ok and trend_ok and momentum_ok and volume_ok:
            buy = True
            label = "⚡ Breakout + Retest validé (1h) + Confluence"
            position_pct = 7
            reasons = [label, f"ADX {adx_value:.1f} >= 22", f"MACD {macd:.3f} > Signal {signal:.3f}"]
        elif trend_ok and momentum_ok and volume_ok:
            near_ema25 = price <= ema25 * 1.01
            candle_ok = (abs(highs[-1] - lows[-1]) / max(lows[-1], 1e-9)) <= 0.03
            if near_ema25 and candle_ok:
                buy = True
                label = "✅ Pullback EMA25 propre + Confluence"
                position_pct = 6
                reasons = [label, f"ADX {adx_value:.1f} >= 22", f"MACD {macd:.3f} > Signal {signal:.3f}"]

        # === GESTION TP / HOLD / SELL ===
        sell = False
        if symbol in trades:
            entry = trades[symbol]['entry']
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            elapsed_time = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            gain = ((price - entry) / entry) * 100
            stop = trades[symbol].get("stop", entry - 0.6 * atr)
            # [#fast-exit-5m-check-standard]
            # — Sortie dynamique rapide (5m) —
            triggered, fx = fast_exit_5m_trigger(symbol, entry, price)
            if triggered:
                vol5 = float(np.mean(volumes[-5:]))
                vol20 = float(np.mean(volumes[-20:]))
                reason_bits = []
                if fx.get("rsi_drop") is not None and fx["rsi_drop"] > 5:
                    reason_bits.append(f"RSI(5m) -{fx['rsi_drop']:.1f} pts")
                if fx.get("macd_cross_down"):
                    reason_bits.append("MACD(5m) croisement baissier")
                raison = "Sortie dynamique 5m: gain ≥ +1% ; " + " + ".join(reason_bits)

                msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, stop, elapsed_time, raison)
                await tg_send(msg)

                log_trade_csv({
                    "trade_id": trades[symbol]["trade_id"],
                    "symbol": symbol,
                    "event": "DYN_EXIT_5M",
                    "strategy": "standard",
                    "version": BOT_VERSION,
                    "entry": entry,
                    "exit": price,
                    "price": price,
                    "pnl_pct": gain,
                    "position_pct": trades[symbol].get("position_pct", ""),
                    "sl_initial": trades[symbol].get("sl_initial", entry - 0.6 * atr),
                    "sl_final": stop,
                    "atr_1h": atr, "atr_mult_at_entry": 0.6,
                    "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                    "supertrend_on": supertrend_signal,
                    "ema25_1h": ema25, "ema200_1h": ema200, "ema50_4h": ema50_4h, "ema200_4h": ema200_4h,
                    "vol_ma5": vol5, "vol_ma20": vol20, "vol_ratio": vol5 / max(vol20, 1e-9),
                    "btc_uptrend": btc_up, "eth_uptrend": eth_up,
                    "reason_entry": trades[symbol].get("reason_entry", ""),
                    "reason_exit": raison
                })

                log_trade(symbol, "SELL", price, gain)
                _delete_trade(symbol)
                return

            if volatility < 0.008:
                stop = max(stop, price - atr * 0.5)
            elif volatility < 0.015:
                stop = max(stop, price - atr * 0.8)
            else:
                stop = max(stop, price - atr * 1.2)

            if rsi < 45 or macd < signal:
                msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, stop, elapsed_time, "RSI bas ou MACD croisé à la baisse")
                await tg_send(msg)

                # LOG CSV SELL (sortie technique) standard
                log_trade_csv({
                    "ts_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_id": trades[symbol]["trade_id"],
                    "symbol": symbol,
                    "event": "SELL",
                    "strategy": "standard",
                    "version": BOT_VERSION,
                    "entry": entry,
                    "exit": price,
                    "price": price,
                    "pnl_pct": gain,
                    "position_pct": trades[symbol].get("position_pct", position_pct),
                    "sl_initial": trades[symbol].get("sl_initial", entry - 0.6 * atr),
                    "sl_final": stop,
                    "atr_1h": atr,
                    "atr_mult_at_entry": 0.6,
                    "rsi_1h": rsi,
                    "macd": macd,
                    "signal": signal,
                    "adx_1h": adx_value,
                    "supertrend_on": supertrend_signal,
                    "ema25_1h": ema25,
                    "ema200_1h": ema200,
                    "ema50_4h": ema50_4h,
                    "ema200_4h": ema200_4h,
                    "vol_ma5": np.mean(volumes[-5:]),
                    "vol_ma20": np.mean(volumes[-20:]),
                    "vol_ratio": np.mean(volumes[-5:]) / max(np.mean(volumes[-20:]), 1e-9),
                    "btc_uptrend": btc_up,
                    "eth_uptrend": eth_up,
                    "reason_entry": trades[symbol].get("reason_entry", ""),
                    "reason_exit": "RSI bas ou MACD croisé à la baisse"
                })

                log_trade(symbol, "SELL", price, gain)
                _delete_trade(symbol)
                return

            # TP progressifs
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
                        save_trades()

                        msg = format_tp_msg(
                            tp_num, symbol, trades[symbol]["trade_id"], price, gain,
                            trades[symbol]["stop"], ((trades[symbol]["stop"] - entry) / entry) * 100,
                            elapsed_time, "Stop ajusté"
                        )
                        await tg_send(msg)

                        # LOG CSV TP standard
                        log_trade_csv({
                            "ts_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                            "trade_id": trades[symbol]["trade_id"],
                            "symbol": symbol,
                            "event": f"TP{tp_num}",
                            "strategy": "standard",
                            "version": BOT_VERSION,
                            "entry": entry,
                            "exit": "",
                            "price": price,
                            "pnl_pct": gain,
                            "position_pct": trades[symbol].get("position_pct", position_pct),
                            "sl_initial": trades[symbol].get("sl_initial", entry - 0.6 * atr),
                            "sl_final": trades[symbol]["stop"],
                            "atr_1h": atr,
                            "atr_mult_at_entry": 0.6,
                            "rsi_1h": rsi,
                            "macd": macd,
                            "signal": signal,
                            "adx_1h": adx_value,
                            "supertrend_on": supertrend_signal,
                            "ema25_1h": ema25,
                            "ema200_1h": ema200,
                            "ema50_4h": ema50_4h,
                            "ema200_4h": ema200_4h,
                            "vol_ma5": np.mean(volumes[-5:]),
                            "vol_ma20": np.mean(volumes[-20:]),
                            "vol_ratio": np.mean(volumes[-5:]) / max(np.mean(volumes[-20:]), 1e-9),
                            "btc_uptrend": btc_up,
                            "eth_uptrend": eth_up,
                            "reason_entry": trades[symbol].get("reason_entry", ""),
                            "reason_exit": ""
                        })

            if trades[symbol].get("tp1", False) and gain < 1:
                sell = True
            if price < trades[symbol]["stop"] or gain <= -1.5:
                msg = format_stop_msg(symbol, trades[symbol]["trade_id"], trades[symbol]["stop"], gain, rsi, adx_value, np.mean(volumes[-5:]) / max(np.mean(volumes[-20:]), 1e-9))
                await tg_send(msg)
                sell = True

                # LOG CSV STOP/SELL standard
                event_name = "STOP" if price < trades[symbol]["stop"] else "SELL"
                log_trade_csv({
                    "ts_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_id": trades[symbol]["trade_id"],
                    "symbol": symbol,
                    "event": event_name,
                    "strategy": "standard",
                    "version": BOT_VERSION,
                    "entry": entry,
                    "exit": price,
                    "price": price,
                    "pnl_pct": gain,
                    "position_pct": trades[symbol].get("position_pct", position_pct),
                    "sl_initial": trades[symbol].get("sl_initial", entry - 0.6 * atr),
                    "sl_final": trades[symbol]["stop"],
                    "atr_1h": atr,
                    "atr_mult_at_entry": 0.6,
                    "rsi_1h": rsi,
                    "macd": macd,
                    "signal": signal,
                    "adx_1h": adx_value,
                    "supertrend_on": supertrend_signal,
                    "ema25_1h": ema25,
                    "ema200_1h": ema200,
                    "ema50_4h": ema50_4h,
                    "ema200_4h": ema200_4h,
                    "vol_ma5": np.mean(volumes[-5:]),
                    "vol_ma20": np.mean(volumes[-20:]),
                    "vol_ratio": np.mean(volumes[-5:]) / max(np.mean(volumes[-20:]), 1e-9),
                    "btc_uptrend": btc_up,
                    "eth_uptrend": eth_up,
                    "reason_entry": trades[symbol].get("reason_entry", ""),
                    "reason_exit": "Stop touché" if event_name == "STOP" else "Perte max (-1.5%)"
                })

            if not sell:
                log_trade(symbol, "HOLD", price)

        # --- Entrée (BUY) ---
        if buy and symbol not in trades:
            trade_id = make_trade_id(symbol)
            sl_initial = price - 0.6 * atr

            trades[symbol] = {
                "entry": price,
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "confidence": confidence,
                "stop": sl_initial,
                "position_pct": position_pct,
                "trade_id": trade_id,
                "tp_times": {},
                "sl_initial": sl_initial,
                "reason_entry": "; ".join(reasons) if reasons else ""
            }
            last_trade_time[symbol] = datetime.now()
            save_trades()   # ⬅️ persiste la nouvelle position

            msg = format_entry_msg(
                symbol, trade_id, "standard", BOT_VERSION, price, position_pct,
                sl_initial, ((price - sl_initial) / price) * 100, atr,
                rsi, macd, signal, adx_value, supertrend_signal,
                ema25, ema50_4h, ema200, ema200_4h,
                np.mean(volumes[-5:]), np.mean(volumes[-20:]),
                np.mean(volumes[-5:]) / max(np.mean(volumes[-20:]), 1e-9),
                btc_up, eth_up,
                confidence, label_conf, reasons
            )
            await tg_send(msg)
            log_trade(symbol, "BUY", price)

        elif sell and symbol in trades:
            entry = trades[symbol]['entry']
            gain = ((price - entry) / entry) * 100
            stop_used = trades[symbol].get("stop", entry - 0.6 * atr)
            await tg_send(f"🔴 Vente {symbol} à {price:.4f} | Gain {gain:.2f}% | Stop final: {stop_used:.4f}")
            log_trade(symbol, "SELL", price, gain)
            _delete_trade(symbol)

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
                if price is None:
                    log_refusal(symbol, "API prix indisponible")
                    return               
                pnl = ((price - entry) / entry) * 100
                trade_id = trades[symbol].get("trade_id", make_trade_id(symbol))
                # Message auto-close
                msg = format_autoclose_msg(symbol, trade_id, price, pnl)
                await tg_send(msg)

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
                _delete_trade(symbol)
                return

        # ---- Analyse agressive ----
        klines = get_cached(symbol, '1h')
        if not klines or len(klines) < 50:
            log_refusal(symbol, "Données 1h insuffisantes")
            return
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        volumes = volumes_series(klines, quote=True)
        price = get_last_price(symbol)
        if price is None:
            log_refusal(symbol, "API prix indisponible")
            return
        # ---- Indicateurs (versions TradingView) ----
        rsi = rsi_tv(closes, period=14)
        macd, signal = compute_macd(closes)
        ema200 = ema_tv(closes, 200)
        atr = atr_tv(klines, period=14)
        adx_value = adx_tv(klines, period=14)
        ema25 = ema_tv(closes, 25)
        # pour comparer l'élan MACD vs bougie précédente
        macd_prev, signal_prev = compute_macd(closes[:-1])

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

        # ---- Conditions confluence ----
        supertrend_ok = compute_supertrend(klines)     # basé sur atr_tv
        above_ema200  = price >= ema200 * 0.98                # ema200 calculé plus haut

        vol5   = np.mean(volumes[-5:])
        vol20  = np.mean(volumes[-20:])
        volume_ok = (vol5 > vol20 * 1.1) and (vol5 > MIN_VOLUME)  # assoupli, en USDT


        if not (supertrend_ok and adx_value >= 18 and above_ema200 and volume_ok):
            return


        # Momentum MACD vs bougie précédente
        if not (macd > signal and (macd - signal) > (macd_prev - signal_prev)):
            return

        # RSI en zone constructive
        if not (52 <= rsi < 82):
            return

        # ---- Confluence 4h ----
        k4 = get_cached(symbol, '4h')
        if not k4 or len(k4) < 50:
            log_refusal(symbol, "Données 4h insuffisantes")
            return
            
        c4 = [float(k[4]) for k in k4]
        ema50_4h = ema_tv(c4, 50)
        ema200_4h = ema_tv(c4, 200)
        if c4[-1] < ema50_4h or ema50_4h < ema200_4h:
            return

        # ----- Breakout + Retest (UNIQUE) -----
        last10_high = max(highs[-10:])
        retest_tolerance = 0.005  # ±0.5%

        # breakout : prix au-dessus du plus haut des 10 dernières bougies (avec marge)
        breakout = price > last10_high * 1.004
        if not breakout:
            log_refusal(symbol, f"Pas de breakout (prix={price}, plus_haut10j={last10_high})")
            return

        # éviter un mouvement déjà trop étendu sur les 3 dernières bougies
        last3_change = (closes[-1] - closes[-4]) / closes[-4] if len(closes) >= 4 else 0
        if last3_change > 0.022:
            log_refusal(symbol, f"Mouvement 3 bougies trop fort (+{last3_change*100:.2f}%)")
            return

        # retest valide = proche du niveau de breakout OU proche de l'EMA25
        near_level = abs(price - last10_high) / last10_high <= retest_tolerance
        near_ema25 = abs(price - ema25) / ema25 <= retest_tolerance

        # éviter d’acheter trop loin de l’EMA25
        too_far_from_ema25 = price >= ema25 * 1.03
        if too_far_from_ema25:
            log_refusal(symbol, f"Prix trop éloigné de l'EMA25 (+2%) (prix={price}, ema25={ema25})")
            return

        # si le prix n'est proche ni du niveau de breakout ni de l'EMA25
        if not (near_level or near_ema25):
            log_refusal(symbol, "Pas de retest valide (ni proche breakout, ni proche EMA25)")
            return

        # [#anti-spike-aggressive]
        open_now  = float(klines[-1][1])  # open de la bougie 1h en cours
        high_now  = float(klines[-1][2])  # high de la bougie 1h en cours
        # on prend le pire des deux: le plus haut "officiel" OU le prix courant si encore plus haut
        spike_up_pct = ((max(high_now, price) - open_now) / max(open_now, 1e-9)) * 100.0
        if spike_up_pct > ANTI_SPIKE_UP_AGR:
            log_refusal(symbol, f"Anti-spike (aggressive): bougie 1h déjà +{spike_up_pct:.2f}% > {ANTI_SPIKE_UP_AGR:.1f}%")
            return


        # ---- Indicateurs ----
        # ---- Scoring + raisons (en réutilisant les variables TV déjà calculées) ----
        indicators = {
            "rsi": rsi,
            "macd": macd,
            "signal": signal,
            "supertrend": supertrend_ok,
            "adx": adx_value,
            "volume_ok": volume_ok,
            "above_ema200": above_ema200,
        }
        score = compute_confidence_score(indicators)
        label_conf = label_confidence(score)
        if score < 7:
            return

        reasons = [
            "Breakout+Retest validé",
            f"ADX {adx_value:.1f} >= 22",
            f"MACD {macd:.3f} > Signal {signal:.3f}",
        ]
        
        # ---- Entrée ----
        atr_val = atr_tv(klines)                # ATR version TV
        ema200_1h = ema200                      # on a déjà ema200 (1h)
        trade_id = make_trade_id(symbol)

        trades[symbol] = {
            "entry": price,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "confidence": score,
            "stop": price - 0.6 * atr_val,
            "position_pct": 5,
            "trade_id": trade_id,
            "tp_times": {},
            "sl_initial": price - 0.6 * atr_val,
            "reason_entry": "; ".join(reasons),
        }
        last_trade_time[symbol] = datetime.now()
        save_trades()

        # uptrends via cache préchargé (pas de requêtes)
        btc_up = is_uptrend([float(k[4]) for k in market_cache.get("BTCUSDT", [])]) if market_cache.get("BTCUSDT") else False
        eth_up = is_uptrend([float(k[4]) for k in market_cache.get("ETHUSDT", [])]) if market_cache.get("ETHUSDT") else False

        msg = format_entry_msg(
            symbol, trade_id, "aggressive", BOT_VERSION, price, 5,
            trades[symbol]["sl_initial"], ((price - trades[symbol]["sl_initial"]) / price) * 100, atr_val,
            rsi, macd, signal, adx_value,
            supertrend_ok, ema25, ema50_4h, ema200_1h, ema200_4h,
            vol5, vol20, vol5 / max(vol20, 1e-9),
            btc_up, eth_up,
            score, label_conf, reasons
        )
        await tg_send(msg)

        # Logging CSV (BUY) COMPLET
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
            "sl_initial": trades[symbol]["sl_initial"],
            "sl_final": "",
            "atr_1h": atr_val,
            "atr_mult_at_entry": 0.6,
            "rsi_1h": rsi,
            "macd": macd,
            "signal": signal,
            "adx_1h": adx_value,
            "supertrend_on": supertrend_ok,
            "ema25_1h": ema25,
            "ema200_1h": ema200_1h,
            "ema50_4h": ema50_4h,
            "ema200_4h": ema200_4h,
            "vol_ma5": vol5,
            "vol_ma20": vol20,
            "vol_ratio": vol5 / max(vol20, 1e-9),
            "btc_uptrend": btc_up,
            "eth_uptrend": eth_up,
            "reason_entry": "; ".join(reasons),
            "reason_exit": ""
        })
        log_trade(symbol, "BUY", price)


        # ---- Gestion TP / HOLD / SELL ----
        # === TP progressifs avec messages formatés + logging ===
        # métriques courantes + fallback ATR pour le stop si absent
        atr_val_current = atr_tv(klines)

        entry = trades[symbol]["entry"]
        gain  = ((price - entry) / entry) * 100
        stop  = trades[symbol].get("stop", entry - 0.6 * atr_val_current)
        elapsed_time = (
            datetime.now(timezone.utc)
            - datetime.strptime(trades[symbol]["time"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        ).total_seconds() / 3600

        # tendances BTC/ETH via le cache préchargé (pas de requêtes)
        btc_up = is_uptrend([float(k[4]) for k in market_cache.get("BTCUSDT", [])]) if market_cache.get("BTCUSDT") else False
        eth_up = is_uptrend([float(k[4]) for k in market_cache.get("ETHUSDT", [])]) if market_cache.get("ETHUSDT") else False

        # [#fast-exit-5m-check-aggressive]
        # — Sortie dynamique rapide (5m) —
        triggered, fx = fast_exit_5m_trigger(symbol, entry, price)
        if triggered:
            vol5_local = float(np.mean(volumes[-5:]))
            vol20_local = float(np.mean(volumes[-20:]))
            reason_bits = []
            if fx.get("rsi_drop") is not None and fx["rsi_drop"] > 5:
                reason_bits.append(f"RSI(5m) -{fx['rsi_drop']:.1f} pts")
            if fx.get("macd_cross_down"):
                reason_bits.append("MACD(5m) croisement baissier")
            raison = "Sortie dynamique 5m: gain ≥ +1% ; " + " + ".join(reason_bits)

            msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, stop, elapsed_time, raison)
            await tg_send(msg)

            log_trade_csv({
                "trade_id": trades[symbol]["trade_id"],
                "symbol": symbol,
                "event": "DYN_EXIT_5M",
                "strategy": "aggressive",
                "version": BOT_VERSION,
                "entry": entry,
                "exit": price,
                "price": price,
                "pnl_pct": gain,
                "position_pct": trades[symbol]["position_pct"],
                "sl_initial": trades[symbol]["sl_initial"],
                "sl_final": trades[symbol]["stop"],
                "atr_1h": atr_val_current, "atr_mult_at_entry": 0.6,
                "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                "supertrend_on": supertrend_ok,
                "ema25_1h": ema25, "ema200_1h": ema200_1h, "ema50_4h": ema50_4h, "ema200_4h": ema200_4h,
                "vol_ma5": vol5_local, "vol_ma20": vol20_local, "vol_ratio": vol5_local / max(vol20_local, 1e-9),
                "btc_uptrend": btc_up, "eth_uptrend": eth_up,
                "reason_entry": trades[symbol]["reason_entry"],
                "reason_exit": raison
            })

            log_trade(symbol, "SELL", price, gain)
            _delete_trade(symbol)
            return


        # ---- TP1 (≥ +1.5%) ----
        if gain >= 1.5 and not trades[symbol].get("tp1", False):
            trades[symbol]["tp1"] = True
            trades[symbol]["tp_times"]["tp1"] = datetime.now()
            # option : remonter le stop au prix d'entrée
            trades[symbol]["stop"] = max(stop, entry)
            save_trades()

            msg = format_tp_msg(
                1, symbol, trades[symbol]["trade_id"], price, gain,
                trades[symbol]["stop"], ((trades[symbol]["stop"] - entry) / entry) * 100,
                elapsed_time, "Stop >= prix d'entrée"
            )
            await tg_send(msg)

            log_trade_csv({
                "ts_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "trade_id": trades[symbol]["trade_id"],
                "symbol": symbol,
                "event": "TP1",
                "strategy": "aggressive",
                "version": BOT_VERSION,
                "entry": entry, "exit": "", "price": price, "pnl_pct": gain,
                "position_pct": trades[symbol]["position_pct"],
                "sl_initial": trades[symbol]["sl_initial"],
                "sl_final": trades[symbol]["stop"],
                "atr_1h": atr_val_current, "atr_mult_at_entry": 0.6,
                "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                "supertrend_on": supertrend_ok, "ema25_1h": ema25, "ema200_1h": ema200_1h,
                "ema50_4h": ema50_4h, "ema200_4h": ema200_4h,
                "vol_ma5": vol5, "vol_ma20": vol20, "vol_ratio": vol5 / max(vol20, 1e-9),
                "btc_uptrend": btc_up, "eth_uptrend": eth_up,
                "reason_entry": trades[symbol]["reason_entry"], "reason_exit": ""
            })

        # ---- TP2 (≥ +3.0%) ----
        if gain >= 3.0 and not trades[symbol].get("tp2", False):
            last_tp1_time = trades[symbol]["tp_times"].get("tp1")
            if not last_tp1_time or (datetime.now() - last_tp1_time).total_seconds() >= 120:
                trades[symbol]["tp2"] = True
                trades[symbol]["tp_times"]["tp2"] = datetime.now()
                # stop > entrée (+1.5%)
                trades[symbol]["stop"] = max(trades[symbol]["stop"], entry * 1.015)
                save_trades()

                msg = format_tp_msg(
                    2, symbol, trades[symbol]["trade_id"], price, gain,
                    trades[symbol]["stop"], ((trades[symbol]["stop"] - entry) / entry) * 100,
                    elapsed_time, "Stop > entrée (+1.5%)"
                )
                await tg_send(msg)

                log_trade_csv({
                    "ts_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_id": trades[symbol]["trade_id"],
                    "symbol": symbol,
                    "event": "TP2",
                    "strategy": "aggressive",
                    "version": BOT_VERSION,
                    "entry": entry, "exit": "", "price": price, "pnl_pct": gain,
                    "position_pct": trades[symbol]["position_pct"],
                    "sl_initial": trades[symbol]["sl_initial"],
                    "sl_final": trades[symbol]["stop"],
                    "atr_1h": atr_val_current, "atr_mult_at_entry": 0.6,
                    "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                    "supertrend_on": supertrend_ok, "ema25_1h": ema25, "ema200_1h": ema200_1h,
                    "ema50_4h": ema50_4h, "ema200_4h": ema200_4h,
                    "vol_ma5": vol5, "vol_ma20": vol20, "vol_ratio": vol5 / max(vol20, 1e-9),
                    "btc_uptrend": btc_up, "eth_uptrend": eth_up,
                    "reason_entry": trades[symbol]["reason_entry"], "reason_exit": ""
                })

        # ---- TP3 (≥ +5.0%) ----
        if gain >= 5.0 and not trades[symbol].get("tp3", False):
            last_tp2_time = trades[symbol]["tp_times"].get("tp2")
            if not last_tp2_time or (datetime.now() - last_tp2_time).total_seconds() >= 120:
                trades[symbol]["tp3"] = True
                trades[symbol]["tp_times"]["tp3"] = datetime.now()
                # stop > entrée (+3%)
                trades[symbol]["stop"] = max(trades[symbol]["stop"], entry * 1.03)
                save_trades()
                
                msg = format_tp_msg(
                    3, symbol, trades[symbol]["trade_id"], price, gain,
                    trades[symbol]["stop"], ((trades[symbol]["stop"] - entry) / entry) * 100,
                    elapsed_time, "Stop > entrée (+3%)"
                )
                await tg_send(msg)

                log_trade_csv({
                    "ts_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_id": trades[symbol]["trade_id"],
                    "symbol": symbol,
                    "event": "TP3",
                    "strategy": "aggressive",
                    "version": BOT_VERSION,
                    "entry": entry, "exit": "", "price": price, "pnl_pct": gain,
                    "position_pct": trades[symbol]["position_pct"],
                    "sl_initial": trades[symbol]["sl_initial"],
                    "sl_final": trades[symbol]["stop"],
                    "atr_1h": atr_val_current, "atr_mult_at_entry": 0.6,
                    "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                    "supertrend_on": supertrend_ok, "ema25_1h": ema25, "ema200_1h": ema200_1h,
                    "ema50_4h": ema50_4h, "ema200_4h": ema200_4h,
                    "vol_ma5": vol5, "vol_ma20": vol20, "vol_ratio": vol5 / max(vol20, 1e-9),
                    "btc_uptrend": btc_up, "eth_uptrend": eth_up,
                    "reason_entry": trades[symbol]["reason_entry"], "reason_exit": ""
                })

                log_trade(symbol, "SELL", price, gain)
                _delete_trade(symbol)
                return

        # ---- Si TP1 pris puis le trade retombe trop ----
        if trades[symbol].get("tp1", False) and gain < 1:
            raison_sortie = "Perte de momentum après TP1"
            msg = format_exit_msg(
                symbol, trades[symbol]["trade_id"], price, gain,
                trades[symbol]["stop"], elapsed_time, raison_sortie
            )
            await tg_send(msg)
            log_trade_csv({
                "ts_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "trade_id": trades[symbol]["trade_id"],
                "symbol": symbol,
                "event": "SELL",
                "strategy": "aggressive",
                "version": BOT_VERSION,
                "entry": entry, "exit": price, "price": price, "pnl_pct": gain,
                "position_pct": trades[symbol]["position_pct"],
                "sl_initial": trades[symbol]["sl_initial"],
                "sl_final": trades[symbol]["stop"],
                "atr_1h": atr_val_current, "atr_mult_at_entry": 0.6,
                "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                "supertrend_on": supertrend_ok, "ema25_1h": ema25, "ema200_1h": ema200_1h,
                "ema50_4h": ema50_4h, "ema200_4h": ema200_4h,
                "vol_ma5": vol5, "vol_ma20": vol20, "vol_ratio": vol5 / max(vol20, 1e-9),
                "btc_uptrend": btc_up, "eth_uptrend": eth_up,
                "reason_entry": trades[symbol]["reason_entry"], "reason_exit": raison_sortie
            })
            log_trade(symbol, "SELL", price, gain)
            _delete_trade(symbol)
            return

        # ---- Stop touché ou perte max ----
        if price < trades[symbol]["stop"] or gain <= -1.5:
            msg = format_stop_msg(
                symbol, trades[symbol]["trade_id"], trades[symbol]["stop"],
                gain, rsi, adx_value, vol5 / max(vol20, 1e-9)
            )
            await tg_send(msg)

            event_name = "STOP" if price < trades[symbol]["stop"] else "SELL"
            log_trade_csv({
                "ts_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "trade_id": trades[symbol]["trade_id"],
                "symbol": symbol,
                "event": event_name,
                "strategy": "aggressive",         # <- important
                "version": BOT_VERSION,
                "entry": entry,
                "exit": price,
                "price": price,
                "pnl_pct": gain,
                "position_pct": trades[symbol]["position_pct"],
                "sl_initial": trades[symbol]["sl_initial"],
                "sl_final": trades[symbol]["stop"],
                "atr_1h": atr_val_current,        # cohérent avec le reste de la section
                "atr_mult_at_entry": 0.6,
                "rsi_1h": rsi,
                "macd": macd,
                "signal": signal,
                "adx_1h": adx_value,
                "supertrend_on": supertrend_ok,   # <- bon nom ici
                "ema25_1h": ema25,
                "ema200_1h": ema200_1h,
                "ema50_4h": ema50_4h,
                "ema200_4h": ema200_4h,
                "vol_ma5": vol5,
                "vol_ma20": vol20,
                "vol_ratio": vol5 / max(vol20, 1e-9),
                "btc_uptrend": btc_up,
                "eth_uptrend": eth_up,
                "reason_entry": trades[symbol]["reason_entry"],
                "reason_exit": "Stop touché" if event_name == "STOP" else "Perte max (-1.5%)"
            })

            log_trade(symbol, event_name, price, gain)
            _delete_trade(symbol)
            return

        # ---- Trailing stop & HOLD ----
        trailing_stop_advanced(symbol, price)
        log_trade(symbol, "HOLD", price)

    except Exception as e:
        print(f"❌ Erreur stratégie agressive {symbol}: {e}")
        traceback.print_exc()

def is_recent(ts_str):
    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    return (datetime.now() - ts).total_seconds() <= 86400

async def send_daily_summary():
    if not history:
        return
    recent = [h for h in history if is_recent(h.get("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))]
    if not recent:
        await tg_send("ℹ️ Aucun trade clôturé dans les dernières 24h.")
        return

    msg = "🌟 Récapitulatif des trades (24h) :\n"
    for h in recent:
        msg += f"📈 {h['symbol']} | Entrée {h['entry']:.2f} | Sortie {h['exit']:.2f} | {h['result']:.2f}%\n"
    await tg_send(msg)

    # ⬇️ Envoi du CSV d’audit en pièce jointe
    try:
        await tg_send_doc(
            CSV_AUDIT_FILE,
            caption=f"trade_audit.csv — {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (UTC)"
        )
    except Exception as e:
        await tg_send(f"⚠️ Échec d’envoi de trade_audit.csv : {e}")

async def main_loop():
    await tg_send(f"🚀 Bot démarré {datetime.now().strftime('%H:%M:%S')}")
    global trades
    trades.update(load_trades())
    for _sym, _t in trades.items():
        try:
            last_trade_time[_sym] = datetime.fromisoformat(_t.get("time"))
        except Exception:
           pass

    last_heartbeat = None
    last_summary_day = None
    last_audit_day = None   

    while True:
        try:
            now = datetime.now()

            # ✅ Message de vie toutes les heures
            if last_heartbeat != now.hour:
                await tg_send(f"✅ Bot actif {now.strftime('%H:%M')}")
                last_heartbeat = now.hour

            # ✅ Résumé quotidien à 23h UTC
            if now.hour == 23 and (last_summary_day is None or last_summary_day != now.date()):
                await send_daily_summary()
                last_summary_day = now.date()
                
            # --- Précharge 1h/4h pour tous les symboles (réduit les requêtes) ---                            
            symbol_cache.clear()
            for s in SYMBOLS:
                symbol_cache.setdefault(s, {})
                symbol_cache[s]["1h"] = get_klines(s, interval="1h", limit=LIMIT)
                symbol_cache[s]["4h"] = get_klines(s, interval="4h", limit=LIMIT)

            # Contexte marché (on alimente market_cache avec le 1h préchargé)
            market_cache['BTCUSDT'] = symbol_cache.get('BTCUSDT', {}).get('1h', [])
            market_cache['ETHUSDT'] = symbol_cache.get('ETHUSDT', {}).get('1h', [])

            # Lancement des analyses
            await asyncio.gather(*(process_symbol(s) for s in SYMBOLS))
            await asyncio.gather(*(process_symbol_aggressive(s) for s in SYMBOLS if s not in trades))

            print("✔️ Itération terminée", flush=True)


        except Exception as e:
            await tg_send(f"⚠️ Erreur : {e}")

        await asyncio.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_loop())
