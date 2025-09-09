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
import random
import threading
from telegram.error import RetryAfter, TimedOut, NetworkError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

nest_asyncio.apply()

# [#http-session]
REQUEST_TIMEOUT = (5, 15)  # (connexion, lecture) en secondes

SESSION = requests.Session()
_retry = Retry(
    total=5,
    backoff_factor=0.5,  # 0.5s, 1s, 2s, 4s...
    status_forcelist=[418, 429, 500, 502, 503, 504],  # <-- 418 ajouté
    allowed_methods=frozenset(["GET"]),
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=100, pool_maxsize=100)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

# En-têtes "humains" (évite certains blocages)
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) BotCrypto/1.0 (+github)",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
})

# Bases Binance alternatives + rotation simple
BINANCE_BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
_base_idx = 0
def _rotate_base():
    global _base_idx
    _base_idx = (_base_idx + 1) % len(BINANCE_BASES)

def binance_get(path, params=None, max_tries=6):
    """GET robuste avec rotation de domaines et backoff + jitter."""
    for attempt in range(max_tries):
        base = BINANCE_BASES[_base_idx]
        url = f"{base}{path}"
        try:
            resp = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
            # codes à contourner
            if resp.status_code in (418, 429, 403, 500, 502, 503, 504):
                print(f"⚠️ Binance {resp.status_code} {url} try {attempt+1}/{max_tries} → rotate")
                _rotate_base()
                # backoff exponentiel + petit bruit
                time.sleep(min(0.5 * (2 ** attempt), 5.0) + random.uniform(0.05, 0.25))
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"❌ Réseau {url}: {e} try {attempt+1}/{max_tries}")
            _rotate_base()
            time.sleep(min(0.5 * (2 ** attempt), 5.0) + random.uniform(0.05, 0.25))
    return None


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
MIN_VOLUME = 600000
COOLDOWN_HOURS = 4
VOL_MED_MULT = 0.10  # Tolérance volume vs médiane 30j (était 0.25)
VOL_CONFIRM_TF = "15m"
VOL_CONFIRM_LOOKBACK = 12
VOL_CONFIRM_MULT = 1.00
ANTI_SPIKE_UP_STD = 0.8   # 0.8% mini (std)
ANTI_SPIKE_UP_AGR = 1.6   # % max d'extension (stratégie agressive)
# --- Anti-spike adaptatif (bonus) ---
ANTI_SPIKE_ATR_MULT = 1.30   # autorise une extension ≈ 1.3 × ATR% (par rapport à l'open)
ANTI_SPIKE_MAX_PCT  = 4.00   # plafond std relevé à 4%
# --- Trailing stop harmonisé (ATR TV) ---
TRAIL_TIERS = [
    (2.0, 0.8),  # gain >= 2%  -> stop = P - 0.8 * ATR
    (4.0, 0.6),  # gain >= 4%  -> stop = P - 0.6 * ATR
    (7.0, 0.4),  # gain >= 7%  -> stop = P - 0.4 * ATR
]
TRAIL_BE_AFTER = 1.5  # lock BE dès ~TP1 (≥ +1.5%)
# --- Stops init en % ---
INIT_SL_PCT_STD_MIN = 0.010  # 1.0% (standard)
INIT_SL_PCT_STD_MAX = 0.012  # 1.2%
INIT_SL_PCT_AGR_MIN = 0.010  # 1.0% (aggressive)
INIT_SL_PCT_AGR_MAX = 0.012  # 1.2%
# --- Auto-close (nouvelle logique) ---
AUTO_CLOSE_MIN_H = 12   # seuil souple: on évalue mais on NE coupe pas systématiquement
AUTO_CLOSE_HARD_H = 24  # sécurité: on coupe quoi qu'il arrive après 24h
# --- Timeout intelligent (stagnation) ---
SMART_TIMEOUT_EARLIEST_H_STD = 3      # on commence à vérifier après 3h (standard)
SMART_TIMEOUT_EARLIEST_H_AGR = 2      # après 2h (aggressive)
SMART_TIMEOUT_WINDOW_H       = 6      # on regarde les 6 dernières bougies 1h
SMART_TIMEOUT_RANGE_PCT_STD  = 0.6    # si High-Low <= 0.6% de l'entrée (standard)
SMART_TIMEOUT_RANGE_PCT_AGR  = 0.8    # 0.8% (aggressive un peu plus tolérant)
SMART_TIMEOUT_MIN_GAIN_STD   = 0.8    # on ne coupe pas si déjà > +0.8% (standard)
SMART_TIMEOUT_MIN_GAIN_AGR   = 0.5    # > +0.5% (aggressive)
SMART_TIMEOUT_ADX_MAX        = 18     # ADX faible
SMART_TIMEOUT_RSI_MAX        = 50     # RSI <= 50 = mou
SMART_TIMEOUT_VOLRATIO_MAX   = 0.90   # MA5/MA20 volume <= 0.90x
# === Filtre régime BTC ===
BTC_1H_DROP_PCT      = 1.0   # blocage si -1.0% sur 1h
BTC_3H_DROP_PCT      = 2.2   # ou -2.2% sur 3h
BTC_ADX_WEAK         = 18    # momentum faible si ADX < 18
BTC_RSI_FLOOR        = 48    # RSI bas
BTC_REGIME_BLOCK_MIN = 90    # minutes de blocage des ALTS
# === Money management (global) ===
RISK_PER_TRADE   = 0.005   # 0.5% du capital par trade
DAILY_MAX_LOSS   = -0.03   # -3% cumulé sur la journée (UTC)

def allowed_trade_slots() -> int:
    """
    MAX_TRADES dynamique :
    min(7, 1 + nb_trades_avec_score_>=8)
    On compte les trades OUVERTS avec confidence >= 8.
    """
    try:
        high = sum(1 for t in trades.values() if float(t.get("confidence", 0)) >= 8)
    except Exception:
        high = 0
    return min(7, 1 + high)

def _parse_dt_flex(ts: str):
    """Parser tolérant pour 'history' (SELL) : retourne un datetime UTC 'aware'."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            # On force l'UTC 'aware'
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None

def daily_pnl_pct_utc() -> float:
    """
    Somme des P&L (en %) des trades clôturés 'aujourd'hui' (UTC) d'après `history`.
    Utilisé par le circuit breaker pour bloquer les nouvelles entrées.
    """
    if not history:
        return 0.0
    today = datetime.now(timezone.utc).date()
    total = 0.0
    for h in history:
        ts = _parse_dt_flex(h.get("time", ""))
        if not ts:
            continue
        # si ts est naïf → on l'interprète comme UTC
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts.astimezone(timezone.utc).date() == today:
            try:
                total += float(h.get("result", 0.0))
            except Exception:
                pass
    return total

bot = Bot(token=TELEGRAM_TOKEN)
trades = {}
history = []
market_cache = {}
last_trade_time = {}
btc_block_until  = None   # datetime UTC jusqu’à laquelle on bloque les alts
btc_block_reason = ""     # mémo de la raison (pour logs)
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

def get_cached(symbol, tf="1h", limit=LIMIT, force: bool = False):
    """
    Retourne les klines depuis le cache de l'itération.
    - Recharge si `force=True` ou si le cache est absent OU plus court que `limit`.
    - N'écrase pas le cache existant si l'API renvoie moins/vides.
    """
    d = symbol_cache.setdefault(symbol, {})
    series = d.get(tf)

    need_reload = force or (series is None) or (int(limit) and len(series) < int(limit))

    if need_reload:
        fresh = get_klines(symbol, interval=tf, limit=int(limit))
        # On ne remplace que si on a vraiment mieux/plus long
        if fresh and (series is None or len(fresh) >= len(series)):
            d[tf] = fresh
            series = fresh

    return series or []

# ====== META / HELPERS POUR MESSAGES & IDs ======
BOT_VERSION = "v1.0.0"
# === Contexte marché global (pré-calcul BTC/ETH pour perf) ===
MARKET_STATE = {
    "btc": {"rsi": None, "macd": None, "signal": None, "adx": None, "up": None},
    "eth": {"rsi": None, "macd": None, "signal": None, "adx": None, "up": None},
}

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
        f"🛡 Stop initial: {sl_initial:.4f} (dist: {sl_dist_pct:.2f}%) | ATR-TV(1h): {atr:.4f}\n"
        f"🎯 TP1/TP2/TP3: +1.5% / +3% / +5% (dynamiques)\n\n"
        f"📊 Indicateurs 1H: RSI {rsi_1h:.2f} | MACD {macd:.4f}/{signal:.4f} | ADX {adx:.2f} | Supertrend {st_onoff(st_on)}\n"
        f"📈 Tendances: EMA25 {ema25:.4f} | EMA50(4h) {ema50_4h:.4f} | EMA200(1h) {ema200_1h:.4f} | EMA200(4h) {ema200_4h:.4f}\n"
        f"📦 Volume: MA5 {vol5:.0f} | MA20 {vol20:.0f} | Ratio {vol_ratio:.2f}x\n"
        f"🌐 Contexte marché: BTC uptrend={btc_up} | ETH uptrend={eth_up}\n"
        f"🧠 Score fiabilité: {score}/10 — {score_label}\n\n"
        f"📌 Raison d’entrée:\n- " + ("\n- ".join(reasons) if reasons else "Setup multi-confluence")
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

def format_autoclose_msg(symbol, trade_id, exit_price, pnl_pct, mode="soft"):
    label = "AUTO-CLOSE 24h (sécurité)" if mode == "hard" else "AUTO-CLOSE 12h (soft)"
    return (
        f"⏰ {label} | {symbol} | trade_id={trade_id}\n"
        f"⏱ UTC: {utc_now_str()} | Prix: {exit_price:.4f} | P&L: {pnl_pct:.2f}%"
    )
        
# [#volume-helpers]
def kline_vol_quote(k):  # k[7] = quote asset volume (ex: USDT)
    return float(k[7])

def volumes_series(klines, quote=True):
    return [kline_vol_quote(k) for k in klines] if quote else [float(k[5]) for k in klines]

def median_volume(symbol, interval="1h", days=30):
    klines = get_cached(symbol, interval)
    if not klines or len(klines) < 24 * days:
        return 0.0
    vols = [float(k[7]) for k in klines]
    return float(np.median(vols[-24*days:]))

def is_hourly_volume_anomalously_low(k1h, factor=0.5, min_lookback=50):
    """
    Retourne (too_low: bool, last_vol: float, median_ref: float, lookback_used: int)
    Compare le volume de la DERNIÈRE bougie 1h à la médiane du lookback dispo.
    - factor=0.5 => trop faible si < 50% de la médiane.
    - On garde aussi le plancher MIN_VOLUME.
    """
    if not k1h or len(k1h) < min_lookback:
        return False, 0.0, 0.0, 0

    vols = np.array(volumes_series(k1h, quote=True), dtype=float)
    last_vol = float(vols[-1])
    # On essaie d'utiliser jusqu'à 720 bougies (≈30j) si dispo ; sinon ce qu'on a.
    lookback = min(len(vols), 720)
    ref = float(np.median(vols[-lookback:]))

    too_low = (last_vol < max(MIN_VOLUME, ref * factor))
    return bool(too_low), last_vol, ref, lookback

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
    data = binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        print(f"❌ Erreur réseau get_klines({symbol}) (après rotation)")
        return []
    return data

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

# ==== Helpers EMA/Structure 15m ====

def ema_tv_series(values, period):
    """
    Série EMA 'TradingView-like' (même recursion que compute_ema_series).
    Retourne un np.array de même longueur que 'values'.
    """
    return compute_ema_series(np.asarray(values, dtype=float), period)

def is_bull_structure_15m(highs, lows, n=3):
    """
    True si les 'n' dernières bougies complètes forment une structure haussière :
    HH en progression ET HL en progression (strictement croissants).
    """
    if len(highs) < n or len(lows) < n:
        return False
    hh = highs[-n:]
    hl = lows[-n:]
    hh_up = all(hh[i] < hh[i+1] for i in range(n-1))
    hl_up = all(hl[i] < hl[i+1] for i in range(n-1))
    return hh_up and hl_up

def check_15m_filter(k15, breakout_level=None):
    """
    Vérifie le filtre 15m avant achat.
    - EMA20(15m) pente positive (EMA20[t] > EMA20[t-1])
    - Structure haussière sur 3 bougies complètes (HH & HL progressent)
    - Close15m > EMA20(15m)
    - Si breakout_level fourni (breakout+retest), Close15m >= breakout_level
    Retourne (ok: bool, details: str)
    """
    if not k15 or len(k15) < 25:
        return False, "Données 15m insuffisantes"

    # On ne prend que les bougies COMPLÈTES (on exclut la bougie en cours)
    k = k15[:-1] if len(k15) >= 2 else k15
    closes = [float(x[4]) for x in k]
    highs  = [float(x[2]) for x in k]
    lows   = [float(x[3]) for x in k]

    ema20_series = ema_tv_series(closes, 20)
    if len(ema20_series) < 2:
        return False, "EMA20(15m) insuffisante"

    ema20_up   = ema20_series[-1] > ema20_series[-2]
    close_ok   = closes[-1] > ema20_series[-1]
    struct_ok  = is_bull_structure_15m(highs, lows, n=3)
    retest_ok  = True if breakout_level is None else (closes[-1] >= breakout_level)

    ok = ema20_up and close_ok and struct_ok and retest_ok
    details = (f"EMA20_up={ema20_up}, Struct={struct_ok}, Close>EMA20={close_ok}"
               + (f", Close>retest={retest_ok}" if breakout_level is not None else ""))
    return ok, details

# === Ajoute ici les nouveaux helpers de l’étape 3 ===

def is_wick_hunt_1h(kline_1h_last) -> bool:
    """
    Mèche haute dominante sur la bougie 1h en cours:
    (high - close) / (high - low) > 0.6  -> considéré comme stop-hunt.
    """
    h = float(kline_1h_last[2])
    l = float(kline_1h_last[3])
    c = float(kline_1h_last[4])
    rng = max(h - l, 1e-9)
    return ((h - c) / rng) > 0.6

def anti_spike_check_std(klines, price, atr_period=14):
    """
    Retourne: (ok: bool, spike_pct: float, limit_pct: float)
    - calcule l’extension de la bougie 1h en cours vs l’open (en %)
    - seuil dynamique = max(ANTI_SPIKE_UP_STD, ANTI_SPIKE_ATR_MULT * ATR%)
      puis plafonné à ANTI_SPIKE_MAX_PCT
    """
    open_now = float(klines[-1][1])
    high_now = float(klines[-1][2])
    spike_pct = ((max(high_now, price) - open_now) / max(open_now, 1e-9)) * 100.0

    atr_val  = atr_tv(klines, period=atr_period)
    atr_pct  = (atr_val / max(open_now, 1e-9)) * 100.0

    dyn_limit = min(ANTI_SPIKE_MAX_PCT, max(ANTI_SPIKE_UP_STD, ANTI_SPIKE_ATR_MULT * atr_pct))
    return (spike_pct <= dyn_limit), float(spike_pct), float(dyn_limit)

def confirm_15m_after_signal(k15, breakout_level=None, ema25_1h=None, tol=0.001) -> bool:
    """
    Exige UNE clôture 15m (la dernière COMPLÈTE) au-dessus:
      - du niveau de retest (si breakout_level fourni), OU
      - de l'EMA25(1h) à ±0.1% (tol=0.001)
    """
    if not k15 or len(k15) < 2:
        return False
    close15 = float(k15[-2][4])  # dernière bougie 15m COMPLÈTE

    ok_level = breakout_level is not None and close15 >= breakout_level
    ok_ema   = ema25_1h is not None and close15 >= (ema25_1h * (1 - tol))
    return ok_level or ok_ema

def compute_ema(prices, period=200):
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    ema = np.convolve(prices, weights, mode='full')[:len(prices)]
    return ema[-1]

def compute_atr(klines, period=14):
    """
    [DEPRECATED] N'utilisez plus cette version classique.
    Harmonisation : ATR-TV uniquement.
    Cette fonction redirige vers atr_tv(...) pour garantir la cohérence.
    """
    try:
        return atr_tv(klines, period=period)
    except Exception:
        # fallback très défensif : renvoie 0.0 si atr_tv échoue
        return 0.0

def detect_rsi_divergence(prices, rsis):
    return prices[-1] > prices[-2] and rsis[-1] < rsis[-2]

def is_uptrend(prices, period=50):
    return prices[-1] > np.mean(prices[-period:])

def is_volume_increasing(klines):
    volumes = volumes_series(klines, quote=True)
    return np.mean(volumes[-5:]) > np.mean(volumes[-10:-5]) and np.mean(volumes[-5:]) > MIN_VOLUME

def is_market_bullish():
    """
    Version ultra simple & rapide : on lit le pré-calcul (MARKET_STATE).
    On bloque SEULEMENT si BTC **et** ETH sont tous les deux "mous":
      RSI < 45, MACD <= signal, ADX < 18.
    Sinon on laisse passer.
    """
    try:
        b = MARKET_STATE["btc"]
        e = MARKET_STATE["eth"]

        # si pas encore pré-rempli, on est permissif
        if any(v is None for v in (b["rsi"], b["macd"], b["signal"], b["adx"],
                                   e["rsi"], e["macd"], e["signal"], e["adx"])):
            return True

        bad_btc = (b["rsi"] < 45) and (b["macd"] <= b["signal"]) and (b["adx"] < 18)
        bad_eth = (e["rsi"] < 45) and (e["macd"] <= e["signal"]) and (e["adx"] < 18)
        return not (bad_btc and bad_eth)
    except Exception:
        return True


def btc_regime_blocked():
    """
    Filtre 'panic only' : on bloque les alts seulement lors de chutes franches du BTC.
    -1h ≤ -1.5% OU -3h ≤ -3.5%.
    Cooldown levé si petit rebond (≥ +0.5% sur 1h).
    """
    global btc_block_until, btc_block_reason

    k1h = market_cache.get("BTCUSDT", [])
    if not k1h or len(k1h) < 10:
        return False, ""

    closes = [float(k[4]) for k in k1h]
    drop1h = (closes[-1] - closes[-2]) / max(closes[-2], 1e-9) * 100.0
    drop3h = ((closes[-1] - closes[-4]) / max(closes[-4], 1e-9) * 100.0) if len(closes) >= 4 else 0.0

    # --- PANIC ONLY ---
    bear_now = (drop1h <= -1.5) or (drop3h <= -3.5)

    now = datetime.now(timezone.utc)

    # Si en cooldown, lever si rebond clair
    if btc_block_until and now < btc_block_until:
        rebound = drop1h >= 0.5  # +0.5% sur 1h -> on lève
        if rebound and not bear_now:
            btc_block_until = None
            btc_block_reason = ""
            return False, ""
        else:
            mins = (btc_block_until - now).total_seconds() / 60.0
            return True, f"cooldown BTC {mins:.0f} min restant — {btc_block_reason}"

    if bear_now:
        btc_block_until  = now + timedelta(minutes=BTC_REGIME_BLOCK_MIN)
        btc_block_reason = f"BTC {drop1h:.2f}%/1h, {drop3h:.2f}%/3h (panic)"
        return True, btc_block_reason

    return False, ""


def in_active_session():
    hour = datetime.now(timezone.utc).hour
    return not (0 <= hour < 6)

def is_active_liquidity_session(now=None, symbol=None):
    if now is None:
        now = datetime.now(timezone.utc)
    h = now.hour

    HIGH_LIQ = {"BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","LINKUSDT"}

    # On ne bloque que 01:00–02:00 UTC, et on NE bloque jamais les paires très liquides
    if 1 <= h < 2 and symbol not in HIGH_LIQ:
        return False, "blocked_01_02"
    return True, "ANY"

def get_klines_4h(symbol, limit=100):
    return get_klines(symbol, interval='4h', limit=limit)

def is_market_range(prices, threshold=0.015):  # 1.5% au lieu de 1.0%
    return (max(prices[-20:]) - min(prices[-20:])) / min(prices[-20:]) < threshold

def get_volatility(atr, price):
    return atr / price

def _clamp(x, a, b):
    return max(a, min(b, x))


def pick_sl_pct(volatility, pct_min, pct_max, v_hi=0.02):
    """
    Choisit un % de stop entre pct_min et pct_max selon la volatilité (ATR/price).
    v_hi=0.02 => au-delà de 2% de vol (ATR/price), on prend pct_max.
    """
    v = _clamp(volatility, 0.0, v_hi)
    t = 0.0 if v_hi == 0 else (v / v_hi)
    return pct_min + (pct_max - pct_min) * t

# --- Money management (risk-based) ---
# garde-fous globaux
POS_MIN_PCT = 1.0   # min 1% du capital
POS_MAX_PCT = 10.0  # max 10% du capital

def position_pct_from_risk(entry_price: float, stop_price: float, score: int | None = None) -> float:
    """
    Taille = (RISK_PER_TRADE / distance_stop) en %
    - distance_stop = (entry - stop)/entry  (en %)
    - cap global entre POS_MIN_PCT et POS_MAX_PCT
    - cap optionnel par score si tu gardes POSITION_BY_SCORE
    """
    risk_pct = (entry_price - stop_price) / max(entry_price, 1e-9)  # distance du stop en %
    if risk_pct <= 0:
        return POS_MIN_PCT
    pos_pct = (RISK_PER_TRADE / risk_pct) * 100.0

    # Si tu veux encore plafonner par le score, décommente la ligne suivante
    # cap_score = POSITION_BY_SCORE.get(score, POS_MAX_PCT) if score is not None else POS_MAX_PCT
    # pos_pct = min(pos_pct, cap_score)

    # Cap dur global
    return float(_clamp(pos_pct, POS_MIN_PCT, POS_MAX_PCT))


def supertrend_like_on_close(klines, period=10, multiplier=3):
    atr = atr_tv(klines, period)
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

# === Cache ATR par symbole/période/dernière bougie (évite de recalculer sans raison) ===
ATR_CACHE = {}  # key: (symbol, period, last_close_ts) -> value: atr

def _last_close_ts_ms(klines):
    try:
        return int(klines[-1][6])  # timestamp de close de la bougie en cours (ms)
    except Exception:
        return None

def atr_tv_cached(symbol, klines, period=14):
    """Retourne l'ATR-TV en lisant un cache si la bougie n'a pas changé."""
    try:
        last_ts = _last_close_ts_ms(klines)
        if last_ts is None:
            return atr_tv(klines, period)
        key = (symbol, int(period), last_ts)
        if key in ATR_CACHE:
            return ATR_CACHE[key]
        val = atr_tv(klines, period)
        ATR_CACHE[key] = val
        return val
    except Exception:
        # en cas de pépin, on retombe sur le calcul direct
        return atr_tv(klines, period)


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

def smart_timeout_check(klines_1h, entry_price, window_h=SMART_TIMEOUT_WINDOW_H,
                        range_pct=0.6):
    """
    Retourne (True, reason) si stagnation:
    - range (high-low) des `window_h` dernières bougies <= range_pct% de l'entrée
    - ET momentum faible (ADX bas + RSI <= 50 ou MACD <= signal)
    - ET volume en décélération (MA5/MA20 <= SMART_TIMEOUT_VOLRATIO_MAX)
    """
    if not klines_1h or len(klines_1h) < max(window_h, 20):
        return False, ""

    closes = [float(k[4]) for k in klines_1h]
    highs  = [float(k[2]) for k in klines_1h]
    lows   = [float(k[3]) for k in klines_1h]

    sub_h = highs[-window_h:]
    sub_l = lows[-window_h:]
    price_range_pct = ((max(sub_h) - min(sub_l)) / max(entry_price, 1e-9)) * 100.0

    adx_now = adx_tv(klines_1h, 14)
    rsi_now = rsi_tv(closes, 14)
    macd_now, signal_now = compute_macd(closes)

    vol = volumes_series(klines_1h, quote=True)
    vol5 = float(np.mean(vol[-5:])) if len(vol) >= 5 else 0.0
    vol20 = float(np.mean(vol[-20:])) if len(vol) >= 20 else 0.0
    vol_ratio = (vol5 / max(vol20, 1e-9)) if vol20 else 0.0

    cond_range    = price_range_pct <= range_pct
    cond_momentum = (adx_now <= SMART_TIMEOUT_ADX_MAX) and (rsi_now <= SMART_TIMEOUT_RSI_MAX or macd_now <= signal_now)
    cond_volume   = vol_ratio <= SMART_TIMEOUT_VOLRATIO_MAX

    if cond_range and cond_momentum and cond_volume:
        reason = (f"stagnation: range {price_range_pct:.2f}%/{window_h}h, "
                  f"ADX {adx_now:.1f}, RSI {rsi_now:.1f}, "
                  f"MACD {macd_now:.3f}≤{signal_now:.3f}, vol {vol_ratio:.2f}x")
        return True, reason
    return False, ""


def log_trade(symbol, side, price, gain=0):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), symbol, side, price, gain])
    if side == "SELL":
        history.append({
            "symbol": symbol,
            "exit": price,
            "result": gain,
            "entry": trades.get(symbol, {}).get("entry", 0),
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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
REFUSAL_FIELDS = ["ts_utc", "symbol", "reason", "trigger", "cooldown_left_min"]

def log_refusal(symbol: str, reason: str, trigger: str = "", cooldown_left_min: int | None = None):
    """Append une ligne dans refusal_log.csv (diagnostic des refus).
       - trigger : valeur déclenchante (ex. 'adx=17.8', 'vol15_ratio=1.12', 'dist_ema25=2.4%')
       - cooldown_left_min : minutes restantes de cooldown si pertinent
    """
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
            "trigger": trigger or "",
            "cooldown_left_min": "" if cooldown_left_min is None else int(cooldown_left_min),
        })

def log_info(symbol: str, reason: str, trigger: str = ""):
    # log "neutre" (juste en console) pour info/diagnostic
    print(f"[INFO] {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} {symbol} | {reason} | {trigger}")

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
def trailing_stop_advanced(symbol, current_price, atr_value=None, atr_period=14):
    """
    Trailing stop harmonisé ATR (version TradingView).
    - tiers de gain -> multiplicateurs d'ATR
    - stop n'est JAMAIS abaissé (monotone)
    - passage à BE quand gain ≥ TRAIL_BE_AFTER ou TP1 atteint
    """
    if symbol not in trades:
        return

    entry = float(trades[symbol]["entry"])
    gain_pct = ((current_price - entry) / max(entry, 1e-9)) * 100.0

    k1h = get_cached(symbol, "1h")
    if not k1h:
        return
    atr_val = atr_value if (atr_value is not None) else atr_tv_cached(symbol, k1h, period=atr_period)


    prev_stop = float(trades[symbol].get("stop", trades[symbol].get("sl_initial", entry)))
    new_stop = prev_stop

    # paliers ATR en fonction du gain
    for thresh_gain, atr_mult in TRAIL_TIERS:
        if gain_pct >= thresh_gain:
            new_stop = max(new_stop, current_price - atr_mult * atr_val)

    # lock BE après TP1 ou gain suffisant
    if trades[symbol].get("tp1", False) or gain_pct >= TRAIL_BE_AFTER:
        new_stop = max(new_stop, entry)

    # garde-fou: stop < prix courant (évite stop immédiat par arrondi)
    new_stop = min(new_stop, current_price * 0.999)

    if new_stop > prev_stop:
        trades[symbol]["stop"] = new_stop
        save_trades()

        
def compute_confidence_score(indicators):
    score = 0
    if indicators["rsi"] > 50 and indicators["rsi"] < 70: score += 2
    if indicators["macd"] > indicators["signal"]: score += 2
    if indicators["supertrend"]: score += 2
    if indicators["adx"] > 22: score += 2
    if indicators["volume_ok"]: score += 1
    if indicators["above_ema200"]: score += 1
    return min(score, 10)

def label_confidence(score):
    if score >= 8: return f"📊 Fiabilité : {score}/10 (Très Fiable)"
    elif score >= 5: return f"📊 Fiabilité : {score}/10 (Fiable)"
    elif score >= 3: return f"📊 Fiabilité : {score}/10 (Risque)"
    else: return f"📊 Fiabilité : {score}/10 (Très Risqué)"

def get_last_price(symbol):
    data = binance_get("/api/v3/ticker/price", {"symbol": symbol})
    if not data:
        print(f"⚠️ Erreur réseau get_last_price({symbol}) (après rotation)")
        return None
    try:
        return float(data["price"])
    except Exception:
        return None

# === Pré-calcul des indicateurs BTC/ETH pour booster les perfs ===
def update_market_state():
    try:
        for sym, key in (("BTCUSDT", "btc"), ("ETHUSDT", "eth")):
            k = market_cache.get(sym, [])
            if not k or len(k) < 30:
                # pas de données -> on met des None
                MARKET_STATE[key].update({"rsi": None, "macd": None, "signal": None, "adx": None, "up": None})
                continue

            closes = [float(x[4]) for x in k]
            rsi    = rsi_tv(closes, 14)
            macd, signal = compute_macd(closes)
            adx    = adx_tv(k, 14)
            up     = is_uptrend(closes)

            MARKET_STATE[key].update({
                "rsi": rsi, "macd": macd, "signal": signal, "adx": adx, "up": up
            })
    except Exception:
        # en cas de pépin, on ne bloque pas le bot
        pass

async def process_symbol(symbol):
    try:
        # --- Auto-close SOUPLE (ne coupe plus automatiquement à 12h) ---
        if symbol in trades and trades[symbol].get("strategy", "standard") == "standard":
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            elapsed_h = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600

            if elapsed_h >= AUTO_CLOSE_MIN_H:
                # On ne force plus une clôture horaire.
                # Les sorties se feront via SMART_TIMEOUT / momentum cassé / fast-exit 5m / trailing.
                pass
            else:
                # On laisse courir : sécuriser au moins à BE si possible
                if "stop" in trades[symbol]:
                    trades[symbol]["stop"] = max(trades[symbol]["stop"], trades[symbol]["entry"])
                save_trades()

        # ---------- Analyse standard ----------
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] 🔍 Analyse de {symbol}", flush=True)

        klines = get_cached(symbol, '1h')# 1h
        if not klines or len(klines) < 50:
            log_refusal(symbol, "Données 1h insuffisantes")
            return

        # --- Volume 1h anormalement faible vs médiane 30j (si dispo) ---
        try:
            vol_now_1h = float(klines[-1][7])  # volume quote de la bougie 1h

            k1h_30d  = get_cached(symbol, '1h', limit=750) or []
            vols_hist = volumes_series(k1h_30d, quote=True)[-721:]  # ~30 jours

            if len(vols_hist) >= 200:
                med_30d = float(np.median(vols_hist[:-1]))  # médiane sans la bougie en cours
                # ⛔ filtre ignoré pour BTCUSDT + seuil assoupli 25%
                if symbol != "BTCUSDT" and med_30d > 0 and vol_now_1h < VOL_MED_MULT * med_30d:
                    log_refusal(
                        symbol,
                        f"Volume 1h anormalement faible ({vol_now_1h:.0f} < {VOL_MED_MULT:.2f}×med30j {med_30d:.0f})"
                    )
                    return

            else:
                if vol_now_1h < MIN_VOLUME:
                    log_refusal(symbol, f"Volume 1h trop faible ({vol_now_1h:.0f} < {MIN_VOLUME})")
                    return
        except Exception:
            try:
                vol_now_1h = float(klines[-1][7])
                if vol_now_1h < MIN_VOLUME:
                    log_refusal(symbol, f"Volume 1h trop faible ({vol_now_1h:.0f} < {MIN_VOLUME})")
                    return
            except Exception:
                pass


        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        volumes = volumes_series(klines, quote=True)
        # moyennes de volume 1h (USDT) – safe si série courte
        vol5  = float(np.mean(volumes[-5:]))  if len(volumes)  >= 5  else 0.0
        vol20 = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
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
        # --- Tendance en 'soft' (on n'interdit plus)
        tendance_soft_notes = []
        if price < ema200:
            tendance_soft_notes.append("Prix < EMA200(1h)")
        if closes_4h[-1] < ema200_4h:
            tendance_soft_notes.append("Close4h < EMA200(4h)")
        if closes_4h[-1] < ema50_4h:
            tendance_soft_notes.append("Close4h < EMA50(4h)")

        # pénalité de score (au lieu d'un refus dur)
        indicators_soft_penalty = 0
        if price < ema200:          indicators_soft_penalty += 1
        if closes_4h[-1] < ema50_4h: indicators_soft_penalty += 1
        if closes_4h[-1] < ema200_4h: indicators_soft_penalty += 1

        if is_market_range(closes_4h):
            await tg_send(f"⚠️ Marché en range sur {symbol} → Trade bloqué")
            return
        if detect_rsi_divergence(closes, rsi_series): return

        volatility = get_volatility(atr, price)
        if volatility < 0.003:
           log_refusal(symbol, f"Volatilité trop faible (ATR/price={volatility:.4f} < 0.003)")
           return

        supertrend_signal = supertrend_like_on_close(klines)

        # --- ADX (standard) assoupli ---
        if adx_value < 10:
            log_refusal(symbol, "ADX 1h très faible", trigger=f"adx={adx_value:.1f}")
            return
        elif adx_value < 15:
            indicators_soft_penalty += 1

        # Supertrend reste obligatoire
        if not supertrend_signal:
            log_refusal(symbol, "Supertrend 1h non haussier (signal=False)")
            return

        if symbol in last_trade_time:
            cooldown_left_h = COOLDOWN_HOURS - (datetime.now(timezone.utc) - last_trade_time[symbol]).total_seconds() / 3600
            if cooldown_left_h > 0:
                log_refusal(
                    symbol,
                    "Cooldown actif",
                    cooldown_left_min=int(cooldown_left_h * 60)
                )
                return
                
        # ----- Garde-fous -----
        slots = allowed_trade_slots()
        if len(trades) >= slots:
            log_refusal(symbol, f"Nombre max de trades atteint dynamiquement ({len(trades)}/{slots})")
            return


        # --- Low-liquidity session -> SOFT ---
        ok_session, _sess = is_active_liquidity_session(symbol=symbol)
        if not ok_session:
            MAJORS = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}
            if symbol in MAJORS:
                # On laisse passer sans pénalité sur les majeures
                log_info(symbol, "Low-liquidity session (tolérée sur major)")
            else:
                # Soft penalty sur les autres paires
                indicators_soft_penalty += 1
                tendance_soft_notes.append("Session à faible liquidité")
                log_info(symbol, "Low-liquidity session (soft)")
            # Pas de return: on continue le flux
            
        # --- Filtre régime BTC ---
        if symbol != "BTCUSDT":
            blocked, why = btc_regime_blocked()
            if blocked:
                log_refusal(symbol, f"Filtre régime BTC: {why}")
                return

        # — Pré-filtre: prix trop loin de l’EMA25 (plus strict)
        EMA25_PREFILTER_STD = 1.03   # +3% (au lieu de +1.5%)
        if price > ema25 * EMA25_PREFILTER_STD:
            dist = (price / max(ema25, 1e-9) - 1) * 100
            log_refusal(
                symbol,
                f"Prix trop éloigné de l'EMA25 (>+{(EMA25_PREFILTER_STD-1)*100:.0f}%)",
                trigger=f"dist_ema25={dist:.2f}%"
            )
            return


        # [#anti-spike-standard]
        ok_spike, spike_up_pct, limit_pct = anti_spike_check_std(klines, price)
        if not ok_spike:
            log_refusal(symbol, "Anti-spike 1h (std)", trigger=f"spike={spike_up_pct:.2f}%>seuil={limit_pct:.2f}%")
            return
      
        # [#volume-confirm-standard]
        k15 = get_cached(symbol, VOL_CONFIRM_TF, limit=max(25, VOL_CONFIRM_LOOKBACK + 5))
        vols15 = volumes_series(k15, quote=True)
        if len(vols15) < VOL_CONFIRM_LOOKBACK + 1:
            log_refusal(symbol, "Données 15m insuffisantes (volume)")
            return

        # APRÈS (standard) — MA12 + seuil 1.10
        # --- volume-confirm-standard (MA12 + seuil 1.00) ---
        vol_now = float(k15[-2][7])
        vol_ma12 = float(np.mean(vols15[-13:-1]))
        vol_ratio_15m = vol_now / max(vol_ma12, 1e-9)

        if vol_ratio_15m < 0.85:
            log_refusal(symbol, "Volume 15m insuffisant", trigger=f"vol15_ratio={vol_ratio_15m:.2f} < 0.85")
            return

        # === Confluence & scoring (final) ===
        volume_ok   = float(np.mean(volumes[-5:])) > float(np.mean(volumes[-20:]))
        trend_ok    = (price > ema200) and supertrend_signal
        momentum_ok = (macd > signal) and (rsi >= 55)


        indicators = {
            "rsi": rsi,
            "macd": macd,
            "signal": signal,
            "supertrend": supertrend_signal,
            "adx": adx_value,
            "volume_ok": volume_ok,
            "above_ema200": price > ema200,
        }
        confidence = max(0, compute_confidence_score(indicators) - indicators_soft_penalty)
        label_conf = label_confidence(confidence)

        # --- Stop provisoire pour le sizing (même logique que l'entrée) ---
        volatility      = get_volatility(atr, price)  # atr/price
        sl_pct_sizing   = pick_sl_pct(volatility, INIT_SL_PCT_STD_MIN, INIT_SL_PCT_STD_MAX)
        stop_for_sizing = price * (1.0 - sl_pct_sizing)

        position_pct = position_pct_from_risk(price, stop_for_sizing)

        # --- Décision d'achat (standard) avec filtre 15m ---
        brk_ok, br_level = detect_breakout_retest(closes, highs, lookback=10, tol=0.003)
        last3_change = (closes[-1] - closes[-4]) / closes[-4]
        if last3_change > 0.022:
            brk_ok = False

        buy = False
        reasons = []

        if brk_ok and trend_ok and momentum_ok and volume_ok:
            # Filtre 15m avec niveau de breakout
            ok15, det15 = check_15m_filter(k15, breakout_level=br_level)
            if not ok15:
                log_refusal(symbol, f"Filtre 15m non validé (BRK): {det15}")
                return

            # --- Anti-chasse (wick 1h) + confirmation 15m + entrée limit resserrée ---
            if is_wick_hunt_1h(klines[-1]):
                log_refusal(symbol, "Anti-chasse: mèche haute dominante sur la bougie 1h d'entrée")
                return

            if not confirm_15m_after_signal(k15, breakout_level=br_level, ema25_1h=ema25, tol=0.001):
                log_refusal(symbol, "Anti-chasse: pas de clôture 15m > niveau de retest OU > EMA25(1h) ±0.1%")
                return

            if price > ema25 * 1.03:  # +3.0% max
                log_refusal(
                    symbol,
                    f"Entrée limit BRK: prix {price:.4f} > EMA25*1.03 ({ema25*1.03:.4f})"
                )
                return

                
            if tendance_soft_notes:
                reasons += [f"Avertissements tendance: {', '.join(tendance_soft_notes)}"]


            buy = True
            label = "⚡ Breakout + Retest validé (1h) + Confluence"
            reasons = [label, f"ADX {adx_value:.1f} >= 22", f"MACD {macd:.3f} > Signal {signal:.3f}"]

        elif trend_ok and momentum_ok and volume_ok:
            # Bande "retest" serrée autour de l'EMA25 (±0.2%)
            RETEST_BAND_STD = 0.002
            near_ema25 = (abs(price - ema25) / max(ema25, 1e-9)) <= RETEST_BAND_STD

            # On garde le contrôle de bougie, mais un peu plus permissif (3.5% au lieu de 3%)
            candle_ok = (abs(highs[-1] - lows[-1]) / max(lows[-1], 1e-9)) <= 0.035

            if near_ema25 and candle_ok:
                # Filtre 15m sans niveau de breakout
                ok15, det15 = check_15m_filter(k15, breakout_level=None)
                if not ok15:
                    log_refusal(symbol, f"Filtre 15m non validé (PB EMA25): {det15}")
                    return

                # --- Anti-chasse & confirmation 15m ---
                if is_wick_hunt_1h(klines[-1]):
                    log_refusal(symbol, "Anti-chasse: mèche haute dominante sur la bougie 1h d'entrée (PB)")
                    return

                # pour PB: on exige close 15m > EMA25(1h) ±0.1%
                if not confirm_15m_after_signal(k15, breakout_level=None, ema25_1h=ema25, tol=0.001):
                    log_refusal(symbol, "Anti-chasse: pas de clôture 15m > EMA25(1h) ±0.1% (PB)")
                    return

                if price > ema25 * 1.03:  # +1.0% max
                    log_refusal(
                        symbol,
                        f"Entrée limit PB: prix {price:.4f} > EMA25*1.01 ({ema25*1.03:.4f})"
                    )
                    return


                buy = True
                label = "✅ Pullback EMA25 propre + Confluence"
                reasons = [label, f"ADX {adx_value:.1f} >= 22", f"MACD {macd:.3f} > Signal {signal:.3f}"]

        # === GESTION TP / HOLD / SELL ===

        sell = False
        if symbol in trades and trades[symbol].get("strategy", "standard") == "standard":
            entry = trades[symbol]['entry']
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            elapsed_time = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            gain = ((price - entry) / entry) * 100
            stop = trades[symbol].get("stop", trades[symbol].get("sl_initial", price * (1 - INIT_SL_PCT_STD_MIN)))
            # --- Sortie anticipée si BTC tourne négatif (gain faible) ---
            blocked, why_btc = btc_regime_blocked()
            if blocked and gain < 0.8:
                raison = "BTC regime turned negative"
                msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, stop, elapsed_time, raison)
                await tg_send(msg)

                vol5_loc  = float(np.mean(volumes[-5:]))
                vol20_loc = float(np.mean(volumes[-20:]))

                log_trade_csv({
                    "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_id": trades[symbol]["trade_id"], "symbol": symbol,
                    "event": "SELL", "strategy": "standard", "version": BOT_VERSION,
                    "entry": entry, "exit": price, "price": price, "pnl_pct": gain,
                    "position_pct": trades[symbol].get("position_pct", ""),
                    "sl_initial": trades[symbol].get("sl_initial", ""), "sl_final": stop,
                    "atr_1h": atr, "atr_mult_at_entry": "",
                    "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                    "supertrend_on": supertrend_signal, "ema25_1h": ema25, "ema200_1h": ema200,
                    "ema50_4h": ema50_4h, "ema200_4h": ema200_4h,
                    "vol_ma5": vol5_loc, "vol_ma20": vol20_loc,
                    "vol_ratio": vol5_loc / max(vol20_loc, 1e-9),
                    "btc_uptrend": btc_up, "eth_uptrend": eth_up,
                    "reason_entry": trades[symbol].get("reason_entry", ""),
                    "reason_exit": raison
                })

                log_trade(symbol, "SELL", price, gain)
                _delete_trade(symbol)
                return

            # --- Timeout intelligent (STANDARD) ---
            if (elapsed_time >= SMART_TIMEOUT_EARLIEST_H_STD
                and gain < SMART_TIMEOUT_MIN_GAIN_STD
                and not trades[symbol].get("tp1", False)):
                k1h_now = get_cached(symbol, '1h')
                trig, why = smart_timeout_check(k1h_now, entry,
                                                window_h=SMART_TIMEOUT_WINDOW_H,
                                                range_pct=SMART_TIMEOUT_RANGE_PCT_STD)
                if trig:
                    raison = f"Timeout intelligent (standard): {why}"
                    msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, stop, elapsed_time, raison)
                    await tg_send(msg)

                    # log CSV
                    vol5_loc  = float(np.mean(volumes[-5:]))
                    vol20_loc = float(np.mean(volumes[-20:]))
                    log_trade_csv({
                        "trade_id": trades[symbol]["trade_id"], "symbol": symbol, "event": "SMART_TIMEOUT",
                        "strategy": "standard", "version": BOT_VERSION,
                        "entry": entry, "exit": price, "price": price, "pnl_pct": gain,
                        "position_pct": trades[symbol].get("position_pct", ""),
                        "sl_initial": trades[symbol].get("sl_initial", ""), "sl_final": stop,
                        "atr_1h": atr, "atr_mult_at_entry": "",
                        "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                        "supertrend_on": supertrend_signal, "ema25_1h": ema25, "ema200_1h": ema200,
                        "ema50_4h": ema50_4h, "ema200_4h": ema200_4h,
                        "vol_ma5": vol5_loc, "vol_ma20": vol20_loc, "vol_ratio": vol5_loc / max(vol20_loc, 1e-9),
                        "btc_uptrend": btc_up, "eth_uptrend": eth_up,
                        "reason_entry": trades[symbol].get("reason_entry", ""), "reason_exit": raison
                    })

                    log_trade(symbol, "SELL", price, gain)
                    _delete_trade(symbol)
                    return

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
                    "sl_initial": trades[symbol].get("sl_initial", ""),
                    "sl_final": stop,
                    "atr_mult_at_entry": "",
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

            trailing_stop_advanced(symbol, price, atr_value=atr)
            stop = trades[symbol].get("stop", trades[symbol].get("sl_initial", price * (1 - INIT_SL_PCT_STD_MIN)))


            if gain < 0.8 and (rsi < 48 or macd < signal):
                raison = "Momentum cassé (sortie anticipée)"
                msg = format_exit_msg(
                    symbol, trades[symbol]["trade_id"], price, gain, stop, elapsed_time, raison
                )
                await tg_send(msg)

                vol5_loc  = float(np.mean(volumes[-5:]))
                vol20_loc = float(np.mean(volumes[-20:]))

                log_trade_csv({
                    "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_id": trades[symbol]["trade_id"],
                    "symbol": symbol,
                    "event": "SELL",
                    "strategy": "standard",
                    "version": BOT_VERSION,
                    "entry": entry,
                    "exit": price,
                    "price": price,
                    "pnl_pct": gain,
                    "position_pct": trades[symbol].get("position_pct", ""),
                    "sl_initial": trades[symbol].get("sl_initial", ""),  # stop initial en %
                    "sl_final": stop,
                    "atr_1h": atr,
                    "atr_mult_at_entry": "",  # n/a (on n'utilise plus ATR*0.6)
                    "rsi_1h": rsi,
                    "macd": macd,
                    "signal": signal,
                    "adx_1h": adx_value,
                    "supertrend_on": supertrend_signal,
                    "ema25_1h": ema25,
                    "ema200_1h": ema200,
                    "ema50_4h": ema50_4h,
                    "ema200_4h": ema200_4h,
                    "vol_ma5": vol5_loc,
                    "vol_ma20": vol20_loc,
                    "vol_ratio": vol5_loc / max(vol20_loc, 1e-9),
                    "btc_uptrend": btc_up,
                    "eth_uptrend": eth_up,
                    "reason_entry": trades[symbol].get("reason_entry", ""),
                    "reason_exit": raison
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
                    if isinstance(last_tp_time, str):
                        try:
                            last_tp_time = datetime.fromisoformat(last_tp_time)
                        except Exception:
                            last_tp_time = None
                            
                    if not last_tp_time or (datetime.now(timezone.utc) - last_tp_time).total_seconds() >= 120:
                        trades[symbol][f"tp{tp_num}"] = True
                        trades[symbol]["tp_times"][f"tp{tp_num}"] = datetime.now(timezone.utc)
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
                            "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "trade_id": trades[symbol]["trade_id"],
                            "symbol": symbol,
                            "event": f"TP{tp_num}",
                            "strategy": "standard",
                            "version": BOT_VERSION,
                            "entry": entry,
                            "exit": "",
                            "price": price,
                            "pnl_pct": gain,
                            "position_pct": trades[symbol].get("position_pct", ""),
                            "sl_initial": trades[symbol].get("sl_initial", ""),
                            "sl_final": trades[symbol]["stop"],
                            "atr_1h": atr,
                            "atr_mult_at_entry": "",
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
                    "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_id": trades[symbol]["trade_id"],
                    "symbol": symbol,
                    "event": event_name,
                    "strategy": "standard",
                    "version": BOT_VERSION,
                    "entry": entry,
                    "exit": price,
                    "price": price,
                    "pnl_pct": gain,
                    "position_pct": trades[symbol].get("position_pct", ""),
                    "sl_initial": trades[symbol].get("sl_initial", ""),
                    "sl_final": trades[symbol]["stop"],
                    "atr_1h": atr,
                    "atr_mult_at_entry": "",
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

        # --- Circuit breaker JOUR (avant toute nouvelle entrée) ---
        if buy and symbol not in trades:
            pnl_today = daily_pnl_pct_utc()
            if pnl_today <= DAILY_MAX_LOSS * 100.0:
                log_refusal(symbol, f"Daily loss limit hit (P&L jour {pnl_today:.2f}% ≤ {DAILY_MAX_LOSS*100:.0f}%)")
                return
        # --- Entrée (BUY) ---
        if buy and symbol not in trades:
            trade_id = make_trade_id(symbol)

            # 🔁 réutilise le stop calculé plus haut pour le sizing
            sl_initial = stop_for_sizing   # <= au lieu de recalculer avec pick_sl_pct(...)

            trades[symbol] = {
                "entry": price,
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "confidence": confidence,
                "stop": sl_initial,
                "position_pct": position_pct,    # <- issu du sizing au risque
                "trade_id": trade_id,
                "tp_times": {},
                "sl_initial": sl_initial,
                "reason_entry": "; ".join(reasons) if reasons else "",
                "strategy": "standard",
            }
            last_trade_time[symbol] = datetime.now(timezone.utc)
            save_trades()

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

        elif sell and symbol in trades and trades[symbol].get("strategy", "standard") == "standard":
            entry = trades[symbol]['entry']
            gain = ((price - entry) / entry) * 100
            stop_used = trades[symbol].get("stop", trades[symbol].get("sl_initial", price * (1 - INIT_SL_PCT_STD_MIN)))
            await tg_send(f"🔴 Vente {symbol} à {price:.4f} | Gain {gain:.2f}% | Stop final: {stop_used:.4f}")
            log_trade(symbol, "SELL", price, gain)
            _delete_trade(symbol)

    except Exception as e:
        print(f"❌ Erreur {symbol}: {e}", flush=True)
        traceback.print_exc()

async def process_symbol_aggressive(symbol):
    try:
        # --- Paramètres VOLUME (aggressive) ---
        MIN_VOLUME_LOCAL = 150_000          # fallback local si pas de médiane 30j
        VOL_MED_MULT_AGR = 0.05             # 5% de la médiane 30j (au lieu de 10%)
        VOL_CONFIRM_MULT_AGR = 1.10         # 15m: 1.10x (au lieu de 1.25x)
        VOL_CONFIRM_LOOKBACK_AGR = 12       # 15m: MA12 (au lieu de 20)

        # ---- Auto-close SOUPLE (aggressive) ----
        if symbol in trades and trades[symbol].get("strategy") == "aggressive":
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            elapsed_h = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            if elapsed_h >= AUTO_CLOSE_MIN_H:
                # On ne force plus une clôture horaire ici.
                # Les vraies sorties seront gérées plus bas (SMART_TIMEOUT / momentum cassé / fast-exit 5m / trailing).
                pass

        # ---- Analyse agressive ----
        klines = get_cached(symbol, '1h')
        if not klines or len(klines) < 50:
            log_refusal(symbol, "Données 1h insuffisantes")
            return

        closes  = [float(k[4]) for k in klines]
        highs   = [float(k[2]) for k in klines]
        lows    = [float(k[3]) for k in klines]
        volumes = volumes_series(klines, quote=True)
        vol5    = float(np.mean(volumes[-5:]))  if len(volumes) >= 5  else 0.0
        vol20   = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
        volume_ok = (vol5 > vol20) and (vol5 > 0.0)

        price = get_last_price(symbol)
        if price is None:
            log_refusal(symbol, "API prix indisponible")
            return

        # --- Volume 1h anormalement faible vs médiane 30j (si dispo) ---
        try:
            vol_now_1h = float(klines[-1][7])  # volume quote de la bougie 1h

            # Charger ~30 jours (720 bougies 1h). Si l'endpoint refuse → fallback.
            k1h_30d  = get_cached(symbol, '1h', limit=750) or []
            vols_hist = volumes_series(k1h_30d, quote=True)[-721:]  # ~30j + bougie courante

            if len(vols_hist) >= 200:
                # médiane sur les 30j SANS la bougie en cours
                med_30d = float(np.median(vols_hist[:-1]))
                # ⛔ ignorer pour BTCUSDT + seuil assoupli à 5% (aggressive)
                if symbol != "BTCUSDT" and med_30d > 0 and vol_now_1h < VOL_MED_MULT_AGR * med_30d:
                    log_refusal(
                        symbol,
                        f"Volume 1h anormalement faible ({vol_now_1h:.0f} < {VOL_MED_MULT_AGR:.2f}×med30j {med_30d:.0f})",
                    )
                    return

            # Fallback si on n'a pas 30j : garde-fou local plus bas
            if vol_now_1h < MIN_VOLUME_LOCAL:
                log_refusal(symbol, f"Volume 1h trop faible ({vol_now_1h:.0f} < {MIN_VOLUME_LOCAL})")
                return

        except Exception:
            # En cas d’erreur réseau ou autre, fallback MIN_VOLUME_LOCAL
            try:
                vol_now_1h = float(klines[-1][7])
                if vol_now_1h < MIN_VOLUME_LOCAL:
                    log_refusal(symbol, f"Volume 1h trop faible ({vol_now_1h:.0f} < {MIN_VOLUME_LOCAL})")
                    return
            except Exception:
                pass

        # ---- Indicateurs (versions TradingView) ----
        rsi       = rsi_tv(closes, period=14)
        macd,signal = compute_macd(closes)
        ema200    = ema_tv(closes, 200)
        ema200_1h = ema200  
        atr       = atr_tv(klines, period=14)
        adx_value = adx_tv(klines, period=14)
        ema25     = ema_tv(closes, 25)
        # comparer l'élan MACD vs bougie précédente
        macd_prev, signal_prev = compute_macd(closes[:-1])

        # ---- Garde-fous ----
        slots = allowed_trade_slots()
        if len(trades) >= slots:
            log_refusal(symbol, f"Nombre max de trades atteint dynamiquement ({len(trades)}/{slots})")
            return

        # --- Low-liquidity session -> SOFT ---
        # init pénalités/notes (doit être fait AVANT de s'en servir)
        indicators_soft_penalty = 0
        tendance_soft_notes = []

        ok_session, _sess = is_active_liquidity_session(symbol=symbol)

        if not ok_session:
            MAJORS = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}
            if symbol in MAJORS:
                # On laisse passer sans pénalité sur les majeures
                log_info(symbol, "Low-liquidity session (tolérée sur major)")
            else:
                # Soft penalty sur les autres paires
                indicators_soft_penalty += 1
                tendance_soft_notes.append("Session à faible liquidité")
                log_info(symbol, "Low-liquidity session (soft)")
            # Pas de return: on continue le flux

        if not is_market_bullish():
            return

        if symbol in last_trade_time:
            cooldown_left_h = COOLDOWN_HOURS - (datetime.now(timezone.utc) - last_trade_time[symbol]).total_seconds() / 3600
            if cooldown_left_h > 0:
                log_refusal(symbol, "Cooldown actif", cooldown_left_min=int(cooldown_left_h * 60))
                return

        # --- Filtre régime BTC (aggressive) ---
        if symbol != "BTCUSDT":
            blocked, why = btc_regime_blocked()
            if blocked:
                log_refusal(symbol, f"Filtre régime BTC: {why}")
                return

        # ---- Conditions confluence ----
        supertrend_ok = supertrend_like_on_close(klines)
        above_ema200  = price >= ema200 * 0.98  # soft

        if not above_ema200:
            indicators_soft_penalty += 1
            tendance_soft_notes.append("Prix ~ sous EMA200(1h)")

        # ADX devient une pénalité (seuil assoupli)
        adx_ok = adx_value >= 15
        if not adx_ok:
            indicators_soft_penalty += 1

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
        ema50_4h  = ema_tv(c4, 50)
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

        # Retest valide = breakout OU rebond EMA25 ±0.3%
        RETEST_BAND_AGR = 0.003
        near_level = abs(price - last10_high) / last10_high <= RETEST_BAND_AGR
        near_ema25  = abs(price - ema25)      / ema25      <= RETEST_BAND_AGR

        # éviter d’acheter trop loin de l’EMA25
        too_far_from_ema25 = price >= ema25 * 1.03
        if too_far_from_ema25:
            log_refusal(symbol, f"Prix trop éloigné de l'EMA25 (+0.8%) (prix={price}, ema25={ema25})")
            return
        # si le prix n'est proche ni du niveau de breakout ni de l'EMA25
        if not (near_level or near_ema25):
            log_refusal(symbol, "Pas de retest valide (ni proche breakout, ni proche EMA25)")
            return

        # Anti-spike (bougie 1h en cours)
        open_now  = float(klines[-1][1])
        high_now  = float(klines[-1][2])
        spike_up_pct = ((max(high_now, price) - open_now) / max(open_now, 1e-9)) * 100.0
        if spike_up_pct > ANTI_SPIKE_UP_AGR:
            log_refusal(symbol, "Anti-spike (aggressive)", trigger=f"bougie_1h={spike_up_pct:.2f}%, seuil={ANTI_SPIKE_UP_AGR:.1f}%")
            return

        # ---- Confirmation volume 15m (aggressive) ----
        k15 = get_cached(symbol, '15m', limit=max(25, VOL_CONFIRM_LOOKBACK_AGR + 5))
        vols15 = volumes_series(k15, quote=True)
        if len(vols15) < VOL_CONFIRM_LOOKBACK_AGR + 1:
            log_refusal(symbol, "Données 15m insuffisantes (volume)")
            return

        # APRÈS (agressif) — MA10 + seuil 1.05
        # [#volume-confirm-aggressive] — MA12 + seuil 0.95
        vol_now = float(k15[-2][7])
        vol_ma12 = float(np.mean(vols15[-13:-1]))
        vol_ratio_15m = vol_now / max(vol_ma12, 1e-9)

        if vol_ratio_15m < 0.85:
            log_refusal(symbol, "Volume 15m insuffisant (aggressive)", trigger=f"vol15_ratio={vol_ratio_15m:.2f} < 0.85")
            return
         
        # --- Filtre structure 15m (aggressive) ---
        if near_level:
            ok15, det15 = check_15m_filter(k15, breakout_level=last10_high)
        else:
            ok15, det15 = check_15m_filter(k15, breakout_level=None)
        if not ok15:
            log_refusal(symbol, f"Filtre 15m non validé (aggressive): {det15}")
            return

        # ---- Scoring + raisons ----
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
        score = max(0, score - indicators_soft_penalty)
        label_conf = label_confidence(score)
        if score < 7:
            return

        # ---- Sizing au risque (aggressive) ----
        volatility_aggr    = get_volatility(atr, price)  # atr/price (1h)
        sl_pct_sizing_aggr = pick_sl_pct(volatility_aggr, INIT_SL_PCT_AGR_MIN, INIT_SL_PCT_AGR_MAX)
        stop_for_sizing    = price * (1.0 - sl_pct_sizing_aggr)
        position_pct       = position_pct_from_risk(price, stop_for_sizing, score)

        # Anti-chasse (wick 1h) + confirmation 15m + entrée limit
        if is_wick_hunt_1h(klines[-1]):
            log_refusal(symbol, "Anti-chasse: mèche haute dominante sur la bougie 1h d'entrée (aggressive)")
            return

        br_level_for_check = last10_high if near_level else None
        if not confirm_15m_after_signal(k15, breakout_level=br_level_for_check, ema25_1h=ema25, tol=0.001):
            if br_level_for_check is not None:
                log_refusal(symbol, "Anti-chasse: pas de clôture 15m > niveau de retest (aggressive)")
            else:
                log_refusal(symbol, "Anti-chasse: pas de clôture 15m > EMA25(1h) ±0.1% (aggressive)")
            return

        if price > ema25 * 1.03:
            log_refusal(symbol, f"Entrée limit aggressive: prix {price:.4f} > EMA25*1.03 ({ema25*1.03:.4f})")
    r        eturn

        # --- Circuit breaker JOUR (aggressive) ---
        pnl_today = daily_pnl_pct_utc()
        if pnl_today <= DAILY_MAX_LOSS * 100.0:
            log_refusal(symbol, f"Daily loss limit hit (P&L jour {pnl_today:.2f}% ≤ {DAILY_MAX_LOSS*100:.0f}%)")
            return

        reasons = [
            "Breakout+Retest validé",
            f"ADX {adx_value:.1f} >= 18",
            f"MACD {macd:.3f} > Signal {signal:.3f}",
        ]

        # ---- Entrée ----
        trade_id = make_trade_id(symbol)
        ema200_1h = ema200
        sl_initial = stop_for_sizing

        trades[symbol] = {
            "entry": price,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "confidence": score,
            "stop": sl_initial,
            "position_pct": position_pct,
            "trade_id": trade_id,
            "tp_times": {},
            "sl_initial": sl_initial,
            "reason_entry": "; ".join(reasons),
            "strategy": "aggressive",
        }
        last_trade_time[symbol] = datetime.now(timezone.utc)
        save_trades()

        # uptrends via cache préchargé
        btc_up = is_uptrend([float(k[4]) for k in market_cache.get("BTCUSDT", [])]) if market_cache.get("BTCUSDT") else False
        eth_up = is_uptrend([float(k[4]) for k in market_cache.get("ETHUSDT", [])]) if market_cache.get("ETHUSDT") else False

        msg = format_entry_msg(
            symbol, trade_id, "aggressive", BOT_VERSION, price, position_pct,
            trades[symbol]["sl_initial"], ((price - trades[symbol]["sl_initial"]) / price) * 100, atr,
            rsi, macd, signal, adx_value,
            supertrend_ok, ema25, ema50_4h, ema200_1h, ema200_4h,
            vol5, vol20, vol5 / max(vol20, 1e-9),
            btc_up, eth_up,
            score, label_conf, reasons
        )
        await tg_send(msg)

        # Logging CSV (BUY)
        log_trade_csv({
            "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "trade_id": trade_id,
            "symbol": symbol,
            "event": "BUY",
            "strategy": "aggressive",
            "version": BOT_VERSION,
            "entry": price,
            "exit": "",
            "price": price,
            "pnl_pct": "",
            "position_pct": position_pct,
            "sl_initial": trades[symbol]["sl_initial"],
            "sl_final": "",
            "atr_1h": atr,
            "atr_mult_at_entry": "",
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
        atr_val_current = atr_tv(klines)
        entry = trades[symbol]["entry"]
        gain  = ((price - entry) / entry) * 100
        stop  = trades[symbol].get("stop", trades[symbol].get("sl_initial", price * (1 - INIT_SL_PCT_AGR_MIN)))
        elapsed_time = (
            datetime.now(timezone.utc)
            - datetime.strptime(trades[symbol]["time"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        ).total_seconds() / 3600

        btc_up = is_uptrend([float(k[4]) for k in market_cache.get("BTCUSDT", [])]) if market_cache.get("BTCUSDT") else False
        eth_up = is_uptrend([float(k[4]) for k in market_cache.get("ETHUSDT", [])]) if market_cache.get("ETHUSDT") else False

        # --- Sortie anticipée si BTC tourne négatif (gain faible) ---
        blocked, why_btc = btc_regime_blocked()
        if blocked and gain < 0.8:
            raison = "BTC regime turned negative"
            msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, stop, elapsed_time, raison)
            await tg_send(msg)

            vol5_loc  = float(np.mean(volumes[-5:]))
            vol20_loc = float(np.mean(volumes[-20:]))

            log_trade_csv({
                "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "trade_id": trades[symbol]["trade_id"], "symbol": symbol,
                "event": "SELL", "strategy": "aggressive", "version": BOT_VERSION,
                "entry": entry, "exit": price, "price": price, "pnl_pct": gain,
                "position_pct": trades[symbol]["position_pct"],
                "sl_initial": trades[symbol]["sl_initial"], "sl_final": stop,
                "atr_1h": atr_val_current, "atr_mult_at_entry": "",
                "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                "supertrend_on": supertrend_ok, "ema25_1h": ema25, "ema200_1h": ema200_1h,
                "ema50_4h": ema50_4h, "ema200_4h": ema200_4h,
                "vol_ma5": vol5_loc, "vol_ma20": vol20_loc,
                "vol_ratio": vol5_loc / max(vol20_loc, 1e-9),
                "btc_uptrend": btc_up, "eth_uptrend": eth_up,
                "reason_entry": trades[symbol]["reason_entry"],
                "reason_exit": raison
            })
            log_trade(symbol, "SELL", price, gain)
            _delete_trade(symbol)
            return

        # --- Timeout intelligent (AGGRESSIVE) ---
        if (elapsed_time >= SMART_TIMEOUT_EARLIEST_H_AGR
            and gain < SMART_TIMEOUT_MIN_GAIN_AGR
            and not trades[symbol].get("tp1", False)):
            k1h_now = get_cached(symbol, '1h')
            trig, why = smart_timeout_check(k1h_now, entry,
                                            window_h=SMART_TIMEOUT_WINDOW_H,
                                            range_pct=SMART_TIMEOUT_RANGE_PCT_AGR)
            if trig:
                raison = f"Timeout intelligent (aggressive): {why}"
                msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, stop, elapsed_time, raison)
                await tg_send(msg)

                vol5_loc  = float(np.mean(volumes[-5:]))
                vol20_loc = float(np.mean(volumes[-20:]))
                log_trade_csv({
                    "trade_id": trades[symbol]["trade_id"], "symbol": symbol, "event": "SMART_TIMEOUT",
                    "strategy": "aggressive", "version": BOT_VERSION,
                    "entry": entry, "exit": price, "price": price, "pnl_pct": gain,
                    "position_pct": trades[symbol]["position_pct"],
                    "sl_initial": trades[symbol]["sl_initial"], "sl_final": stop,
                    "atr_1h": atr_val_current, "atr_mult_at_entry": "",
                    "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                    "supertrend_on": supertrend_ok, "ema25_1h": ema25, "ema200_1h": ema200_1h,
                    "ema50_4h": ema50_4h, "ema200_4h": ema200_4h,
                    "vol_ma5": vol5_loc, "vol_ma20": vol20_loc, "vol_ratio": vol5_loc / max(vol20_loc, 1e-9),
                    "btc_uptrend": btc_up, "eth_uptrend": eth_up,
                    "reason_entry": trades[symbol]["reason_entry"], "reason_exit": raison
                })
                log_trade(symbol, "SELL", price, gain)
                _delete_trade(symbol)
                return

        # — Sortie dynamique rapide (5m) —
        triggered, fx = fast_exit_5m_trigger(symbol, entry, price)
        if triggered:
            vol5_local  = float(np.mean(volumes[-5:]))
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
                "atr_1h": atr_val_current, "atr_mult_at_entry": "",
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

        # ---- Sortie anticipée "momentum cassé" (aggressive) ----
        if gain < 0.8 and (rsi < 48 or macd < signal):
            raison = "Momentum cassé (sortie anticipée)"
            msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, stop, elapsed_time, raison)
            await tg_send(msg)

            vol5_loc  = float(np.mean(volumes[-5:]))
            vol20_loc = float(np.mean(volumes[-20:]))

            log_trade_csv({
                "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "trade_id": trades[symbol]["trade_id"],
                "symbol": symbol,
                "event": "SELL",
                "strategy": "aggressive",
                "version": BOT_VERSION,
                "entry": entry,
                "exit": price,
                "price": price,
                "pnl_pct": gain,
                "position_pct": trades[symbol]["position_pct"],
                "sl_initial": trades[symbol]["sl_initial"],
                "sl_final": stop,
                "atr_1h": atr_val_current,
                "atr_mult_at_entry": "",
                "rsi_1h": rsi,
                "macd": macd,
                "signal": signal,
                "adx_1h": adx_value,
                "supertrend_on": supertrend_ok,
                "ema25_1h": ema25,
                "ema200_1h": ema200_1h,
                "ema50_4h": ema50_4h,
                "ema200_4h": ema200_4h,
                "vol_ma5": vol5_loc,
                "vol_ma20": vol20_loc,
                "vol_ratio": vol5_loc / max(vol20_loc, 1e-9),
                "btc_uptrend": btc_up,
                "eth_uptrend": eth_up,
                "reason_entry": trades[symbol]["reason_entry"],
                "reason_exit": raison
            })
            log_trade(symbol, "SELL", price, gain)
            _delete_trade(symbol)
            return

        # ---- TP1 (≥ +1.5%) ----
        if gain >= 1.5 and not trades[symbol].get("tp1", False):
            trades[symbol]["tp1"] = True
            trades[symbol]["tp_times"]["tp1"] = datetime.now(timezone.utc)
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
                "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "trade_id": trades[symbol]["trade_id"],
                "symbol": symbol,
                "event": "TP1",
                "strategy": "aggressive",
                "version": BOT_VERSION,
                "entry": entry, "exit": "", "price": price, "pnl_pct": gain,
                "position_pct": trades[symbol]["position_pct"],
                "sl_initial": trades[symbol]["sl_initial"],
                "sl_final": trades[symbol]["stop"],
                "atr_1h": atr_val_current, "atr_mult_at_entry": "",
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
            if isinstance(last_tp1_time, str):
                try:
                    last_tp1_time = datetime.fromisoformat(last_tp1_time)
                except Exception:
                    last_tp1_time = None
 
            if not last_tp1_time or (datetime.now(timezone.utc) - last_tp1_time).total_seconds() >= 120:
                trades[symbol]["tp2"] = True
                trades[symbol]["tp_times"]["tp2"] = datetime.now(timezone.utc)
                trades[symbol]["stop"] = max(trades[symbol]["stop"], entry * 1.015)
                save_trades()
 
                msg = format_tp_msg(
                    2, symbol, trades[symbol]["trade_id"], price, gain,
                    trades[symbol]["stop"], ((trades[symbol]["stop"] - entry) / entry) * 100,
                    elapsed_time, "Stop > entrée (+1.5%)"
                )
                await tg_send(msg)
 
                log_trade_csv({
                    "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_id": trades[symbol]["trade_id"],
                    "symbol": symbol,
                    "event": "TP2",
                    "strategy": "aggressive",
                    "version": BOT_VERSION,
                    "entry": entry, "exit": "", "price": price, "pnl_pct": gain,
                    "position_pct": trades[symbol]["position_pct"],
                    "sl_initial": trades[symbol]["sl_initial"],
                    "sl_final": trades[symbol]["stop"],
                    "atr_1h": atr_val_current, "atr_mult_at_entry": "",
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
            if isinstance(last_tp2_time, str):
                try:
                    last_tp2_time = datetime.fromisoformat(last_tp2_time)
                except Exception:
                    last_tp2_time = None

            if not last_tp2_time or (datetime.now(timezone.utc) - last_tp2_time).total_seconds() >= 120:
                trades[symbol]["tp3"] = True
                trades[symbol]["tp_times"]["tp3"] = datetime.now(timezone.utc)
                trades[symbol]["stop"] = max(trades[symbol]["stop"], entry * 1.03)
                save_trades()
        
                msg = format_tp_msg(
                    3, symbol, trades[symbol]["trade_id"], price, gain,
                    trades[symbol]["stop"], ((trades[symbol]["stop"] - entry) / entry) * 100,
                    elapsed_time, "Stop > entrée (+3%)"
                )
                await tg_send(msg)

                log_trade_csv({
                    "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_id": trades[symbol]["trade_id"],
                    "symbol": symbol,
                    "event": "TP3",
                    "strategy": "aggressive",
                    "version": BOT_VERSION,
                    "entry": entry, "exit": "", "price": price, "pnl_pct": gain,
                    "position_pct": trades[symbol]["position_pct"],
                    "sl_initial": trades[symbol]["sl_initial"],
                    "sl_final": trades[symbol]["stop"],
                    "atr_1h": atr_val_current, "atr_mult_at_entry": "",
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
                "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "trade_id": trades[symbol]["trade_id"],
                "symbol": symbol,
                "event": "SELL",
                "strategy": "aggressive",
                "version": BOT_VERSION,
                "entry": entry, "exit": price, "price": price, "pnl_pct": gain,
                "position_pct": trades[symbol]["position_pct"],
                "sl_initial": trades[symbol]["sl_initial"],
                "sl_final": trades[symbol]["stop"],
                "atr_1h": atr_val_current, "atr_mult_at_entry": "",
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
                "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
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
                "atr_mult_at_entry": "",
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
        trailing_stop_advanced(symbol, price, atr_value=atr_val_current)
        log_trade(symbol, "HOLD", price)

    except Exception as e:
        print(f"❌ Erreur stratégie agressive {symbol}: {e}")
        traceback.print_exc()

def is_recent(ts_str):
    ts = _parse_dt_flex(ts_str)  # "YYYY-MM-DD HH:MM[:SS]"
    if not ts:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() <= 86400

async def send_daily_summary():
    if not history:
        return
    recent = [h for h in history if is_recent(h.get("time", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))]
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
    await tg_send(f"🚀 Bot démarré {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
    global trades
    trades.update(load_trades())
    # Hydratation correcte de last_trade_time depuis les trades persistés (UTC aware)
    for _sym, _t in trades.items():
        try:
            ts = _t.get("time")  # ex: "2025-02-14 13:47"
            if not ts:
                continue
            # 1) essaie parse tolérant (aware UTC grâce au patch de _parse_dt_flex)
            dt = _parse_dt_flex(ts)
            # 2) sinon tente ISO
            if dt is None:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            # 3) stocke en UTC
            last_trade_time[_sym] = dt.astimezone(timezone.utc)
        except Exception:
            # ignore les timestamps illisibles
            pass

    last_heartbeat = None
    last_summary_day = None
    last_audit_day = None   

    while True:
        try:
            now = datetime.now(timezone.utc)

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
                await asyncio.sleep(0.08)  # ~80 ms entre requêtes

            # Contexte marché (on alimente market_cache avec le 1h préchargé)
            market_cache['BTCUSDT'] = symbol_cache.get('BTCUSDT', {}).get('1h', [])
            market_cache['ETHUSDT'] = symbol_cache.get('ETHUSDT', {}).get('1h', [])
            update_market_state()

            # Contexte marché (pré-calcul perf)
            try:
                for sym, key in [("BTCUSDT","btc"), ("ETHUSDT","eth")]:
                    k = market_cache.get(sym, [])
                    if k and len(k) >= 30:
                        closes = [float(x[4]) for x in k]
                        MARKET_STATE[key]["rsi"]    = rsi_tv(closes, 14)
                        m, s = compute_macd(closes)
                        MARKET_STATE[key]["macd"]   = m
                        MARKET_STATE[key]["signal"] = s
                        MARKET_STATE[key]["adx"]    = adx_tv(k, 14)
                        MARKET_STATE[key]["up"]     = is_uptrend(closes)
                    else:
                        # valeurs neutres (ne bloquent pas)
                        MARKET_STATE[key] = {"rsi":50, "macd":0, "signal":0, "adx":20, "up":True}
            except Exception:
                pass


            # Lancement des analyses
            # 1) D’abord la stratégie standard
            await asyncio.gather(*(process_symbol(s) for s in SYMBOLS))
            # 2) Ensuite la stratégie agressive, mais seulement si pas déjà en trade
            await asyncio.gather(*(process_symbol_aggressive(s) for s in SYMBOLS if s not in trades))

            print("✔️ Itération terminée", flush=True)


        except Exception as e:
            await tg_send(f"⚠️ Erreur : {e}")

        await asyncio.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_loop())
