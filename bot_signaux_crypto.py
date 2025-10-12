import asyncio
import requests
import numpy as np
from datetime import datetime, timezone, timedelta
# === Date parser tolérant (UTC-aware) ===
def _parse_dt_flex(s: str):
    """
    Accepte 'YYYY-MM-DD HH:MM' ou 'YYYY-MM-DD HH:MM:SS' (naïf -> UTC),
    ou ISO8601 avec/ss timezone. Retourne un datetime aware UTC, sinon None.
    """
    if not s:
        return None
    s = s.strip()
    fmt_try = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
    for fmt in fmt_try:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

from telegram import Bot
import nest_asyncio
import traceback
from dotenv import load_dotenv; load_dotenv()
import csv
import os, json
import threading
# [#imports-retry]
# [#imports-ratelimit]
import time
import random
import threading
from telegram.error import RetryAfter, TimedOut, NetworkError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import os, json, csv

# Récupérer les variables depuis Render (Environment Variables)
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
DATA_DIR = os.getenv("DATA_DIR", "/var/data")

# S’assurer que le dossier existe (utile avec Render Disks)
os.makedirs(DATA_DIR, exist_ok=True)

# Fichiers persistants
PERSIST_FILE     = os.path.join(DATA_DIR, "trades_state.json")
CSV_AUDIT_FILE   = os.path.join(DATA_DIR, "trade_audit.csv")
HISTORY_FILE     = os.path.join(DATA_DIR, "history.json")
file_lock = threading.Lock()
REFUSAL_LOG_FILE = os.path.join(DATA_DIR, "refusal_log.csv")
LOG_FILE         = os.path.join(DATA_DIR, "trade_log.csv")


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

# --- versions async (non bloquantes) ---
SESSION_LOCK = threading.Lock()
def _binance_get_sync(path, params=None):
    base = BINANCE_BASES[_base_idx]; url = f"{base}{path}"
    with SESSION_LOCK:
        return SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)

        
async def binance_get_async(path, params=None, max_tries=6):
    for attempt in range(max_tries):
        base = BINANCE_BASES[_base_idx]
        url = f"{base}{path}"
        try:
            resp = await asyncio.to_thread(_binance_get_sync, path, params)
            if resp.status_code in (418,429,403,500,502,503,504):
                _rotate_base()
                await asyncio.sleep(min(0.5 * (2 ** attempt), 5.0) + random.uniform(0.05, 0.25))
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            _rotate_base()
            await asyncio.sleep(min(0.5 * (2 ** attempt), 5.0) + random.uniform(0.05, 0.25))
    return None

async def get_klines_async(symbol, interval='1h', limit=100):
    return await binance_get_async(
        "/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit}
    ) or []


SYMBOLS = [
    'BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT',
    'XRPUSDT','ADAUSDT','LINKUSDT','AVAXUSDT',
    'DOTUSDT','DOGEUSDT'
]
INTERVAL = '1h'
LIMIT = 100
TF_LIST = [("1h", 750), ("4h", 300), ("15m", 200), ("5m", 120)]
SLEEP_SECONDS = 300
MAX_TRADES = 7
MIN_VOLUME = 600000
COOLDOWN_HOURS = 2
VOL_MED_MULT = 0.05 # Tolérance volume vs médiane 30j (était 0.25)
VOL_CONFIRM_TF = "15m"
VOL_CONFIRM_LOOKBACK = 12
VOL_CONFIRM_MULT = 1.00
ANTI_SPIKE_UP_STD = 0.8   # 0.8% mini (std)
ANTI_SPIKE_UP_AGR = 3.0  # au lieu de 2.4
# --- Anti-spike adaptatif (bonus) ---
# plus tolérant : accepte des extensions intraday raisonnables
ANTI_SPIKE_ATR_MULT = 2.40
ANTI_SPIKE_MAX_PCT  = 7.00   # au lieu de 5.00
# ===== Learning phase (assouplissements) =====
LEARNING_MODE = True

ADX_MIN = 15                 # au lieu de 18/22
RSI_MIN = 50                 # RSI >= 50 accepté
RSI_MAX = 65                 # RSI <= 65 accepté
VOL15M_MIN_RATIO = 0.50  # assoupli : 0.50
ANTI_SPIKE_OPEN_MAX = 0.035  # 3.5% max vs open (1h)

# --- Trailing stop harmonisé (ATR TV) ---
TRAIL_TIERS = [
    (1.8, 0.9),   # dès +1.8% ⇒ stop = P - 0.9*ATR
    (3.5, 0.6),
    (6.0, 0.45),
]
TRAIL_BE_AFTER = 1.5  # lock BE dès ~TP1 (≥ +1.5%)
# --- Take-profits dynamiques (multiplicateurs d'ATR) ---
TP_ATR_MULTS_STD = [1.0, 2.0, 3.0]      # standard : TP1=1×ATR, TP2=2×ATR, TP3=3×ATR
TP_ATR_MULTS_AGR = [1.0, 2.0, 3.0]      # aggressive (modifiable si besoin)
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

from typing import Final
MAJORS: Final = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"}

def allowed_trade_slots(strategy: str | None = None) -> int:
    """
    Nombre de positions autorisées en parallèle.
    - base = 3 (au lieu de 1)
    - +1 slot pour chaque trade "confiant" (confidence >= 8), max 7.
    - si `strategy` est fourni, on ne compte que cette stratégie.
    """
    base = 3
    try:
        high = sum(
            1 for t in trades.values()
            if (strategy is None or t.get("strategy") == strategy)
            and float(t.get("confidence", 0)) >= 8
        )
    except Exception:
        high = 0
    return min(7, base + high)

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

from telegram.request import HTTPXRequest

def pnl_7d_pct_utc() -> float:
    """
    Somme des P&L (%) des trades clôturés sur les 7 derniers jours (UTC).
    Utilise `history` (persisté).
    """
    if not history:
        return 0.0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    total = 0.0
    for h in history:
        ts = _parse_dt_flex(h.get("time", ""))
        if not ts:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        if ts >= cutoff:
            try:
                total += float(h.get("result", 0.0))
            except Exception:
                pass
    return total

def perf_cap_max_trades(strategy: str) -> int:
    """
    Cap 'machine à sous':
    - Si P&L 7j < 0  → cap bas (prudence)
      standard: 1   | aggressive: 1
    - Si P&L 7j ≥ 0 → cap haut
      standard: 3   | aggressive: 4
    """
    pnl7 = pnl_7d_pct_utc()
    if pnl7 < 0:
        return 1 if strategy == "standard" else 1
    else:
        return 3 if strategy == "standard" else 4

def build_tg_bot(connect=20, read=60, pool=20):
    req = HTTPXRequest(connect_timeout=connect, read_timeout=read, pool_timeout=pool)
    return Bot(token=TELEGRAM_TOKEN, request=req)

bot = build_tg_bot()


trades = {}
history = []
market_cache = {}
last_trade_time = {}
btc_block_until  = None   # datetime UTC jusqu’à laquelle on bloque les alts
btc_block_reason = ""     # mémo de la raison (pour logs)
# === Cache par itération pour limiter les requêtes ===
symbol_cache = {}  # {"BTCUSDT": {"1h": [...], "4h": [...], "15m": [...], "5m": [...]} }
hold_buffer = {}


def load_trades():
    try:
        with open(PERSIST_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_trades():
    try:
        with file_lock:
            serializable = {}
            for sym, t in trades.items():
                d = dict(t)
                if "tp_times" in d:
                    d["tp_times"] = {k: str(v) for k,v in d["tp_times"].items()}
                serializable[sym] = d
            with open(PERSIST_FILE, "w") as f:
                json.dump(serializable, f)
    except Exception as e:
        print(f"⚠️ save_trades: {e}")

def load_history():
    try:
        with open(HISTORY_FILE, "r") as f:
            data = json.load(f)
        # sécurité : liste de dicts
        return data if isinstance(data, list) else []
    except Exception:
        return []

def save_history():
    try:
        with file_lock:
            with open(HISTORY_FILE, "w") as f:
                json.dump(history, f)
    except Exception as e:
        print(f"⚠️ save_history: {e}")

def log_trade_csv(row: dict):
    header_needed = (not os.path.exists(CSV_AUDIT_FILE) or os.path.getsize(CSV_AUDIT_FILE) == 0)
    with file_lock:
        with open(CSV_AUDIT_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_AUDIT_FIELDS)
            if header_needed: 
                w.writeheader()
            clean = {k: row.get(k, "") for k in CSV_AUDIT_FIELDS}
            if not clean.get("ts_utc"):
                clean["ts_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            w.writerow(clean)

async def send_refusal_top(n_minutes=60, topk=8):
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=n_minutes)
        counts = {}
        if not os.path.exists(REFUSAL_LOG_FILE):
            return
        with open(REFUSAL_LOG_FILE) as f:
            r = csv.DictReader(f)
            for row in r:
                ts = _parse_dt_flex(row.get("ts_utc", ""))
                if ts and ts >= cutoff:
                    key = row.get("reason", "")
                    counts[key] = counts.get(key, 0) + 1
        if counts:
            top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:topk]
            lines = [f"• {k}: {v}" for k, v in top]
            await tg_send("🧪 Top refus (dernière heure):\n" + "\n".join(lines))
    except Exception as e:
        print("top refus err:", e)

def _delete_trade(symbol):
    if symbol in trades:
        del trades[symbol]
        save_trades()

def get_cached(symbol, tf="1h", limit=None, force: bool=False):
    return symbol_cache.get(symbol, {}).get(tf, [])

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
async def buffer_hold(symbol: str, text: str):
    # on stocke le message (tronqué proprement)
    hold_buffer.setdefault(symbol, []).append(safe_message(text))

def safe_message(text) -> str:
    """
    Raccourcit proprement les messages trop longs pour Telegram.
    Telegram limite ≈ 4096 caractères pour le champ 'text'.
    """
    try:
        s = str(text)
    except Exception:
        s = repr(text)
    return s if len(s) < 4000 else s[:3900] + "\n... (tronqué)"


async def tg_send(text: str, chat_id: int = CHAT_ID):
    """
    Envoi Telegram robuste avec gestion explicite :
    - RetryAfter : on respecte e.retry_after (sec)
    - TimedOut / NetworkError : backoff exponentiel doux
    - Autres erreurs : backoff puis retry limité
    """
    text = safe_message(text)
    max_tries = 6
    base = 0.8  # backoff de base

    for attempt in range(1, max_tries + 1):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return True
        except RetryAfter as e:
            wait_s = getattr(e, "retry_after", 3)
            print(f"[tg_send] RetryAfter: wait {wait_s}s (try#{attempt}/{max_tries})")
            await asyncio.sleep(float(wait_s) + 0.25)
        except TimedOut:
            wait_s = min(8, base * (2 ** (attempt - 1)))
            print(f"[tg_send] TimedOut: retry in {wait_s:.1f}s (try#{attempt}/{max_tries})")
            await asyncio.sleep(wait_s)
        except NetworkError as e:
            wait_s = min(10, base * (2 ** (attempt - 1)))
            print(f"[tg_send] NetworkError: {e} → retry in {wait_s:.1f}s (try#{attempt}/{max_tries})")
            await asyncio.sleep(wait_s)
        except Exception as e:
            wait_s = min(12, base * (2 ** (attempt - 1)))
            print(f"[tg_send] Exception: {e} → retry in {wait_s:.1f}s (try#{attempt}/{max_tries})")
            await asyncio.sleep(wait_s)

    print("[tg_send] échec après retries")
    return False

async def tg_send_doc(path: str, caption: str = "", chat_id: int = CHAT_ID):
    """Envoi simple de fichier Telegram (sans anti-flood)."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            await tg_send(f"ℹ️ Fichier introuvable ou vide: {path}")
            return
        with open(path, "rb") as f:
            await bot.send_document(chat_id=chat_id, document=f, caption=safe_message(caption)[:1024])
    except Exception as e:
        print(f"❌ tg_send_doc error: {e}")


async def ensure_tg_ready(max_wait_s: int = 120) -> bool:
    """
    Essaie d'envoyer un ping Telegram jusqu'à succès (limité à max_wait_s).
    Recrée la session HTTP entre les tentatives (utile si le pool est froid).
    """
    start = time.monotonic()
    attempt = 0
    while (time.monotonic() - start) < max_wait_s:
        attempt += 1
        try:
            # on recrée le client à chaque tentative pour repartir propre
            global bot
            bot = build_tg_bot()

            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            await bot.send_message(chat_id=CHAT_ID, text=f"🧪 Ping (try#{attempt}) {ts}")
            print(f"✅ Telegram prêt (try#{attempt})")
            return True
        except Exception as e:
            left = max_wait_s - int(time.monotonic() - start)
            print(f"⏳ Ping TG échec try#{attempt}: {e} — {left}s restants")
            # backoff doux 2, 3, 5, 8, 13...
            delay = [2,3,5,8,13,13,13][min(attempt-1, 6)]
            await asyncio.sleep(delay)
    print("⚠️ Telegram non joignable dans la fenêtre d’amorçage")
    return False



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

def is_bull_structure_15m(highs, lows, n=3, tol=0.001):
    """
    Structure haussière tolérante :
    - On demande n bougies complètes, mais on accepte des plateaux (>=) avec marge 'tol'
    - Suffit que TOUTES les comparaisons soient non-bear (>= (1 - tol))
    """
    if len(highs) < n or len(lows) < n:
        return False
    hh = highs[-n:]
    hl = lows[-n:]
    hh_up = all(hh[i+1] >= hh[i] * (1 - tol) for i in range(n-1))
    hl_up = all(hl[i+1] >= hl[i] * (1 - tol) for i in range(n-1))
    return hh_up and hl_up

def check_15m_filter(k15, breakout_level=None, n_struct=3, tol_struct=0.0015):
    """
    Vérifie le filtre 15m avant achat (tolérant).
    - EMA20 pente positive
    - Structure haussière tolérante (n_struct = 2 sur majors / ADX fort)
    - Close15m > EMA20(15m)
    - Si breakout_level: accepte close >= level * (1 - tol_struct)
    """
    if not k15 or len(k15) < 25:
        return False, "Données 15m insuffisantes"

    k = k15[:-1] if len(k15) >= 2 else k15
    closes = [float(x[4]) for x in k]
    highs  = [float(x[2]) for x in k]
    lows   = [float(x[3]) for x in k]

    ema20_series = ema_tv_series(closes, 20)
    if len(ema20_series) < 2:
        return False, "EMA20(15m) insuffisante"

    ema20_up  = ema20_series[-1] > ema20_series[-2]
    close_ok  = closes[-1] >= ema20_series[-1]
    struct_ok = is_bull_structure_15m(highs, lows, n=n_struct, tol=tol_struct)
    if breakout_level is not None:
        retest_ok = closes[-1] >= breakout_level * (1 - tol_struct)
    else:
        retest_ok = True

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

def check_spike_and_wick(symbol: str, klines_1h, price: float, mode_label="std") -> bool:
    """
    Combine anti-spike 1h et mèche haute dominante dans un seul log.
    Retourne True si OK, False si refus.
    Ne modifie plus indicators_soft_penalty (soft-only).
    """
    try:
        ok_spike, spike_up_pct, limit_pct = anti_spike_check_std(klines_1h, price)
        wick = is_wick_hunt_1h(klines_1h[-1]) if klines_1h else False

        if ok_spike and not wick:
            return True

        # Spike légèrement au-dessus du seuil → on laisse passer (log seulement)
        if not ok_spike:
            if spike_up_pct <= (limit_pct + 0.8) or symbol in MAJORS:
                log_refusal(
                    symbol,
                    "Anti-excès 1h toléré (soft)",
                    trigger=f"spike={spike_up_pct:.2f}%>seuil={limit_pct:.2f}% (+tol)"
                )
                return True
            else:
                reasons = [f"spike={spike_up_pct:.2f}%>seuil={limit_pct:.2f}%"]
        else:
            reasons = []

        if wick:
            reasons.append("mèche_haute_dominante")

        # Log uniquement, pas de pénalité de score
        log_refusal(symbol, "Anti-excès 1h (soft, non bloquant)", trigger=" | ".join(reasons))
        return True  # soft: on continue quand même

    except Exception:
        # fail-open
        return True

        
    except Exception as _:
        # En cas d’erreur, on ne bloque pas (fail–open)
        return True

def confirm_15m_after_signal(symbol, breakout_level=None, ema25_1h=None):
    # exiger les 2 dernières bougies 15m complètes
    k15 = get_cached(symbol, "15m", 20)
    if len(k15) < 3:
        return False
    closes = [float(k15[-2][4]), float(k15[-3][4])]
    ok_level = (breakout_level is not None) and all(c >= breakout_level for c in closes)
    ok_ema   = (ema25_1h is not None) and all(c >= ema25_1h * 0.999 for c in closes)
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
    if len(volumes) < 15:
        return False  # sécurité basique

    vol5 = np.mean(volumes[-5:])
    vol10 = np.mean(volumes[-10:-5])
    vol20 = np.mean(volumes[-20:-5]) if len(volumes) >= 20 else vol10

    # ✅ Adouci : ratio volume actuel vs historique
    ratio = vol5 / max(vol20, 1e-9)
    return (ratio >= 0.85) or (vol5 > vol10)

def is_market_bullish():
    """
    On ne bloque plus globalement. On renvoie toujours True,
    et on laisse les pénalités agir dans le scoring.
    """
    return True
    
def btc_regime_blocked():
    """
    PANIC ONLY + cooldown court + exemption majors.
    Bloque les ALTS seulement si chute forte et récente du BTC.
    """
    global btc_block_until, btc_block_reason

    MAJORS = {"BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT"}
    k1h = market_cache.get("BTCUSDT", [])
    if not k1h or len(k1h) < 10:
        return False, ""

    closes = [float(k[4]) for k in k1h]
    drop1h = (closes[-1] - closes[-2]) / max(closes[-2], 1e-9) * 100.0
    drop3h = ((closes[-1] - closes[-4]) / max(closes[-4], 1e-9) * 100.0) if len(closes) >= 4 else 0.0

    # PANIC ONLY (plus dur que chez toi)
    bear_now = (drop1h <= -2.0) or (drop3h <= -4.5)

    now = datetime.now(timezone.utc)

    # lever le cooldown si petit rebond
    if btc_block_until and now < btc_block_until:
        rebound = drop1h >= 0.6  # +0.6% en 1h
        if rebound and not bear_now:
            btc_block_until = None
            btc_block_reason = ""
            return False, ""
        else:
            mins = (btc_block_until - now).total_seconds() / 60.0
            return True, f"cooldown BTC {mins:.0f} min restant — {btc_block_reason}"

    if bear_now:
        btc_block_until  = now + timedelta(minutes=45)  # 90 → 45 min
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

   # Patch 5 — 01:00–02:00 UTC
    # On évite de bloquer inutilement les majors
    if 1 <= h < 2:
        if symbol in HIGH_LIQ:
            # BTC, ETH, BNB, SOL… passent toujours
            return True, "HIGH_LIQ_01_02"
        else:
            # autres paires → soft block
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
    
# garde-fous globaux
# --- Money management (risk-based) ---
POS_MIN_PCT = 1.0
POS_MAX_PCT = 7.0

def min_vol_threshold(symbol: str) -> float:
    """Seuil ATR/price minimal par catégorie."""
    MAJORS = {"BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT"}
    if symbol in MAJORS:
        return 0.0015   # 0.15% pour majors
    return 0.0020       # 0.20% pour alts

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

# --- Trailing ADX adaptatif ---

def trail_factor_from_adx(adx: float) -> float:
    """
    Retourne le coefficient (k * ATR) pour le trailing.
    Tendance forte => trailing plus serré ; faible => plus large.
    """
    if adx is None:
        return 0.9
    if adx >= 28:
        return 0.70   # tendance très forte -> serré
    if adx >= 22:
        return 0.85   # tendance correcte -> normal/serré
    return 1.10       # tendance faible -> large

TRAIL_TIERS_BASE = [
    (1.8, 1.0),   # seuil en % de gain, coeff de base (sera multiplié par k_adx)
    (3.5, 0.7),
    (6.0, 0.55),
]

# [#fast-exit-5m]
def _last_two_finite(values):
    arr = np.asarray(values, dtype=float)
    finite = arr[~np.isnan(arr)]
    if finite.size >= 2:
        return float(finite[-2]), float(finite[-1])
    return float('nan'), float('nan')

def is_bearish_engulfing_5m(k5):
    """
    Englobante baissière sur 5m en utilisant les 2 DERNIÈRES bougies COMPLÈTES.
    Conditions :
      - bougie -2 verte (close > open)
      - bougie -1 rouge (close < open)
      - open(-1) >= close(-2) ET close(-1) <= open(-2)
    """
    if not k5 or len(k5) < 3:
        return False
    a = k5[-3]  # bougie -2 (complète)
    b = k5[-2]  # bougie -1 (complète)
    open_a, close_a = float(a[1]), float(a[4])
    open_b, close_b = float(b[1]), float(b[4])
    return (close_a > open_a) and (close_b < open_b) and (open_b >= close_a) and (close_b <= open_a)

def fast_exit_5m_trigger(symbol: str, entry: float, current_price: float):
    """
    Sortie dynamique (timeframe 5m) :
    - Si gain >= +0.8% ET (RSI(5m) chute > 5 pts OU MACD croise baissier OU bearish engulfing 5m) -> True
    Retourne (trigger: bool, info: dict)
    """
    try:
        if entry <= 0 or current_price is None:
            return False, {}
        gain_pct = ((current_price - entry) / entry) * 100.0
        if gain_pct < 0.8:
            return False, {"gain_pct": gain_pct}

        k5 = get_cached(symbol, '5m', limit=60)
        if not k5 or len(k5) < 20:
            return False, {"gain_pct": gain_pct}

        # On travaille sur les bougies COMPLÈTES
        closes5 = [float(k[4]) for k in k5[:-1]] if len(k5) >= 2 else [float(k5[-1][4])]

        # RSI(5m) : chute entre les 2 dernières clôtures complètes
        rsi5_series = rsi_tv_series(closes5, period=14)
        rsi_prev, rsi_now = _last_two_finite(rsi5_series)
        rsi_drop = (not np.isnan(rsi_prev) and not np.isnan(rsi_now) and (rsi_prev - rsi_now) > 5.0)

        # MACD(5m) : croisement baissier récent (entre -2 et -1 complètes)
        macd_now,  signal_now  = compute_macd(closes5)
        macd_prev, signal_prev = compute_macd(closes5[:-1])
        macd_cross_down = (macd_prev >= signal_prev) and (macd_now < signal_now)

        # Bearish engulfing 5m (sur les 2 dernières bougies complètes)
        bearish_5m = is_bearish_engulfing_5m(k5)

        trigger = gain_pct >= 0.8 and (rsi_drop or macd_cross_down or bearish_5m)
        return trigger, {
            "gain_pct": gain_pct,
            "rsi5_prev": rsi_prev, "rsi5_now": rsi_now,
            "rsi_drop": (rsi_prev - rsi_now) if (not np.isnan(rsi_prev) and not np.isnan(rsi_now)) else None,
            "macd5": macd_now, "signal5": signal_now,
            "macd5_prev": macd_prev, "signal5_prev": signal_prev,
            "macd_cross_down": macd_cross_down,
            "bearish_engulfing_5m": bearish_5m
        }
    except Exception:
        return False, {}  


def detect_breakout_retest(closes, highs, lookback=10, tol=0.0015):
    """
    Détecte Breakout + Retest en 1h (version assouplie).
    - Breakout (bougie -2) : close >= plus_haut_lookback * (1 + tol) 
      OU (close>=level ET close(-1)>=level)  ← breakout "quasi-plat" accepté
    - Retest (bougie -1) : |close(-1) - level| / level <= tol * 1.2  OU close(-1) >= level
    Retourne (ok: bool, level: float)
    """
    if len(highs) < lookback + 3 or len(closes) < lookback + 3:
        return False, None

    # plus haut avant la bougie -2
    level = max(highs[-(lookback+2):-2])

    c_m2 = closes[-2]
    c_m1 = closes[-1]

    # breakout toléré (marge allégée + cas quasi-plat)
    breakout = (c_m2 >= level * (1.0 + tol)) or ((c_m2 >= level) and (c_m1 >= level))

    # retest toléré (un poil plus large) ou continuation au-dessus du level
    retest = (abs(c_m1 - level) / max(level, 1e-9) <= tol * 1.2) or (c_m1 >= level)

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
        # ✅ persist
        save_history()
        
# ====== CSV détaillé (audit) ======
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

REFUSAL_FIELDS = ["ts_utc", "symbol", "reason", "trigger", "cooldown_left_min"]

# — Création des CSV au démarrage si absents —
import os
import csv

def _ensure_csv(path: str, header: list[str]):
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)

# crée/refait l'en-tête si les fichiers n'existent pas
_ensure_csv(CSV_AUDIT_FILE, CSV_AUDIT_FIELDS)
_ensure_csv(REFUSAL_LOG_FILE, REFUSAL_FIELDS)


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

# === Finalisation de sortie (unique & idempotente) ===
def _finalize_exit(symbol, exit_price, pnl_pct, reason, event_name, ctx):
    """
    Clôture propre d'une position:
    - envoie le message (STOP ou EXIT),
    - écrit le CSV d'audit,
    - pousse dans l'historique,
    - supprime le trade de `trades` (et persiste).
    ctx peut contenir: rsi, macd, signal, adx, atr, st_on, ema25, ema200, ema50_4h, ema200_4h,
                       vol5, vol20, vol_ratio, btc_up, eth_up, elapsed_h.
    """
    try:
        trade = trades.get(symbol)
        if not trade:
            return  # déjà supprimé

        # 1) Message unique
        if event_name == "STOP":
            msg = format_stop_msg(
                symbol, trade["trade_id"], trade.get("stop", exit_price),
                pnl_pct, ctx.get("rsi", 0), ctx.get("adx", 0), ctx.get("vol_ratio", 0)
            )
        else:
            msg = format_exit_msg(
                symbol, trade["trade_id"], exit_price, pnl_pct,
                trade.get("stop", exit_price), ctx.get("elapsed_h", 0), reason
            )
        # on ne bloque pas la loop si tg_send est async
        asyncio.create_task(tg_send(msg))

        # 2) CSV d'audit
        log_trade_csv({
            "ts_utc": utc_now_str(),
            "trade_id": trade["trade_id"], "symbol": symbol,
            "event": event_name, "strategy": trade.get("strategy", "standard"),
            "version": BOT_VERSION,
            "entry": trade.get("entry", ""), "exit": exit_price, "price": exit_price, "pnl_pct": pnl_pct,
            "position_pct": trade.get("position_pct", ""),
            "sl_initial": trade.get("sl_initial", ""), "sl_final": trade.get("stop", ""),
            "atr_1h": ctx.get("atr", ""), "atr_mult_at_entry": "",
            "rsi_1h": ctx.get("rsi", ""), "macd": ctx.get("macd", ""), "signal": ctx.get("signal", ""),
            "adx_1h": ctx.get("adx", ""),
            "supertrend_on": ctx.get("st_on", ""),
            "ema25_1h": ctx.get("ema25", ""), "ema200_1h": ctx.get("ema200", ""),
            "ema50_4h": ctx.get("ema50_4h", ""), "ema200_4h": ctx.get("ema200_4h", ""),
            "vol_ma5": ctx.get("vol5", ""), "vol_ma20": ctx.get("vol20", ""),
            "vol_ratio": ctx.get("vol_ratio", ""),
            "btc_uptrend": ctx.get("btc_up", ""), "eth_uptrend": ctx.get("eth_up", ""),
            "reason_entry": trade.get("reason_entry", ""), "reason_exit": reason
        })

        # 3) Historique + suppression
        log_trade(symbol, "SELL", exit_price, pnl_pct)   # ajoute à history + save_history()
        _delete_trade(symbol)                             # supprime dans trades + save_trades()

    except Exception:
        traceback.print_exc()

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

def btc_is_bullish_strong() -> bool:
    """
    True si BTC(1h) est franchement haussier :
    RSI > 50, MACD > Signal, ADX > 22.
    Retourne False si info manquante.
    """
    try:
        b = MARKET_STATE["btc"]
        req = (b["rsi"], b["macd"], b["signal"], b["adx"])
        if any(v is None for v in req):
            return False
        return (b["rsi"] > 50) and (b["macd"] > b["signal"]) and (b["adx"] > 22)
    except Exception:
        return False

def btc_market_drift() -> bool:
    """
    True si BTC est en dérive baissière en 1h :
    - close < EMA200(1h)
    - MACD < signal
    Utilisé pour bloquer les *ALTS* uniquement (pas BTC).
    """
    k = market_cache.get("BTCUSDT", [])
    if not k or len(k) < 210:
        return False
    closes = [float(x[4]) for x in k]
    ema200_btc = ema_tv(closes, 200)
    macd_btc, signal_btc = compute_macd(closes)
    return (closes[-1] < ema200_btc) and (macd_btc < signal_btc)

# === Relative strength ALT vs BTC (autorise si l'ALT surperforme) ===
def rel_strength_vs_btc(symbol, klines_1h_alt=None, lookback=3):
    k_alt = klines_1h_alt or get_cached(symbol, '1h')
    k_btc = market_cache.get('BTCUSDT', [])
    if not k_alt or not k_btc or len(k_alt) < lookback+1 or len(k_btc) < lookback+1:
        return 0.0
    a0, aN = float(k_alt[-lookback-1][4]), float(k_alt[-1][4])
    b0, bN = float(k_btc[-lookback-1][4]), float(k_btc[-1][4])
    alt_chg = (aN - a0) / max(a0, 1e-9)
    btc_chg = (bN - b0) / max(b0, 1e-9)
    return alt_chg - btc_chg  # positif = ALT > BTC

def _nb_trades(strategy=None):
    """
    Compte les trades actifs.
    - strategy=None : tous
    - strategy="standard" ou "aggressive" : seulement ceux de ce type
    """
    return sum(1 for t in trades.values() if strategy is None or t.get("strategy") == strategy)

async def process_symbol(symbol):
    try:
        # [PATCH-COOLDOWN std]
        in_trade = symbol in trades  # pour ne pas bloquer la gestion d'une position déjà ouverte

        # --- Auto-close SOUPLE (ne coupe plus automatiquement à 12h) ---
        if symbol in trades and trades[symbol].get("strategy", "standard") == "standard":
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            elapsed_h = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            # ❌ plus d’auto-BE avant 12h — le BE est géré uniquement par TRAIL_BE_AFTER/TP1
            pass


        # ---------- Analyse standard ----------
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] 🔍 Analyse de {symbol}", flush=True)

        klines = get_cached(symbol, '1h')# 1h
        # --- GUARD: trade ouvert mais pas de données 1h -> gérer en mode minimal ---
        in_trade_std = (symbol in trades) and (trades[symbol].get("strategy", "standard") == "standard")

        if in_trade_std and (not klines or len(klines) < 20):
            price = get_last_price(symbol)
            if price is None:
                await tg_send(f"⚠️ {symbol} en position (standard) mais données 1h indisponibles. Pas d’update.")
                return

            # Lectures sûres
            entry = float(trades[symbol].get("entry", price))
            # stop fallback : stop > sl_initial > petit filet basé sur INIT_SL_PCT_STD_MIN
            stop = float(trades[symbol].get(
                "stop",
                trades[symbol].get("sl_initial", price * (1 - INIT_SL_PCT_STD_MIN))
            ))

            gain = ((price - entry) / max(entry, 1e-9)) * 100.0

            # Parsing d’heure d’entrée tolérant (UTC-aware)
            et = trades[symbol].get("time", "")
            entry_time = _parse_dt_flex(et) or datetime.now(timezone.utc)

            # Sortie de sécurité si stop/perte max touchés
            if price <= stop or gain <= -1.5:
                event = "STOP" if price <= stop else "SELL"
                reason = "Stop (fallback 1h indisponible)" if event == "STOP" else "Perte max (-1.5%) (fallback)"
                ctx = {
                    "rsi": 0.0, "macd": 0.0, "signal": 0.0, "adx": 0.0,
                    "atr": 0.0, "st_on": False,
                    "ema25": 0.0, "ema200": 0.0, "ema50_4h": 0.0, "ema200_4h": 0.0,
                    "vol5": 0.0, "vol20": 0.0, "vol_ratio": 0.0,
                    "btc_up": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_up": MARKET_STATE.get("eth", {}).get("up", False),
                    "elapsed_h": (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600.0
                }
                _finalize_exit(symbol, price, gain, reason, event, ctx)
                return

            # Sinon tenir la position : HOLD minimal (un seul passage)
            log_trade(symbol, "HOLD", price)
            await buffer_hold(
                symbol,
                f"{utc_now_str()} | {symbol} HOLD (fallback) | prix {price:.4f} | "
                f"gain {gain:.2f}% | stop {stop:.4f}"
            )
            return

        # --- Volume 1h vs médiane 30j (robuste & borné) ---
        vol_now_1h = float(klines[-1][7])

        k1h_30d = get_cached(symbol, '1h', limit=750) or []
        vols_hist = volumes_series(k1h_30d, quote=True)[-721:]  # ~30j + current

        VOL_MED_MULT_EFF = (0.05 if symbol in MAJORS else 0.07)
        MIN_VOLUME_ABS   = (80_000 if symbol in MAJORS else 120_000)

        if len(vols_hist) >= 200:
            # borne haute : on limite l'influence de quelques mega-bougies
            med_30d_raw = float(np.median(vols_hist[:-1]))
            p70 = float(np.percentile(vols_hist[:-1], 70))
            med_30d = min(med_30d_raw, p70 * 1.5)  # cap raisonnable

            if symbol != "BTCUSDT" and med_30d > 0 and vol_now_1h < max(MIN_VOLUME_ABS, VOL_MED_MULT_EFF * med_30d):
                log_refusal(symbol, f"Volume 1h trop faible vs med30j (...)")
                if not in_trade:
                    return
        else:
            if vol_now_1h < MIN_VOLUME_ABS:
                log_refusal(symbol, f"Volume 1h trop faible (abs) {vol_now_1h:.0f} < {MIN_VOLUME_ABS}")
                return

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
            if not in_trade:
                return
        # --- Indicateurs (versions TradingView) ---
        rsi = rsi_tv(closes, period=14)
        rsi_series = rsi_tv_series(closes, period=14)
        macd, signal = compute_macd(closes)      # MACD EMA/EMA
        ema200 = ema_tv(closes, 200)
        atr = atr_tv(klines, period=14)
        adx_value = adx_tv(klines, period=14)
        ema25 = ema_tv(closes, 25)
        supertrend_signal = supertrend_like_on_close(klines)

        in_trade = (symbol in trades) and (trades[symbol].get("strategy", "standard") == "standard")
        if in_trade:
            # === GESTION D'UN TRADE OUVERT (STANDARD) ===
            entry = trades[symbol]['entry']
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            elapsed_time = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            price = get_last_price(symbol)  # re-sécu
            gain = ((price - entry) / max(entry, 1e-9)) * 100.0
            stop = trades[symbol].get("stop", trades[symbol].get("sl_initial", price * (1 - INIT_SL_PCT_STD_MIN)))

            # Trailing stop adaptatif (ADX)
            adx_val = trades[symbol].get("adx_1h", 20)
            trail_multiplier = 0.3 if adx_val >= 25 else (0.5 if adx_val >= 20 else 0.8)
            new_stop = price * (1 - trail_multiplier * atr / max(price, 1e-9))
            trades[symbol]["stop"] = max(stop, new_stop)

            # ====== 1) Filtre régime BTC (sortie anticipée légère) ======
            blocked, _why_btc = btc_regime_blocked()
            if blocked and gain < 0.8:
                vol5_loc  = float(np.mean(volumes[-5:]))  if len(volumes) >= 5  else 0.0
                vol20_loc = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
                ctx = {
                    "rsi": rsi, "macd": macd, "signal": signal, "adx": adx_value,
                    "atr": atr, "st_on": supertrend_signal, "ema25": ema25, "ema200": ema200,
                    "ema50_4h": ema_tv([float(x[4]) for x in get_cached(symbol,'4h')], 50) if get_cached(symbol,'4h') else 0.0,
                    "ema200_4h": ema_tv([float(x[4]) for x in get_cached(symbol,'4h')], 200) if get_cached(symbol,'4h') else 0.0,
                    "vol5": vol5_loc, "vol20": vol20_loc,
                    "vol_ratio": (vol5_loc / max(vol20_loc, 1e-9)) if vol20_loc else 0.0,
                    "btc_up": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_up": MARKET_STATE.get("eth", {}).get("up", False),
                    "elapsed_h": elapsed_time
                }
                _finalize_exit(symbol, price, gain, "BTC regime turned negative", "SELL", ctx)
                return

            # ====== 2) Timeout intelligent ======
            k1h_now = get_cached(symbol, '1h')
            trig, why = smart_timeout_check(k1h_now, entry,
                                            window_h=SMART_TIMEOUT_WINDOW_H,
                                            range_pct=SMART_TIMEOUT_RANGE_PCT_STD)
            if trig and gain < SMART_TIMEOUT_MIN_GAIN_STD and not trades[symbol].get("tp1", False) and not btc_is_bullish_strong():
                vol5_loc  = float(np.mean(volumes[-5:]))  if len(volumes) >= 5  else 0.0
                vol20_loc = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
                ctx = {
                    "rsi": rsi, "macd": macd, "signal": signal, "adx": adx_value,
                    "atr": atr, "st_on": supertrend_signal, "ema25": ema25, "ema200": ema200,
                    "ema50_4h": ema_tv([float(x[4]) for x in get_cached(symbol,'4h')], 50) if get_cached(symbol,'4h') else 0.0,
                    "ema200_4h": ema_tv([float(x[4]) for x in get_cached(symbol,'4h')], 200) if get_cached(symbol,'4h') else 0.0,
                    "vol5": vol5_loc, "vol20": vol20_loc,
                    "vol_ratio": (vol5_loc / max(vol20_loc, 1e-9)) if vol20_loc else 0.0,
                    "btc_up": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_up": MARKET_STATE.get("eth", {}).get("up", False),
                    "elapsed_h": elapsed_time
                }
                _finalize_exit(symbol, price, gain, f"Timeout intelligent (standard): {why}", "SMART_TIMEOUT", ctx)
                return

            # ====== 3) Sortie dynamique 5m ======
            triggered, fx = fast_exit_5m_trigger(symbol, entry, price)
            if triggered:
                vol5_loc  = float(np.mean(volumes[-5:]))  if len(volumes) >= 5  else 0.0
                vol20_loc = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
                bits = []
                if fx.get("rsi_drop") is not None and fx["rsi_drop"] > 5: bits.append(f"RSI(5m) -{fx['rsi_drop']:.1f} pts")
                if fx.get("macd_cross_down"): bits.append("MACD(5m) croisement baissier")
                raison = "Sortie dynamique 5m: " + " ; ".join(bits) if bits else "Sortie dynamique 5m"
                ctx = {
                    "rsi": rsi, "macd": macd, "signal": signal, "adx": adx_value,
                    "atr": atr, "st_on": supertrend_signal, "ema25": ema25, "ema200": ema200,
                    "ema50_4h": ema_tv([float(x[4]) for x in get_cached(symbol,'4h')], 50) if get_cached(symbol,'4h') else 0.0,
                    "ema200_4h": ema_tv([float(x[4]) for x in get_cached(symbol,'4h')], 200) if get_cached(symbol,'4h') else 0.0,
                    "vol5": vol5_loc, "vol20": vol20_loc,
                    "vol_ratio": (vol5_loc / max(vol20_loc, 1e-9)) if vol20_loc else 0.0,
                    "btc_up": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_up": MARKET_STATE.get("eth", {}).get("up", False),
                    "elapsed_h": elapsed_time
                }
                _finalize_exit(symbol, price, gain, raison, "DYN_EXIT_5M", ctx)
                return

            # ====== 4) Momentum cassé ======
            if gain < 0.8 and (rsi < 48 or macd < signal):
                vol5_loc  = float(np.mean(volumes[-5:]))  if len(volumes) >= 5  else 0.0
                vol20_loc = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
                ctx = {
                    "rsi": rsi, "macd": macd, "signal": signal, "adx": adx_value,
                    "atr": atr, "st_on": supertrend_signal, "ema25": ema25, "ema200": ema200,
                    "ema50_4h": ema_tv([float(x[4]) for x in get_cached(symbol,'4h')], 50) if get_cached(symbol,'4h') else 0.0,
                    "ema200_4h": ema_tv([float(x[4]) for x in get_cached(symbol,'4h')], 200) if get_cached(symbol,'4h') else 0.0,
                    "vol5": vol5_loc, "vol20": vol20_loc,
                    "vol_ratio": (vol5_loc / max(vol20_loc, 1e-9)) if vol20_loc else 0.0,
                    "btc_up": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_up": MARKET_STATE.get("eth", {}).get("up", False),
                    "elapsed_h": elapsed_time
                }
                _finalize_exit(symbol, price, gain, "Momentum cassé (sortie anticipée)", "SELL", ctx)
                return

            # ====== 5) Perte de momentum après TP1 ======
            if trades[symbol].get("tp1", False) and gain < 1:
                vol5_loc  = float(np.mean(volumes[-5:]))  if len(volumes) >= 5  else 0.0
                vol20_loc = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
                ctx = {
                    "rsi": rsi, "macd": macd, "signal": signal, "adx": adx_value,
                    "atr": atr, "st_on": supertrend_signal, "ema25": ema25, "ema200": ema200,
                    "ema50_4h": ema_tv([float(x[4]) for x in get_cached(symbol,'4h')], 50) if get_cached(symbol,'4h') else 0.0,
                    "ema200_4h": ema_tv([float(x[4]) for x in get_cached(symbol,'4h')], 200) if get_cached(symbol,'4h') else 0.0,
                    "vol5": vol5_loc, "vol20": vol20_loc,
                    "vol_ratio": (vol5_loc / max(vol20_loc, 1e-9)) if vol20_loc else 0.0,
                    "btc_up": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_up": MARKET_STATE.get("eth", {}).get("up", False),
                    "elapsed_h": elapsed_time
                }
                _finalize_exit(symbol, price, gain, "Perte de momentum après TP1", "SELL", ctx)
                return

            # ====== 6) Stop touché / perte max ======
            if price <= trades[symbol]["stop"] or gain <= -1.5:
                vol5_loc  = float(np.mean(volumes[-5:]))  if len(volumes) >= 5  else 0.0
                vol20_loc = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
                event = "STOP" if price <= trades[symbol]["stop"] else "SELL"
                reason = "Stop touché" if event == "STOP" else "Perte max (-1.5%)"
                ctx = {
                    "rsi": rsi, "macd": macd, "signal": signal, "adx": adx_value,
                    "atr": atr, "st_on": supertrend_signal, "ema25": ema25, "ema200": ema200,
                    "ema50_4h": ema_tv([float(x[4]) for x in get_cached(symbol,'4h')], 50) if get_cached(symbol,'4h') else 0.0,
                    "ema200_4h": ema_tv([float(x[4]) for x in get_cached(symbol,'4h')], 200) if get_cached(symbol,'4h') else 0.0,
                    "vol5": vol5_loc, "vol20": vol20_loc,
                    "vol_ratio": (vol5_loc / max(vol20_loc, 1e-9)) if vol20_loc else 0.0,
                    "btc_up": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_up": MARKET_STATE.get("eth", {}).get("up", False),
                    "elapsed_h": elapsed_time
                }
                _finalize_exit(symbol, price, gain, reason, event, ctx)
                return

            # sinon: HOLD
            log_trade(symbol, "HOLD", price)
            await buffer_hold(symbol, f"{utc_now_str()} | {symbol} HOLD | prix {price:.4f} | gain {gain:.2f}% | stop {trades[symbol].get('stop', trades[symbol].get('sl_initial', price)):.4f}")
            return


        # --- Filtre marché BTC (assoupli) pour ALT (STANDARD) ---
        if symbol != "BTCUSDT":
            btc = market_cache.get("BTCUSDT", [])
            if len(btc) >= 50:
                closes_btc = [float(k[4]) for k in btc]
                btc_rsi = rsi_tv(closes_btc, 14)
                btc_macd, btc_signal = compute_macd(closes_btc)

                # ET + seuil RSI 43
                WEAK_BTC = (btc_rsi < 43) and (btc_macd <= btc_signal)

                # Exemptions: MAJORS + high-liq
                HIGH_LIQ = {"BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","LINKUSDT"}

                if WEAK_BTC and (symbol not in MAJORS) and (symbol not in HIGH_LIQ):
                    # pénalité soft (pas de blocage dur ici)
                    try:
                        indicators_soft_penalty += 1
                    except NameError:
                        indicators_soft_penalty = 1
                    log_refusal(symbol, "BTC faible (soft): RSI<43 ET MACD<=Signal")
                    # ⚠️ on continue: seul btc_regime_blocked() peut bloquer (panic)

                    
        # --- 4h ---
        klines_4h = get_cached(symbol, '4h')
        if not klines_4h or len(klines_4h) < 50:
            log_refusal(symbol, "Données 4h insuffisantes")
            if not in_trade:
                return

        closes_4h = [float(k[4]) for k in klines_4h]
        ema200_4h = ema_tv(closes_4h, 200)
        ema50_4h  = ema_tv(closes_4h, 50)
        rsi_4h    = rsi_tv(closes_4h, period=14)

        # Contexte marché via cache
        btc_up = is_uptrend([float(k[4]) for k in market_cache.get('BTCUSDT', [])]) if market_cache.get('BTCUSDT') else False
        eth_up = is_uptrend([float(k[4]) for k in market_cache.get('ETHUSDT', [])]) if market_cache.get('ETHUSDT') else False

        if not is_market_bullish():
            b = MARKET_STATE.get("btc", {})
            e = MARKET_STATE.get("eth", {})
            btc_ok = b.get("rsi", 50) >= 45 and (b.get("macd", 0) > b.get("signal", 0) or b.get("adx", 0) >= 15)
            eth_ok = e.get("rsi", 50) >= 45 and (e.get("macd", 0) > e.get("signal", 0) or e.get("adx", 0) >= 15)
            if not (btc_ok or eth_ok):
                log_refusal(symbol, "Blocage ALT: BTC & ETH faibles (soft)")
                return
            # pénalité (mais on autorise)
            try:
                indicators_soft_penalty += 1
            except NameError:
                pass


        supertrend_signal = supertrend_like_on_close(klines)
        indicators_soft_penalty = 0
        reasons = []
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
        if price < ema200:          indicators_soft_penalty += 1
        if closes_4h[-1] < ema50_4h: indicators_soft_penalty += 1
        if closes_4h[-1] < ema200_4h: indicators_soft_penalty += 1

        if is_market_range(closes_4h):
            log_info(symbol, "Marché en range (soft) → pénalité")
            indicators_soft_penalty += 1
        # Supertrend 1h non haussier (pénalité)
        if not supertrend_signal:
            log_info(symbol, "Supertrend 1h non haussier (soft) → pénalité")
            indicators_soft_penalty += 1

        if detect_rsi_divergence(closes, rsi_series):
            log_refusal(symbol, "RSI divergence (soft)", trigger=f"price↑ & RSI↓ ({closes[-2]:.4f}->{closes[-1]:.4f} ; rsi {rsi_series[-2]:.1f}->{rsi_series[-1]:.1f})")
            reasons += ["⚠️ RSI divergence détectée (soft)"]
            # pas de return -> on continue


        volatility = get_volatility(atr, price)
        VOL_MIN = 0.001  # test assoupli (avant 0.002 ou 0.003)
        if volatility < VOL_MIN:
            log_refusal(symbol, f"Volatilité faible (ATR/price={volatility:.4f} < {VOL_MIN})")
            try:
                indicators_soft_penalty += 1
            except NameError:
                pass
            # si vraiment très faible (<0.0008), on refuse
            if volatility < 0.0008:
                return

        # --- ADX (standard) assoupli + tolérance momentum fort ---
        strong_momentum = (macd > signal) and (rsi >= 55)
        adx_min_eff = (ADX_MIN if LEARNING_MODE else 18)

        if adx_value < adx_min_eff:
            if strong_momentum and adx_value >= 14:
                # tolérance : pas de pénalité si MACD>Signal et RSI≥55 avec ADX 14–15
                log_refusal(symbol, f"ADX borderline toléré (soft): {adx_value:.1f} avec momentum fort")
                # pas de reasons += ... et pas de pénalité
                pass
            else:
                log_refusal(symbol, f"ADX trop faible (soft): {adx_value:.1f}")
                reasons += [f"⚠️ ADX {adx_value:.1f} (soft)"]
                try:
                    indicators_soft_penalty += 1
                except NameError:
                    pass
        # pas de return -> on continue



        # Supertrend reste obligatoire
        if not supertrend_signal:
            log_refusal(symbol, "Supertrend 1h non haussier (signal=False)")
            return

        # [PATCH-COOLDOWN std] — le cooldown ne bloque que les nouvelles entrées
        if (symbol in last_trade_time) and (not in_trade):
            cooldown_left_h = COOLDOWN_HOURS - (datetime.now(timezone.utc) - last_trade_time[symbol]).total_seconds() / 3600
            if cooldown_left_h > 0:
                log_refusal(
                    symbol,
                    "Cooldown actif",
                    cooldown_left_min=int(cooldown_left_h * 60)
                )
                return
            
        # ----- Garde-fous -----
        # ⚙️ Patch : suppression de la limite de trades simultanés
        slots = min(allowed_trade_slots("standard"), perf_cap_max_trades("standard"))
        if _nb_trades("standard") >= slots:
            log_info(symbol, f"[Patch] Max trades standard atteints ({_nb_trades('standard')}/{slots}) — autorisé quand même")
            # on laisse passer (pas de return)

        # --- Low-liquidity session -> SOFT ---
        ok_session, _sess = is_active_liquidity_session(symbol=symbol)
        if not ok_session:
            if symbol in MAJORS:
                # On laisse passer sans pénalité sur les majeures
                log_info(symbol, "Low-liquidity session (tolérée sur major)")
            else:
                # Soft penalty sur les autres paires
                indicators_soft_penalty += 1
                tendance_soft_notes.append("Session à faible liquidité")
                log_info(symbol, "Low-liquidity session (soft)")
            # Pas de return: on continue le flux

        if symbol not in MAJORS:
            blocked, why = btc_regime_blocked()
            if blocked:
                log_refusal(symbol, f"Filtre régime BTC: {why}")
                if not in_trade:
                    return

        # --- BTC drift : hard seulement sur low-liq sans edge, sinon soft ---
        if symbol != "BTCUSDT" and btc_market_drift():
            rs = rel_strength_vs_btc(symbol)  # edge ALT vs BTC (3 bougies 1h)
            HIGH_LIQ = {"BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","LINKUSDT"}
            # Hard uniquement si alts low-liq, RS faible et momentum pas fou
            if (symbol not in HIGH_LIQ) and (rs <= 0.006) and (adx_value < 22):
                log_refusal(symbol, "BTC drift (hard on low-liq)", trigger=f"RSvsBTC={rs:.3%}")
                if not in_trade:
                    return
            else:
                # Soft penalty sinon (on laisse passer)
                try:
                    indicators_soft_penalty += 1
                except NameError:
                    indicators_soft_penalty = 1
                log_info(symbol, "BTC drift (soft) — high-liq ou RS>BTC")
     
        # — Pré-filtre: prix trop loin de l’EMA25 (pénalité soft, seuil relevé)
        EMA25_PREFILTER_STD = (1.06 if symbol in MAJORS else 1.10)

        if price > ema25 * EMA25_PREFILTER_STD:
            dist = (price / max(ema25, 1e-9) - 1) * 100
            seuil_pct = (EMA25_PREFILTER_STD - 1) * 100
            log_refusal(
                symbol,
                f"Prix éloigné EMA25 (soft > +{seuil_pct:.0f}%)",
                trigger=f"dist_ema25={dist:.2f}%"
            )
            try:
                indicators_soft_penalty += 1
            except NameError:
                pass
                
            # pas de return : on continue
        if not check_spike_and_wick(symbol, klines, price, mode_label="std"):
            if not in_trade:
                return
            
        # [#volume-confirm-standard]
        k15 = get_cached(symbol, VOL_CONFIRM_TF, limit=max(25, VOL_CONFIRM_LOOKBACK + 5))
        vols15 = volumes_series(k15, quote=True)
        if len(vols15) < VOL_CONFIRM_LOOKBACK + 1:
            log_refusal(symbol, "Données 15m insuffisantes (volume)")
            if not in_trade:
                return

        # APRÈS (standard) — MA10 + seuil 0.50
        # --- volume-confirm-standard (MA10) ---
        vol_now = float(k15[-2][7])
        vol_ma10 = float(np.mean(vols15[-11:-1]))
        vol_ratio_15m = vol_now / max(vol_ma10, 1e-9)

        min_ratio15 = 0.45 if symbol in MAJORS else VOL15M_MIN_RATIO
        if vol_ratio_15m < min_ratio15:
            log_refusal(symbol, f"Vol 15m faible (soft): {vol_ratio_15m:.2f}")
            reasons += [f"⚠️ Vol15m {vol_ratio_15m:.2f} (soft)"]
            # pas de return -> on continue

        # === Confluence & scoring (final) ===
        volume_ok   = float(np.mean(volumes[-5:])) > float(np.mean(volumes[-20:]))
        trend_ok = (
            (price >= ema200 * 0.99)                             # tolérance -1%
            or (closes_4h[-1] > ema50_4h and ema50_4h > ema200_4h)  # 4h propre
        ) and supertrend_signal

        momentum_ok = (macd > signal) and (rsi >= (52 if adx_value >= 20 else 55))

        indicators = {
            "rsi": rsi,
            "macd": macd,
            "signal": signal,
            "supertrend": supertrend_signal,
            "adx": adx_value,
            "volume_ok": volume_ok,
            "above_ema200": price > ema200,
        }

        # Coiffe les pénalités “soft”
        indicators_soft_penalty = min(indicators_soft_penalty, 2)

        # Calcul du score puis bonus marché si BTC est propre
        confidence = max(0, compute_confidence_score(indicators) - indicators_soft_penalty)
        if btc_is_bullish_strong():
            confidence = min(10, confidence + 1)

        label_conf = label_confidence(confidence)

        # --- Stop provisoire pour le sizing (même logique que l'entrée) ---
        # --- Stop initial ATR (plus large, moins de faux stops) ---
        ATR_INIT_MULT_STD = 1.2
        sl_initial = price - ATR_INIT_MULT_STD * atr
        # sécurité: ne jamais dépasser le prix (et >= 0)
        sl_initial = max(0.0, min(sl_initial, price * 0.999))

        position_pct = position_pct_from_risk(price, sl_initial)

        # --- Décision d'achat (standard) avec filtre 15m ---
        brk_ok, br_level = detect_breakout_retest(closes, highs, lookback=10, tol=0.003)
        last3_change = (closes[-1] - closes[-4]) / max(closes[-4], 1e-9)
        atr_pct = atr / max(price, 1e-9)
        limit = (0.032 if symbol in MAJORS else 0.030)
        limit = max(limit, 2.0 * atr_pct)  # tolère davantage si vol élevé
        if last3_change > limit:
            brk_ok = False

        buy = False

        if brk_ok and trend_ok and momentum_ok and volume_ok:
            # Filtre 15m avec niveau de breakout
            n_struct = 2 if (symbol in MAJORS or adx_value >= 20) else 3
            ok15, det15 = check_15m_filter(k15, breakout_level=br_level, n_struct=n_struct, tol_struct=0.0015)
            if not ok15:
                log_refusal(symbol, f"Filtre 15m non validé (BRK): {det15}")
                if not in_trade:
                    return

            if not confirm_15m_after_signal(symbol, breakout_level=br_level, ema25_1h=ema25):
                log_refusal(symbol, "Anti-chasse: ...")
                if not in_trade:
                    return
                    
            if price > ema25 * 1.08:
                log_refusal(symbol, f"Prix éloigné EMA25 (soft): {price:.4f} > EMA25×1.05 ({ema25*1.05:.4f})")
                reasons += [f"⚠️ Distance EMA25 {price/ema25-1:.2%} (soft)"]
                # pas de return -> on continue
            
            if tendance_soft_notes:
                reasons += [f"Avertissements tendance: {', '.join(tendance_soft_notes)}"]


            buy = True
            label = "⚡ Breakout + Retest validé (1h) + Confluence"
            reasons = [label, f"ADX {adx_value:.1f} >= 22", f"MACD {macd:.3f} > Signal {signal:.3f}"]

        elif trend_ok and momentum_ok and volume_ok:
            # Bande "retest" serrée autour de l'EMA25 (±0.2%)
            RETEST_BAND_STD = 0.004
            near_ema25 = (abs(price - ema25) / max(ema25, 1e-9)) <= RETEST_BAND_STD

            # On garde le contrôle de bougie, mais un peu plus permissif (3.5% au lieu de 3%)
            candle_ok = (abs(highs[-1] - lows[-1]) / max(lows[-1], 1e-9)) <= 0.035

            if near_ema25 and candle_ok:
                # Filtre 15m sans niveau de breakout
                n_struct = 2 if (symbol in MAJORS or adx_value >= 20) else 3
                ok15, det15 = check_15m_filter(k15, breakout_level=None, n_struct=n_struct, tol_struct=0.0015)
                if not ok15:
                    log_refusal(symbol, f"Filtre 15m non validé (PB EMA25): {det15}")
                    return
                    
                # pour PB: on exige close 15m > EMA25(1h) ±0.1%
                if not confirm_15m_after_signal(symbol, breakout_level=None, ema25_1h=ema25):
                    log_refusal(symbol, "Anti-chasse: pas de clôture 15m > EMA25(1h) ±0.1% (PB)")
                    return

                if price > ema25 * 1.08:
                    log_refusal(symbol, f"Prix éloigné EMA25 (soft): {price:.4f} > EMA25×1.05 ({ema25*1.05:.4f})")
                    reasons += [f"⚠️ Distance EMA25 {price/ema25-1:.2%} (soft)"]
                    # pas de return -> on continue

                buy = True
                label = "✅ Pullback EMA25 propre + Confluence"
                reasons = [label, f"ADX {adx_value:.1f} >= 22", f"MACD {macd:.3f} > Signal {signal:.3f}"]

        # ===== Patch 4 — score minimum (standard) assoupli & non bloquant =====
        SCORE_MIN_STD = 3

        if confidence < SCORE_MIN_STD:
            # On LOG pour suivi, mais on ne coupe plus le trade.
            log_refusal(symbol, f"Score insuffisant après pénalités (std): {confidence:.1f} < {SCORE_MIN_STD}")
            # Petite pénalité additionnelle au scoring si la variable existe
            try:
                indicators_soft_penalty += 1
            except NameError:
                pass
            # Réduction de la taille plutôt que d'annuler l'entrée
            try:
                position_pct = max(POS_MIN_PCT, min(position_pct * 0.75, POS_MAX_PCT))
            except NameError:
                # si pas de sizing encore défini ici, on met une petite taille par défaut
                position_pct = POS_MIN_PCT

        # --- Circuit breaker JOUR (avant toute nouvelle entrée) ---
        if buy and not in_trade:
            pnl_today = daily_pnl_pct_utc()
            if pnl_today <= DAILY_MAX_LOSS * 100.0:
                log_refusal(symbol, f"Daily loss limit hit (...)")
                return

        # --- Entrée (BUY) ---
        if buy and symbol not in trades:
            trade_id = make_trade_id(symbol)

            # 🔁 réutilise le stop calculé plus haut pour le sizing
            sl_initial = sl_initial   # <= au lieu de recalculer avec pick_sl_pct(...)

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
                "atr_at_entry": atr_tv(klines),
                "tp_multipliers": TP_ATR_MULTS_STD,
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

    except Exception as e:
        print(f"❌ Erreur {symbol}: {e}", flush=True)
        traceback.print_exc()


async def process_symbol_aggressive(symbol):
    try:
        in_trade = symbol in trades

        # --- Paramètres VOLUME (aggressive) ---
        MIN_VOLUME_LOCAL = 50_000
        VOL_MED_MULT_AGR = 0.15
        VOL_CONFIRM_MULT_AGR = 0.85
        VOL_CONFIRM_LOOKBACK_AGR = 12

        # ---- Auto-close SOUPLE (aggressive) ----
        if symbol in trades and trades[symbol].get("strategy") == "aggressive":
            entry_time = datetime.strptime(trades[symbol]['time'], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            elapsed_h = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            # Pas d’auto-close forcé ici
            pass

        # ---- Analyse agressive ----
        klines = get_cached(symbol, '1h')
        in_trade_agr = (symbol in trades) and (trades[symbol].get("strategy") == "aggressive")

        # Fallback si pas de 1h alors qu’on est en position
        if in_trade_agr and (not klines or len(klines) < 20):
            price = get_last_price(symbol)
            if price is None:
                await tg_send(f"⚠️ {symbol} en position (aggressive) mais données 1h indisponibles. Pas d’update.")
                return
            entry = float(trades[symbol].get("entry", price))
            stop  = float(trades[symbol].get("stop", trades[symbol].get("sl_initial", price)))
            gain  = ((price - entry) / max(entry, 1e-9)) * 100.0

            if price <= stop or gain <= -1.5:
                msg = format_stop_msg(symbol, trades[symbol]["trade_id"], stop, gain, 0, 0, 0)
                await tg_send(msg)
                log_trade_csv({
                    "ts_utc": utc_now_str(),
                    "trade_id": trades[symbol]["trade_id"],
                    "symbol": symbol,
                    "event": "STOP",
                    "strategy": "aggressive",
                    "version": BOT_VERSION,
                    "entry": entry,
                    "exit": price,
                    "price": price,
                    "pnl_pct": gain,
                    "position_pct": trades[symbol].get("position_pct", ""),
                    "sl_initial": trades[symbol].get("sl_initial", ""),
                    "sl_final": stop,
                    "atr_1h": 0.0, "atr_mult_at_entry": "",
                    "rsi_1h": 0.0, "macd": 0.0, "signal": 0.0, "adx_1h": 0.0,
                    "supertrend_on": False,
                    "ema25_1h": 0.0, "ema200_1h": 0.0, "ema50_4h": 0.0, "ema200_4h": 0.0,
                    "vol_ma5": 0.0, "vol_ma20": 0.0, "vol_ratio": 0.0,
                    "btc_uptrend": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_uptrend": MARKET_STATE.get("eth", {}).get("up", False),
                    "reason_entry": trades[symbol].get("reason_entry", ""),
                    "reason_exit": "Stop (fallback 1h indisponible)"
                })
                log_trade(symbol, "STOP", price, gain)
                _delete_trade(symbol)
                return

            log_trade(symbol, "HOLD", price)
            await buffer_hold(symbol, f"{utc_now_str()} | {symbol} HOLD (fallback) | prix {price:.4f} | stop {stop:.4f}")
            return

        if not klines or len(klines) < 50:
            log_refusal(symbol, "Données 1h insuffisantes")
            if not in_trade:
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
            if not in_trade:
                return

        # --- Volume 1h vs médiane 30j ---
        vol_now_1h = float(klines[-1][7])
        k1h_30d = get_cached(symbol, '1h', limit=750) or []
        vols_hist = volumes_series(k1h_30d, quote=True)[-721:]

        VOL_MED_MULT_EFF = (0.05 if symbol in MAJORS else 0.07)
        MIN_VOLUME_ABS   = (80_000 if symbol in MAJORS else 120_000)

        if len(vols_hist) >= 200:
            med_30d_raw = float(np.median(vols_hist[:-1]))
            p70 = float(np.percentile(vols_hist[:-1], 70))
            med_30d = min(med_30d_raw, p70 * 1.5)
            if symbol != "BTCUSDT" and med_30d > 0 and vol_now_1h < max(MIN_VOLUME_ABS, VOL_MED_MULT_EFF * med_30d):
                log_refusal(symbol, f"Volume 1h trop faible vs med30j ({vol_now_1h:.0f} < {VOL_MED_MULT_EFF:.2f}×{med_30d:.0f})")
                return
        else:
            if vol_now_1h < MIN_VOLUME_ABS:
                log_refusal(symbol, f"Volume 1h trop faible (abs) {vol_now_1h:.0f} < {MIN_VOLUME_ABS}")
                return

        # ---- Indicateurs (TV-like) ----
        rsi       = rsi_tv(closes, period=14)
        macd,signal = compute_macd(closes)
        ema200    = ema_tv(closes, 200)
        ema200_1h = ema200
        atr       = atr_tv(klines, period=14)
        adx_value = adx_tv(klines, period=14)
        ema25     = ema_tv(closes, 25)
        macd_prev, signal_prev = compute_macd(closes[:-1])

        # === Gestion d'un trade agressif OUVERT ===
        in_trade = (symbol in trades) and (trades[symbol].get("strategy") == "aggressive")
        if in_trade:
            atr_val_current = atr_tv(klines)
            entry = trades[symbol]["entry"]
            gain  = ((price - entry) / max(entry, 1e-9)) * 100.0
            stop  = trades[symbol].get("stop", trades[symbol].get("sl_initial", price * (1 - INIT_SL_PCT_AGR_MIN)))
            elapsed_time = (
                datetime.now(timezone.utc)
                - datetime.strptime(trades[symbol]["time"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            ).total_seconds() / 3600

            # TP progressifs
            if "tp_times" not in trades[symbol]:
                trades[symbol]["tp_times"] = {}
            for tp_idx, atr_mult in enumerate(TP_ATR_MULTS_AGR, start=1):
                threshold_pct = (atr_mult * atr_val_current / max(entry, 1e-9)) * 100.0
                if gain >= threshold_pct and not trades[symbol].get(f"tp{tp_idx}", False):
                    last_tp_time = trades[symbol]["tp_times"].get(f"tp{tp_idx-1}") if tp_idx > 1 else None
                    if isinstance(last_tp_time, str):
                        try:
                            last_tp_time = datetime.fromisoformat(last_tp_time)
                        except Exception:
                            last_tp_time = None
                    if not last_tp_time or (datetime.now(timezone.utc) - last_tp_time).total_seconds() >= 120:
                        trades[symbol][f"tp{tp_idx}"] = True
                        trades[symbol]["tp_times"][f"tp{tp_idx}"] = datetime.now(timezone.utc)
                        new_stop_level = entry if tp_idx == 1 else entry * (1.0 + max(0.0, threshold_pct - 0.5) / 100.0)
                        trades[symbol]["stop"] = max(stop, new_stop_level)
                        save_trades()
                        msg = format_tp_msg(
                            tp_idx, symbol, trades[symbol]["trade_id"], price, gain,
                            trades[symbol]["stop"],
                            ((trades[symbol]["stop"] - entry) / entry) * 100.0,
                            elapsed_time, "Stop ajusté (ATR)"
                        )
                        await tg_send(msg)

            # Trailing stop adaptatif (ADX)
            adx_val = trades[symbol].get("adx_1h", 20)
            trail_multiplier = 0.3 if adx_val >= 25 else (0.5 if adx_val >= 20 else 0.8)
            trades[symbol]["stop"] = max(stop, price * (1 - trail_multiplier * atr_val_current / max(price, 1e-9)))

            # Filtre régime BTC (définir blocked AVANT)
            blocked, _why_btc = btc_regime_blocked()
            if blocked and gain < 0.8:
                supertrend_ok_local = supertrend_like_on_close(klines)
                ctx = {
                    "rsi": rsi, "macd": macd, "signal": signal, "adx": adx_value,
                    "atr": atr, "st_on": supertrend_ok_local, "ema25": ema25, "ema200": ema200,
                    "ema50_4h": ema_tv([float(x[4]) for x in get_cached(symbol, '4h')], 50) if get_cached(symbol,'4h') else 0.0,
                    "ema200_4h": ema_tv([float(x[4]) for x in get_cached(symbol, '4h')], 200) if get_cached(symbol,'4h') else 0.0,
                    "vol5": vol5, "vol20": vol20,
                    "vol_ratio": (vol5 / max(vol20, 1e-9)) if vol20 else 0.0,
                    "btc_up": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_up": MARKET_STATE.get("eth", {}).get("up", False),
                    "elapsed_h": elapsed_time
                }
                _finalize_exit(symbol, price, gain, "BTC regime turned negative", "SELL", ctx)
                return

            # Timeout intelligent (aggressive)
            if (elapsed_time >= SMART_TIMEOUT_EARLIEST_H_AGR
                and gain < SMART_TIMEOUT_MIN_GAIN_AGR
                and not trades[symbol].get("tp1", False)
                and not btc_is_bullish_strong()):
                k1h_now = get_cached(symbol, '1h')
                trig, why = smart_timeout_check(k1h_now, entry,
                                                window_h=SMART_TIMEOUT_WINDOW_H,
                                                range_pct=SMART_TIMEOUT_RANGE_PCT_AGR)
                if trig:
                    raison = f"Timeout intelligent (aggressive): {why}"
                    msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, trades[symbol]["stop"], elapsed_time, raison)
                    await tg_send(msg)
                    vol5_loc  = float(np.mean(volumes[-5:])) if len(volumes) >= 5 else 0.0
                    vol20_loc = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
                    log_trade_csv({
                        "trade_id": trades[symbol]["trade_id"], "symbol": symbol, "event": "SMART_TIMEOUT",
                        "strategy": "aggressive", "version": BOT_VERSION,
                        "entry": entry, "exit": price, "price": price, "pnl_pct": gain,
                        "position_pct": trades[symbol]["position_pct"],
                        "sl_initial": trades[symbol]["sl_initial"], "sl_final": trades[symbol]["stop"],
                        "atr_1h": atr_val_current, "atr_mult_at_entry": "",
                        "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                        "supertrend_on": supertrend_like_on_close(klines), "ema25_1h": ema25, "ema200_1h": ema200_1h,
                        "ema50_4h": ema_tv([float(x[4]) for x in get_cached(symbol, '4h')], 50) if get_cached(symbol, '4h') else 0.0,
                        "ema200_4h": ema_tv([float(x[4]) for x in get_cached(symbol, '4h')], 200) if get_cached(symbol, '4h') else 0.0,
                        "vol_ma5": vol5_loc, "vol_ma20": vol20_loc, "vol_ratio": vol5_loc / max(vol20_loc, 1e-9) if vol20_loc else 0.0,
                        "btc_uptrend": MARKET_STATE.get("btc", {}).get("up", False),
                        "eth_uptrend": MARKET_STATE.get("eth", {}).get("up", False),
                        "reason_entry": trades[symbol]["reason_entry"], "reason_exit": raison
                    })
                    log_trade(symbol, "SELL", price, gain)
                    _delete_trade(symbol)
                    return

            # Sortie dynamique 5m
            triggered, fx = fast_exit_5m_trigger(symbol, entry, price)
            if triggered:
                vol5_local  = float(np.mean(volumes[-5:])) if len(volumes) >= 5 else 0.0
                vol20_local = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
                reason_bits = []
                if fx.get("rsi_drop") is not None and fx["rsi_drop"] > 5:
                    reason_bits.append(f"RSI(5m) -{fx['rsi_drop']:.1f} pts")
                if fx.get("macd_cross_down"):
                    reason_bits.append("MACD(5m) croisement baissier")
                raison = "Sortie dynamique 5m: gain ≥ +1% ; " + " + ".join(reason_bits)
                msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, trades[symbol]["stop"], elapsed_time, raison)
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
                    "supertrend_on": supertrend_like_on_close(klines),
                    "ema25_1h": ema25, "ema200_1h": ema200_1h, "ema50_4h": ema_tv([float(x[4]) for x in get_cached(symbol, '4h')], 50) if get_cached(symbol, '4h') else 0.0,
                    "ema200_4h": ema_tv([float(x[4]) for x in get_cached(symbol, '4h')], 200) if get_cached(symbol, '4h') else 0.0,
                    "vol_ma5": vol5_local, "vol_ma20": vol20_local, "vol_ratio": vol5_local / max(vol20_local, 1e-9) if vol20_local else 0.0,
                    "btc_uptrend": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_uptrend": MARKET_STATE.get("eth", {}).get("up", False),
                    "reason_entry": trades[symbol]["reason_entry"],
                    "reason_exit": raison
                })
                log_trade(symbol, "SELL", price, gain)
                _delete_trade(symbol)
                return

            # Momentum cassé
            if gain < 0.8 and (rsi < 48 or macd < signal):
                raison = "Momentum cassé (sortie anticipée)"
                msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, trades[symbol]["stop"], elapsed_time, raison)
                await tg_send(msg)
                vol5_loc  = float(np.mean(volumes[-5:])) if len(volumes) >= 5 else 0.0
                vol20_loc = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
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
                    "atr_1h": atr_val_current,
                    "atr_mult_at_entry": "",
                    "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                    "supertrend_on": supertrend_like_on_close(klines),
                    "ema25_1h": ema25, "ema200_1h": ema200_1h,
                    "ema50_4h": ema_tv([float(x[4]) for x in get_cached(symbol, '4h')], 50) if get_cached(symbol, '4h') else 0.0,
                    "ema200_4h": ema_tv([float(x[4]) for x in get_cached(symbol, '4h')], 200) if get_cached(symbol, '4h') else 0.0,
                    "vol_ma5": vol5_loc, "vol_ma20": vol20_loc, "vol_ratio": vol5_loc / max(vol20_loc, 1e-9) if vol20_loc else 0.0,
                    "btc_uptrend": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_uptrend": MARKET_STATE.get("eth", {}).get("up", False),
                    "reason_entry": trades[symbol]["reason_entry"],
                    "reason_exit": raison
                })
                log_trade(symbol, "SELL", price, gain)
                _delete_trade(symbol)
                return

            # Après TP1 si retombée
            if trades[symbol].get("tp1", False) and gain < 1:
                raison_sortie = "Perte de momentum après TP1"
                msg = format_exit_msg(symbol, trades[symbol]["trade_id"], price, gain, trades[symbol]["stop"], elapsed_time, raison_sortie)
                await tg_send(msg)
                vol5_loc  = float(np.mean(volumes[-5:])) if len(volumes) >= 5 else 0.0
                vol20_loc = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
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
                    "supertrend_on": supertrend_like_on_close(klines), "ema25_1h": ema25, "ema200_1h": ema200_1h,
                    "ema50_4h": ema_tv([float(x[4]) for x in get_cached(symbol, '4h')], 50) if get_cached(symbol, '4h') else 0.0,
                    "ema200_4h": ema_tv([float(x[4]) for x in get_cached(symbol, '4h')], 200) if get_cached(symbol, '4h') else 0.0,
                    "vol_ma5": vol5_loc, "vol_ma20": vol20_loc, "vol_ratio": vol5_loc / max(vol20_loc, 1e-9) if vol20_loc else 0.0,
                    "btc_uptrend": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_uptrend": MARKET_STATE.get("eth", {}).get("up", False),
                    "reason_entry": trades[symbol]["reason_entry"], "reason_exit": raison_sortie
                })
                log_trade(symbol, "SELL", price, gain)
                _delete_trade(symbol)
                return

            # Stop touché / perte max
            if price < trades[symbol]["stop"] or gain <= -1.5:
                msg = format_stop_msg(symbol, trades[symbol]["trade_id"], trades[symbol]["stop"], gain, rsi, adx_value, vol5 / max(vol20, 1e-9) if vol20 else 0.0)
                await tg_send(msg)
                event_name = "STOP" if price < trades[symbol]["stop"] else "SELL"
                log_trade_csv({
                    "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_id": trades[symbol]["trade_id"],
                    "symbol": symbol,
                    "event": event_name,
                    "strategy": "aggressive",
                    "version": BOT_VERSION,
                    "entry": entry, "exit": price, "price": price, "pnl_pct": gain,
                    "position_pct": trades[symbol]["position_pct"],
                    "sl_initial": trades[symbol]["sl_initial"],
                    "sl_final": trades[symbol]["stop"],
                    "atr_1h": atr_val_current,
                    "atr_mult_at_entry": "",
                    "rsi_1h": rsi, "macd": macd, "signal": signal, "adx_1h": adx_value,
                    "supertrend_on": supertrend_like_on_close(klines),
                    "ema25_1h": ema25, "ema200_1h": ema200_1h,
                    "ema50_4h": ema_tv([float(x[4]) for x in get_cached(symbol, '4h')], 50) if get_cached(symbol, '4h') else 0.0,
                    "ema200_4h": ema_tv([float(x[4]) for x in get_cached(symbol, '4h')], 200) if get_cached(symbol, '4h') else 0.0,
                    "vol_ma5": vol5, "vol_ma20": vol20,
                    "vol_ratio": vol5 / max(vol20, 1e-9) if vol20 else 0.0,
                    "btc_uptrend": MARKET_STATE.get("btc", {}).get("up", False),
                    "eth_uptrend": MARKET_STATE.get("eth", {}).get("up", False),
                    "reason_entry": trades[symbol]["reason_entry"],
                    "reason_exit": "Stop touché" if event_name == "STOP" else "Perte max (-1.5%)"
                })
                log_trade(symbol, event_name, price, gain)
                _delete_trade(symbol)
                return

            # HOLD avec trailing avancé
            trailing_stop_advanced(symbol, price, atr_value=atr_val_current)
            log_trade(symbol, "HOLD", price)
            await buffer_hold(symbol, f"{utc_now_str()} | {symbol} HOLD | prix {price:.4f} | gain {gain:.2f}% | stop {trades[symbol].get('stop', trades[symbol].get('sl_initial', price)):.4f}")
            return

        # --- Filtre marché BTC (assoupli) pour ALT (AGGRESSIVE) ---
        if symbol != "BTCUSDT":
            btc_klines = market_cache.get("BTCUSDT", [])
            if len(btc_klines) >= 50:
                closes_btc = [float(k[4]) for k in btc_klines]
                btc_rsi = rsi_tv(closes_btc, period=14)
                btc_macd, btc_signal = compute_macd(closes_btc)

                WEAK_BTC = (btc_rsi < 43) and (btc_macd <= btc_signal)
                HIGH_LIQ = {"BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","LINKUSDT"}
    
                if WEAK_BTC and (symbol not in MAJORS) and (symbol not in HIGH_LIQ):
                    try:
                        indicators_soft_penalty += 1
                    except NameError:
                        indicators_soft_penalty = 1
                    log_refusal(symbol, "BTC faible (soft): RSI<43 ET MACD<=Signal")
                    # on continue ; le blocage dur est géré par btc_regime_blocked()

        # ---- Garde-fous ----
        # ⚙️ Patch : suppression de la limite de trades simultanés
        slots = min(allowed_trade_slots("aggressive"), perf_cap_max_trades("aggressive"))
        if _nb_trades("aggressive") >= slots:
            log_info(symbol, f"[Patch] Max trades aggressive atteints ({_nb_trades('aggressive')}/{slots}) — autorisé quand même")
            # on laisse passer (pas de return)

        # --- Low-liquidity session -> SOFT ---
        indicators_soft_penalty = 0
        tendance_soft_notes = []
        reasons = []

        ok_session, _sess = is_active_liquidity_session(symbol=symbol)
        if not ok_session:
            if symbol in MAJORS:
                log_info(symbol, "Low-liquidity session (tolérée sur major)")
            else:
                indicators_soft_penalty += 1
                tendance_soft_notes.append("Session à faible liquidité")
                log_info(symbol, "Low-liquidity session (soft)")

        # Contexte marché global soft
        if not is_market_bullish():
            b = MARKET_STATE.get("btc", {})
            e = MARKET_STATE.get("eth", {})
            btc_ok = b.get("rsi", 50) >= 45 and (b.get("macd", 0) > b.get("signal", 0) or b.get("adx", 0) >= 15)
            eth_ok = e.get("rsi", 50) >= 45 and (e.get("macd", 0) > e.get("signal", 0) or e.get("adx", 0) >= 15)
            if not (btc_ok or eth_ok):
                log_refusal(symbol, "Blocage ALT: BTC & ETH faibles (soft)")
                return
            indicators_soft_penalty += 1

        # Cooldown (nouvelles entrées uniquement)
        if (symbol in last_trade_time) and (not in_trade):
            cooldown_left_h = COOLDOWN_HOURS - (datetime.now(timezone.utc) - last_trade_time[symbol]).total_seconds() / 3600
            if cooldown_left_h > 0:
                log_refusal(symbol, "Cooldown actif", cooldown_left_min=int(cooldown_left_h * 60))
                return

        # Filtre régime BTC (panic only) — pas pour MAJORS
        if symbol not in MAJORS:
            blocked, why = btc_regime_blocked()
            if blocked:
                log_refusal(symbol, f"Filtre régime BTC: {why}")
                if not in_trade:
                    return

        # Confluence principale ----
        supertrend_ok = supertrend_like_on_close(klines)
        above_ema200  = price >= ema200 * 0.98  # soft
        if not above_ema200:
            indicators_soft_penalty += 1
            tendance_soft_notes.append("Prix ~ sous EMA200(1h)")

        # ADX avec tolérance si momentum fort (MACD>Signal et RSI≥55)
        strong_momentum = (macd > signal) and (rsi >= 55)
        if adx_value >= 15 or (strong_momentum and adx_value >= 14):
            adx_ok = True
        else:
            adx_ok = False
            indicators_soft_penalty += 1


        # Momentum MACD (renforcé)
        if not (macd > signal and (macd - signal) > (macd_prev - signal_prev)):
            return

        # RSI zone constructive
        if not (51 <= rsi < 82):
            return

        # ---- Confluence 4h ----
        k4 = get_cached(symbol, '4h')
        if not k4 or len(k4) < 50:
            log_refusal(symbol, "Données 4h insuffisantes")
            if not in_trade:
                return

        c4 = [float(k[4]) for k in k4]
        ema50_4h  = ema_tv(c4, 50)
        ema200_4h = ema_tv(c4, 200)
        if c4[-1] < ema50_4h or ema50_4h < ema200_4h:
            return

        # ----- Breakout + Retest -----
        last10_high = max(highs[-10:])
        breakout = price > last10_high * 1.001  # 0.1% au lieu de 0.2%
        if not breakout:
            log_refusal(symbol, "Pas de breakout (last10_high non dépassé)")
            if not in_trade:
                return

        last3_change = (closes[-1] - closes[-4]) / closes[-4] if len(closes) >= 4 else 0
        if last3_change > 0.028:  # 2.8% au lieu de 2.2%
            log_refusal(symbol, "Mouvement 3 bougies trop fort (>2.8%)")
            if not in_trade:
                return
    
        RETEST_BAND_AGR = 0.003
        near_level = abs(price - last10_high) / last10_high <= RETEST_BAND_AGR
        near_ema25  = abs(price - ema25)      / ema25      <= RETEST_BAND_AGR

        # Pré-filtre EMA25 éloignée (seuil relevé)
        EMA25_PREFILTER_STD = (1.06 if symbol in MAJORS else 1.10)
        if price >= ema25 * EMA25_PREFILTER_STD:
            dist = (price / max(ema25, 1e-9) - 1) * 100
            log_refusal(symbol, f"Prix éloigné EMA25 (soft > +{(EMA25_PREFILTER_STD-1)*100:.0f}%)",
                        trigger=f"dist_ema25={dist:.2f}%")
            indicators_soft_penalty += 1

        # Anti-spike 1h brut
        open_now  = float(klines[-1][1])
        high_now  = float(klines[-1][2])
        spike_up_pct = ((max(high_now, price) - open_now) / max(open_now, 1e-9)) * 100.0
        if spike_up_pct > ANTI_SPIKE_UP_AGR:
            log_refusal(symbol, "Anti-spike (aggressive)", trigger=f"spike={spike_up_pct:.2f}%>{ANTI_SPIKE_UP_AGR:.2f}%")
            if not in_trade:
                return

        # Anti-spike + wick (soft)
        if not check_spike_and_wick(symbol, klines, price, mode_label="aggro"):
            return

        # ---- Confirmation volume 15m (aggressive) ----
        k15 = get_cached(symbol, '15m', limit=max(25, VOL_CONFIRM_LOOKBACK_AGR + 5))
        vols15 = volumes_series(k15, quote=True)
        if len(vols15) < VOL_CONFIRM_LOOKBACK_AGR + 1:
            log_refusal(symbol, "Données 15m insuffisantes (volume)")
            if not in_trade:
                return

        vol_now = float(k15[-2][7])
        vol_ma12 = float(np.mean(vols15[-13:-1]))
        vol_ratio_15m = vol_now / max(vol_ma12, 1e-9)
        if vol_ratio_15m < VOL_CONFIRM_MULT_AGR:
            log_refusal(symbol, f"Vol 15m faible (soft): {vol_ratio_15m:.2f}")
            reasons += [f"⚠️ Vol15m {vol_ratio_15m:.2f} (soft)"]
            # on continue (soft)

        # --- Filtre structure 15m (aggressive) ---
        n_struct = 2 if (symbol in MAJORS or adx_value >= 20) else 3
        ok15, det15 = check_15m_filter(
            k15,
            breakout_level=(last10_high if near_level else None),
            n_struct=n_struct,
            tol_struct=0.0015
        )
        if not ok15:
            log_refusal(symbol, f"Filtre 15m non validé (aggressive): {det15}")
            if not in_trade:
                return

        # ---- Scoring & sizing ----
        indicators = {
            "rsi": rsi,
            "macd": macd,
            "signal": signal,
            "supertrend": supertrend_ok,
            "adx": adx_value,
            "volume_ok": volume_ok,
            "above_ema200": above_ema200,
        }
        indicators_soft_penalty = min(indicators_soft_penalty, 2)
        score = max(0, compute_confidence_score(indicators) - indicators_soft_penalty)
        if btc_is_bullish_strong():
            score = min(10, score + 1)
        label_conf = label_confidence(score)

        SCORE_MIN_AGR = 6
        if score < SCORE_MIN_AGR:
            log_refusal(symbol, f"Score insuffisant (agr): {score:.1f} < {SCORE_MIN_AGR}")
            # Réduire la taille plutôt que refuser
            try:
                position_pct = max(POS_MIN_PCT, min(position_pct * 0.70, POS_MAX_PCT))
            except NameError:
                position_pct = POS_MIN_PCT

        ATR_INIT_MULT_AGR = 1.2
        sl_initial = price - ATR_INIT_MULT_AGR * atr
        sl_initial = max(0.0, min(sl_initial, price * 0.999))

        position_pct = position_pct_from_risk(price, sl_initial, score)

        br_level_for_check = last10_high if near_level else None
        if not confirm_15m_after_signal(symbol, breakout_level=br_level_for_check, ema25_1h=ema25):
            if br_level_for_check is not None:
                log_refusal(symbol, "Anti-chasse: pas de clôture 15m > niveau de retest (aggressive)")
            else:
                log_refusal(symbol, "Anti-chasse: pas de clôture 15m > EMA25(1h) ±0.1% (aggressive)")
            if not in_trade:
                return

        if price > ema25 * 1.08:
            log_refusal(symbol, f"Prix éloigné EMA25 (soft): {price:.4f} > EMA25×1.05 ({ema25*1.05:.4f})")
            reasons += [f"⚠️ Distance EMA25 {price/ema25-1:.2%} (soft)"]

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
            "atr_at_entry": atr_tv(klines),
            "tp_multipliers": TP_ATR_MULTS_AGR,
            "strategy": "aggressive",
        }
        last_trade_time[symbol] = datetime.now(timezone.utc)
        save_trades()

        # Contexte uptrend via cache
        btc_up = is_uptrend([float(k[4]) for k in market_cache.get("BTCUSDT", [])]) if market_cache.get("BTCUSDT") else False
        eth_up = is_uptrend([float(k[4]) for k in market_cache.get("ETHUSDT", [])]) if market_cache.get("ETHUSDT") else False

        msg = format_entry_msg(
            symbol, trade_id, "aggressive", BOT_VERSION, price, position_pct,
            sl_initial, ((price - sl_initial) / price) * 100, atr,
            rsi, macd, signal, adx_value,
            supertrend_ok,
            ema25,
            ema50_4h, ema200_1h, ema200_4h,
            vol5, vol20, vol5 / max(vol20, 1e-9),
            btc_up, eth_up,
            score, label_conf, reasons
        )
        await tg_send(msg)

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
            "sl_initial": sl_initial,
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

        # HOLD buffer initial (gain=0)
        await buffer_hold(
            symbol,
            f"{utc_now_str()} | {symbol} HOLD | prix {price:.4f} | gain {0.0:.2f}% | stop {trades[symbol].get('stop', trades[symbol].get('sl_initial', price)):.4f}"
        )

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

async def flush_hold_buffer():
    """Envoie les messages HOLD accumulés puis vide le buffer."""
    if not hold_buffer:
        return
    try:
        for sym, msgs in list(hold_buffer.items()):
            if not msgs:
                continue
            # on compacte par symbole (dernier 10 max)
            chunk = "\n".join(msgs[-10:])
            await tg_send(f"📡 HOLD {sym}\n{chunk}")
        hold_buffer.clear()
    except Exception as e:
        print(f"⚠️ flush_hold_buffer: {e}")

async def main_loop():
    global trades  # ✅ déclaré dès le début

    await asyncio.sleep(0.5)

    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=f"🚀 Bot démarré {datetime.now(timezone.utc).strftime('%H:%M:%S')}"
        )
        print("✅ Message de démarrage envoyé")
    except Exception as e:
        print(f"❌ Erreur envoi démarrage: {e}")

    # Charger les trades + history sauvegardés
    trades.update(load_trades())
    # garde la même liste en mémoire (référence) et remplit depuis disque
    history_loaded = load_history()
    history.clear()
    history.extend(history_loaded)

    # hydrater last_trade_time depuis le disque
    for _sym, _t in trades.items():
        try:
            ts = _t.get("time")
            if not ts:
                continue
            dt = _parse_dt_flex(ts)
            if dt is None:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            last_trade_time[_sym] = dt.astimezone(timezone.utc)
        except Exception:
            pass

    last_heartbeat = None
    last_summary_day = None
    last_audit_day = None

    while True:
        try:
            now = datetime.now(timezone.utc)

            # ✅ heartbeat horaire
            if last_heartbeat != now.hour:
                await tg_send(f"✅ Bot actif {now.strftime('%H:%M')}")
                await send_refusal_top(60, 8)
                last_heartbeat = now.hour

            # ✅ résumé quotidien 23:00 UTC
            if now.hour == 23 and (last_summary_day is None or last_summary_day != now.date()):
                await send_daily_summary()
                last_summary_day = now.date()

            # --- préchargement multi-TF ---
            symbol_cache.clear()
            tasks = []
            for s in SYMBOLS:
                symbol_cache.setdefault(s, {})
                for tf, lim in TF_LIST:
                    tasks.append(get_klines_async(s, tf, lim))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            idx = 0
            for s in SYMBOLS:
                for tf, lim in TF_LIST:
                    r = results[idx]; idx += 1
                    symbol_cache[s][tf] = [] if isinstance(r, Exception) or r is None else r

            # contexte marché
            market_cache['BTCUSDT'] = symbol_cache.get('BTCUSDT', {}).get('1h', [])
            market_cache['ETHUSDT'] = symbol_cache.get('ETHUSDT', {}).get('1h', [])
            update_market_state()

            # analyses
            await asyncio.gather(*(process_symbol(s) for s in SYMBOLS))
            await asyncio.gather(*(process_symbol_aggressive(s) for s in SYMBOLS if s not in trades))

            # flush du buffer HOLD
            await flush_hold_buffer()

            print("✔️ Itération terminée", flush=True)


        except Exception as e:
            await tg_send(f"⚠️ Erreur : {e}")

        await asyncio.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_loop())
