# -*- coding: utf-8 -*-
"""
RSI Scanner + Setup 3489 / Setup 3489 with M5 structure break.
Derived from the user's uploaded script. The legacy RSI, calendar, sessions
and heartbeat modules are retained and can be disabled independently.

3489 type 1: M15 EMA34/89 pullback/recovery + H4/D1 RSI confirmation.
3489 type 2: type 1 + a fresh confirmed LH/HL closing break on M5 in the
same completing M15 candle. No additional confluence or trade planning.

3489 uses closed bars with as-of higher-timeframe RSI and separate persistent
notification keys. Times displayed to the user are Vietnam time (UTC+7).
The scheduler must persist state.json between runs and avoid concurrent runs.
See README_setup3489.md for assumptions and testing limitations.
"""

import os
import json
import time
import math
import html
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
try:
    import yfinance as yf
except ModuleNotFoundError:
    yf = None  # Allows offline unit tests without the network adapter installed.
import requests

VN_TZ = timezone(timedelta(hours=7))
STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ============================================================================
# CẤU HÌNH CHUNG
# ============================================================================

# Mã trên Yahoo Finance. Nếu 1 mã không lấy được dữ liệu, bạn có thể
# đổi ticker ở đây (ví dụ Vàng có thể thử "XAUUSD=X" thay cho "GC=F").
SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "Vàng (XAU/USD)": "GC=F",
    "US30 (Dow Jones)": "YM=F",
    "US100 (Nasdaq)": "NQ=F",
    "AUD/USD": "AUDUSD=X",
    "NZD/USD": "NZDUSD=X",
    "EUR/GBP": "EURGBP=X",
    "USD/CAD": "USDCAD=X",
    "EUR/JPY": "EURJPY=X",
    "AUD/JPY": "AUDJPY=X",
    "BTC/USD": "BTC-USD",
    "Dầu (WTI)": "CL=F",
}

TIMEFRAMES = ["5m", "15m", "1h", "4h", "1D"]

RSI_PERIOD = 14
MAX_RETRIES = 3
RETRY_DELAY_SEC = 5
FAIL_ALERT_THRESHOLD = 6  # ~1 giờ liên tục lỗi mới báo

YFINANCE_DELAY_SEC = 0.7
TELEGRAM_DELAY_SEC = 1.2

# --- Ngưỡng RSI setup ---
# CANH SELL (thường): 5m trong 50-70, và 15m/1h/4h/1D đều < 50
SELL_5M_RANGE = (50, 70)
SELL_15M_MAX_NORMAL = 50
# CANH BUY (thường): 5m trong 30-50, và 15m/1h/4h/1D đều > 50
BUY_5M_RANGE = (30, 50)
BUY_15M_MIN_NORMAL = 50
# CANH SELL MẠNH: 5m > 70, 15m < 60, và 1h/4h/1D đều < 50
SELL_STRONG_5M_MIN = 70
SELL_STRONG_15M_MAX = 60
# CANH BUY MẠNH: 5m < 30, 15m > 40, và 1h/4h/1D đều > 50
BUY_STRONG_5M_MAX = 30
BUY_STRONG_15M_MIN = 40

# --- Lịch tin tức (module 3) ---
FOREXFACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_DIGEST_HOUR_VN = 8    # giờ VN gửi tổng hợp tin đỏ
CALENDAR_DIGEST_MINUTE_VN = 30  # phút VN gửi tổng hợp (8:30) - thống kê tin đỏ trong 24h tới
CALENDAR_MINUTES_BEFORE = 15  # báo thêm trước mỗi tin bao nhiêu phút
CALENDAR_ALERT_WINDOW_MIN = 5  # dung sai +-5 phút quanh mốc 15 phút (vì quét mỗi 10 phút)

# --- Phiên giao dịch (module 4) ---
SESSIONS = [
    {"name": "Sydney",   "tz": "Australia/Sydney", "open": (7, 0),  "close": (16, 0)},
    {"name": "Tokyo",    "tz": "Asia/Tokyo",        "open": (9, 0),  "close": (18, 0)},
    {"name": "London",   "tz": "Europe/London",     "open": (8, 0),  "close": (16, 30)},
    {"name": "New York", "tz": "America/New_York",  "open": (8, 0),  "close": (17, 0)},
]
SESSION_ALERT_WINDOW_MIN = 10  # bắt sự kiện trong vòng 10 phút sau giờ mở/đóng

# --- Ping định kỳ (module 5) ---
PING_HOURS_VN = [7]  # danh sách giờ VN sẽ gửi ping mỗi ngày, thêm số vào list nếu muốn nhiều lần/ngày

# --- Module switches: retain the other modules from the supplied script. ---
def env_flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name, "1" if default else "0").strip().lower()
    if value not in {"1", "0", "true", "false", "yes", "no"}:
        raise ValueError(f"Invalid boolean setting {name}={value!r}")
    return value in {"1", "true", "yes"}


ENABLE_RSI_MODULE = env_flag("ENABLE_RSI_MODULE")
ENABLE_CALENDAR_MODULE = env_flag("ENABLE_CALENDAR_MODULE")
ENABLE_SESSION_MODULE = env_flag("ENABLE_SESSION_MODULE")
ENABLE_PING_MODULE = env_flag("ENABLE_PING_MODULE")
ENABLE_3489_BASE = env_flag("ENABLE_3489_BASE")
ENABLE_3489_STRUCTURE = env_flag("ENABLE_3489_STRUCTURE")

# --- Setup 3489: no additional confluence filters or trade planning. ---
EMA_CROSS_TIMEFRAME = "15m"
EMA_CROSS_FAST = 34
EMA_CROSS_SLOW = 89
# Maximum number of M15 bars from the FIRST EMA89 touch to recovery.
# Retains the original script's 20-bar horizon; this is configurable.
EMA_CROSS_TOUCH_LOOKBACK = 20
EMA_CROSS_RSI_THRESHOLD = 50

# M5 swing definition: strictly greater/lower than two bars on each side.
# A pivot is usable only AFTER its right-side bars have closed.
STRUCTURE_SWING_LEFT = 2
STRUCTURE_SWING_RIGHT = 2
STRUCTURE_LOOKBACK_M5 = 240
# Strict "simultaneous": M5 closing breakout must occur inside the SAME
# M15 candle that completes 3489. Later M15 candles cannot upgrade this setup.

# Operational safeguards, NOT additional trading confluence filters.
CANDLE_CLOSE_GRACE_SECONDS = 30
MAX_SIGNAL_AGE_MINUTES = 30  # catch up recent bars, not an old historical flood
SENT_STATE_RETENTION_DAYS = 7
H4_ANCHOR_TIMEZONE = "UTC"   # synthetic H4: 00:00, 04:00, 08:00, ... UTC
H4_ANCHOR_OFFSET_HOURS = 0


# ============================================================================
# TIỆN ÍCH DÙNG CHUNG: STATE + TELEGRAM
# ============================================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("state.json must contain a JSON object")
    return data


def save_state(state):
    temporary = f"{STATE_FILE}.tmp.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, default=str, allow_nan=False)
    os.replace(temporary, STATE_FILE)


def send_telegram(text: str) -> bool:
    """True only after Telegram acknowledges success; never log the bot token."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID; not sent.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, data=payload, timeout=15)
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Invalid Telegram response")
        if response.status_code == 200 and body.get("ok") is True:
            return True
        description = str(body.get("description", "Unknown error")).replace(TELEGRAM_TOKEN, "[REDACTED]")
        print(f"[TELEGRAM] HTTP {response.status_code}: {description}")
    except (requests.RequestException, ValueError) as exc:
        # Exception text can contain the URL, including the secret token.
        print(f"[TELEGRAM] Request failed ({type(exc).__name__}); will retry 3489 next run.")
    finally:
        time.sleep(TELEGRAM_DELAY_SEC)
    return False


def now_vn_str():
    return datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M")


# ============================================================================
# MODULE DÙNG CHUNG: LẤY GIÁ + TÍNH RSI TỪ YAHOO FINANCE
# ============================================================================

def calc_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """RSI kiểu Wilder - giống cách hầu hết nền tảng (TradingView, MT4) tính."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def as_utc(value=None) -> pd.Timestamp:
    """Return a timezone-aware UTC timestamp; naive inputs mean UTC."""
    stamp = pd.Timestamp.now(tz="UTC") if value is None else pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def normalize_ohlc(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Normalize Yahoo bars and attach their conservative availability time.

    Intraday BarClose is bar start + duration. D1 becomes available at the
    next local midnight in the source index timezone, not the broker's session
    close. H4 is explicitly resampled in H4_ANCHOR_TIMEZONE. The returned frame
    can still contain a forming bar; only the new 3489 module removes it.
    This preserves the original RSI module's use of the latest Yahoo value.
    """
    if df is None or df.empty:
        raise RuntimeError("Empty OHLC data")
    d = df.copy()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    required = ["Open", "High", "Low", "Close"]
    if not all(col in d.columns for col in required):
        raise RuntimeError("Missing OHLC columns")
    d = d[required].apply(pd.to_numeric, errors="coerce")
    d = d.replace([float("inf"), -float("inf")], float("nan")).dropna()
    d.index = pd.DatetimeIndex(d.index)
    if d.index.tz is None:
        # ignore_tz=False normally supplies source timezone; log fallback.
        print("  [DATA] Naive timestamps: assuming UTC; verify source timezone.")
        d.index = d.index.tz_localize("UTC")
    d = d[~d.index.duplicated(keep="last")].sort_index()
    if d.empty:
        raise RuntimeError("No valid OHLC rows")
    if timeframe == "1D":
        # DateOffset, not Timedelta(24h), preserves midnight across DST.
        bar_close = d.index.normalize() + pd.DateOffset(days=1)
        d["BarClose"] = bar_close.tz_convert("UTC")
        d.index = d.index.tz_convert("UTC")
    else:
        if timeframe == "4h":
            d.index = d.index.tz_convert(H4_ANCHOR_TIMEZONE)
            d = d.resample(
                "4h", closed="left", label="left", origin="start_day",
                offset=pd.Timedelta(hours=H4_ANCHOR_OFFSET_HOURS),
            ).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
        durations = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h"}
        if timeframe not in durations:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        d.index = d.index.tz_convert("UTC")
        d["BarClose"] = d.index + pd.Timedelta(durations[timeframe])
    return d


def closed_bars(df: pd.DataFrame, as_of) -> pd.DataFrame:
    """BarClose must already be present; never silently guess D1 boundaries."""
    if df is None or df.empty:
        return pd.DataFrame()
    if "BarClose" not in df.columns:
        raise ValueError("Call normalize_ohlc before closed_bars")
    cutoff = as_utc(as_of)
    # A snapshot downloaded BEFORE a bar closed must not become a "closed"
    # candle merely because scanning other instruments took several minutes.
    fetched_at = df.attrs.get("fetched_at_utc")
    if fetched_at is not None:
        safe_snapshot = as_utc(fetched_at) - pd.Timedelta(seconds=CANDLE_CLOSE_GRACE_SECONDS)
        cutoff = min(cutoff, safe_snapshot)
    return df.loc[df["BarClose"] <= cutoff].copy()


def fetch_ohlc(ticker: str, timeframe: str, period_override: str = None) -> pd.DataFrame:
    """Fetch raw bars with explicit timezone handling; H4 uses hourly data."""
    if yf is None:
        raise RuntimeError("Missing yfinance: run python -m pip install yfinance pandas requests")
    settings = {
        "5m": ("5m", "5d"),
        "15m": ("15m", "1mo"),  # extra EMA warm-up, within intraday limits
        "1h": ("60m", "1mo"),
        "4h": ("60m", "1mo"),
        "1D": ("1d", "6mo"),
    }
    if timeframe not in settings:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    interval, period = settings[timeframe]
    fetched_at = as_utc()  # capture BEFORE request; conservative snapshot cutoff
    df = yf.download(
        ticker, interval=interval, period=period_override or period,
        progress=False, auto_adjust=False, ignore_tz=False,
        threads=False, timeout=20,
    )
    result = normalize_ohlc(df, timeframe)
    result.attrs["fetched_at_utc"] = fetched_at.isoformat()
    return result


def fetch_close_series(ticker: str, timeframe: str) -> pd.Series:
    return fetch_ohlc(ticker, timeframe)["Close"]


def fetch_ohlc_with_retry(ticker: str, timeframe: str, period_override: str = None, min_bars: int = 0):
    """Trả về (df_ohlc, err). Có thử lại khi lỗi tạm thời hoặc thiếu dữ liệu."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = fetch_ohlc(ticker, timeframe, period_override=period_override)
            if len(df) < min_bars:
                raise RuntimeError(f"Chỉ có {len(df)} nến, cần tối thiểu {min_bars} nến")
            time.sleep(YFINANCE_DELAY_SEC)
            return df, None
        except Exception as e:
            last_err = str(e)
            print(f"  -> Lỗi lần {attempt}/{MAX_RETRIES} ({ticker}, {timeframe}): {last_err}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC)
    return None, last_err


def fetch_ohlc_and_rsi_with_retry(ticker: str, timeframe: str):
    """Return (OHLC, original RSI series, error) with retries; reuse prices."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = fetch_ohlc(ticker, timeframe)
            rsi_series = calc_rsi(df["Close"])
            time.sleep(YFINANCE_DELAY_SEC)
            return df, rsi_series, None
        except Exception as e:
            last_err = str(e)
            print(f"  -> Lỗi lần {attempt}/{MAX_RETRIES} ({ticker}, {timeframe}): {last_err}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC)
    return None, None, last_err


# ============================================================================
# MODULE 1: RSI SETUP (CANH SELL / CANH BUY)
# ============================================================================

def check_setup(rsi: dict):
    """Trả về (setup, is_strong): setup là 'sell'/'buy'/None.
    Sell/Buy thường và Mạnh là 2 nhánh độc lập (không phải Mạnh nằm trong Thường)."""
    r5, r15, r1h, r4h, r1d = (rsi["5m"], rsi["15m"], rsi["1h"], rsi["4h"], rsi["1D"])

    sell_context = r1h < 50 and r4h < 50 and r1d < 50
    buy_context = r1h > 50 and r4h > 50 and r1d > 50

    if sell_context:
        if r5 > SELL_STRONG_5M_MIN and r15 < SELL_STRONG_15M_MAX:
            return "sell", True
        if SELL_5M_RANGE[0] <= r5 <= SELL_5M_RANGE[1] and r15 < SELL_15M_MAX_NORMAL:
            return "sell", False

    if buy_context:
        if r5 < BUY_STRONG_5M_MAX and r15 > BUY_STRONG_15M_MIN:
            return "buy", True
        if BUY_5M_RANGE[0] <= r5 <= BUY_5M_RANGE[1] and r15 > BUY_15M_MIN_NORMAL:
            return "buy", False

    return None, False


def format_rsi_alert(name: str, setup: str, rsi_values: dict, is_new: bool, is_strong: bool) -> str:
    if setup == "sell":
        emoji, label = ("🔴🔴", "CANH SELL MẠNH") if is_strong else ("🔴", "CANH SELL")
    else:
        emoji, label = ("🟢🟢", "CANH BUY MẠNH") if is_strong else ("🟢", "CANH BUY")

    trang_thai = "🆕 MỚI XUẤT HIỆN" if is_new else "🔁 ĐANG TIẾP DIỄN"

    lines = [f"{emoji} <b>{name}</b> — {label}", trang_thai]
    for tf in TIMEFRAMES:
        v = rsi_values.get(tf)
        lines.append(f"  • {tf}: {v:.1f}" if v is not None else f"  • {tf}: (n/a)")
    lines.append(f"🕒 {now_vn_str()} (giờ VN)")
    return "\n".join(lines)


def run_rsi_module(name, ticker, rsi_values, state, changed_flags):
    sym_state = state.setdefault("rsi", {}).setdefault(ticker, {"status": None})

    setup, is_strong = check_setup(rsi_values)
    old_status = sym_state.get("status")

    if setup is not None:
        is_new = (setup != old_status)
        send_telegram(format_rsi_alert(name, setup, rsi_values, is_new, is_strong))
        trang_thai_log = "MỚI XUẤT HIỆN" if is_new else "ĐANG TIẾP DIỄN"
        muc_do_log = "MẠNH" if is_strong else "thường"
        nhan_log = "CANH SELL" if setup == "sell" else "CANH BUY"
        print(f"  -> [RSI] ĐÃ GỬI CẢNH BÁO ({nhan_log}, mức {muc_do_log}, {trang_thai_log})")
    else:
        print("  -> [RSI] Chưa thoả điều kiện Canh Sell / Canh Buy")

    if setup != old_status:
        changed_flags["changed"] = True

    sym_state["status"] = setup
    sym_state["last_rsi"] = rsi_values
    sym_state["last_check"] = now_vn_str()


# ============================================================================
# MODULE: SETUP 3489 + SETUP 3489 / M5 STRUCTURE BREAK
# ============================================================================

def calc_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def detect_3489_patterns(df_15m: pd.DataFrame) -> list:
    """Chronological state machine on CLOSED M15 bars, without RSI yet.

    BUY: maintain EMA34 > EMA89, first observe a close above both; then price
    pulls through EMA34 and touches/passes EMA89; the FIRST close back above
    both completes that pullback. SELL is the exact mirror. High/Low count
    for touches, Close counts for recovery. A touch-and-recovery in one M15
    bar is allowed if a favorable close was already observed before it.

    A regime flip, equality of EMAs, or >20 bars since first touch invalidates
    the pending pullback. Recovery consumes it even when the later RSI gate
    rejects the signal: the same touch can never be recycled.
    """
    if df_15m is None or len(df_15m) < EMA_CROSS_SLOW + 1:
        return []
    if EMA_CROSS_TOUCH_LOOKBACK < 1:
        raise ValueError("EMA_CROSS_TOUCH_LOOKBACK must be positive")
    fast = calc_ema(df_15m["Close"], EMA_CROSS_FAST).to_numpy()
    slow = calc_ema(df_15m["Close"], EMA_CROSS_SLOW).to_numpy()
    close = df_15m["Close"].to_numpy()
    low = df_15m["Low"].to_numpy()
    high = df_15m["High"].to_numpy()
    times = df_15m.index
    ends = df_15m["BarClose"]
    events = []
    regime = None
    armed = False
    touch_idx = None
    pullback_idx = None
    for i in range(EMA_CROSS_SLOW - 1, len(df_15m)):
        side = "buy" if fast[i] > slow[i] else "sell" if fast[i] < slow[i] else None
        if side != regime or side is None:
            regime, armed, touch_idx, pullback_idx = side, False, None, None
        if side is None:
            continue
        favorable = close[i] > max(fast[i], slow[i]) if side == "buy" else close[i] < min(fast[i], slow[i])
        crosses_fast = low[i] <= fast[i] if side == "buy" else high[i] >= fast[i]
        touches_slow = low[i] <= slow[i] if side == "buy" else high[i] >= slow[i]
        if touch_idx is not None and i - touch_idx > EMA_CROSS_TOUCH_LOOKBACK:
            armed, touch_idx, pullback_idx = False, None, None
        if armed:
            if crosses_fast and pullback_idx is None:
                pullback_idx = i
            if touches_slow and touch_idx is None:
                touch_idx = i
            if favorable and touch_idx is not None:
                events.append({
                    "side": side,
                    "bar_start": times[i].isoformat(),
                    "bar_close": as_utc(ends.iloc[i]).isoformat(),
                    "pullback_start": times[pullback_idx if pullback_idx is not None else touch_idx].isoformat(),
                    "touch_time": times[touch_idx].isoformat(),
                    "touch_price": float(low[touch_idx] if side == "buy" else high[touch_idx]),
                    "touch_bars_ago": i - touch_idx,
                    "ema_fast": float(fast[i]),
                    "ema_slow": float(slow[i]),
                })
        if favorable:
            armed, touch_idx, pullback_idx = True, None, None
    return events


def rsi_asof(frame: pd.DataFrame, moment):
    """Return (RSI, RSI bar close time), using only data available at moment."""
    d = closed_bars(frame, moment)
    if d.empty or len(d) < RSI_PERIOD + 1:
        return None, None
    series = calc_rsi(d["Close"])
    value = series.iloc[-1]
    if pd.isna(value) or not math.isfinite(float(value)):
        return None, None
    return float(value), as_utc(d["BarClose"].iloc[-1]).isoformat()


def qualify_3489(event: dict, h4: pd.DataFrame, d1: pd.DataFrame):
    """The only momentum filters: H4 and D1 RSI strictly above/below 50."""
    r4, t4 = rsi_asof(h4, event["bar_close"])
    rd, td = rsi_asof(d1, event["bar_close"])
    if r4 is None or rd is None:
        return None
    threshold = EMA_CROSS_RSI_THRESHOLD
    ok = r4 > threshold and rd > threshold if event["side"] == "buy" else r4 < threshold and rd < threshold
    if not ok:
        return None
    return dict(event, rsi_h4=r4, rsi_d1=rd, rsi_h4_bar_close=t4, rsi_d1_bar_close=td)


def check_structure_break(df_5m: pd.DataFrame, event: dict):
    """First fresh closing break of the nearest active confirmed LH/HL.

    A BUY candidate is the latest confirmed swing high only if it is lower
    than the immediately preceding swing high. For SELL, use the latest low
    only if higher than the preceding swing low. Equal levels are neither.
    A newly confirmed non-LH/non-HL clears the prior candidate. Once a level
    has been broken on a close, a later recross does not make it fresh again.

    Pivot confirmation must PRECEDE the breakout candle. Detect each pivot
    using only candles that had closed by then, avoiding future-bar leakage.
    Require the breakout's M5 close in (M15 start, M15 close]. No pending
    upgrade across later M15 candles. Missing M5 data affects only type 2.
    """
    if df_5m is None or df_5m.empty:
        return None
    start, end = as_utc(event["bar_start"]), as_utc(event["bar_close"])
    d = closed_bars(df_5m, end).tail(STRUCTURE_LOOKBACK_M5)
    # The three constituent M5 bars must be available. A data gap is not a
    # negative signal; run_3489_module can retry type 2 during the catch-up window.
    required = pd.date_range(start, periods=3, freq="5min")
    if not required.isin(d.index).all():
        return None
    left, right = STRUCTURE_SWING_LEFT, STRUCTURE_SWING_RIGHT
    if min(left, right) < 1:
        raise ValueError("Swing left/right parameters must be positive")
    buy = event["side"] == "buy"
    values = d["High" if buy else "Low"].to_numpy()
    closes = d["Close"].to_numpy()
    ends = d["BarClose"]
    previous_pivot = None
    candidate = None
    for j in range(1, len(d)):
        # p+right == j-1: the right-hand confirmation bars closed BEFORE j.
        p = j - right - 1
        if p >= left:
            window = values[p-left:p+right+1]
            v = values[p]
            unique = int((window == v).sum()) == 1
            extremum = v == (window.max() if buy else window.min())
            if unique and extremum:
                is_structure = previous_pivot is not None and (
                    v < previous_pivot if buy else v > previous_pivot
                )
                candidate = {
                    "kind": "LH" if buy else "HL",
                    "level": float(v),
                    "previous_level": float(previous_pivot),
                    "pivot_time": d.index[p].isoformat(),
                    "confirmed_at": as_utc(ends.iloc[p+right]).isoformat(),
                    "broken": False,
                } if is_structure else None
                previous_pivot = float(v)
        if candidate is None or candidate["broken"]:
            continue
        level = candidate["level"]
        crossed = closes[j-1] <= level < closes[j] if buy else closes[j-1] >= level > closes[j]
        if not crossed:
            continue
        candidate["broken"] = True
        breakout_time = as_utc(ends.iloc[j])
        if start < breakout_time <= end:
            return {
                "kind": candidate["kind"], "level": level,
                "previous_level": candidate["previous_level"],
                "pivot_time": candidate["pivot_time"],
                "confirmed_at": candidate["confirmed_at"],
                "break_bar_start": d.index[j].isoformat(),
                "break_bar_close": breakout_time.isoformat(),
                "break_close": float(closes[j]),
            }
    return None


def vn_timestamp(value) -> str:
    return as_utc(value).tz_convert(VN_TZ).strftime("%d/%m/%Y %H:%M")


def format_3489_alert(name: str, ticker: str, info: dict, structure=None) -> str:
    """Two independent alert labels. No order suggestions or trade levels."""
    side = info["side"].upper()
    icon = "\U0001f7e2" if side == "BUY" else "\U0001f534"
    relation = "&gt;" if side == "BUY" else "&lt;"
    position = "tr\u00ean" if side == "BUY" else "d\u01b0\u1edbi"
    number = 2 if structure else 1
    title = "SETUP 3489" if structure is None else f"SETUP 3489 + PH\u00c1 {structure['kind']} M5"
    lines = [
        f"{icon} <b>T\u00cdN HI\u1ec6U {number} | {title} | {side}</b>",
        f"<b>{html.escape(name)}</b> ({html.escape(ticker)})",
        f"M15: EMA34 {relation} EMA89; gi\u00e1 \u0111\u00e3 ch\u1ea1m/c\u1eaft EMA89 v\u00e0 \u0111\u00f3ng c\u1eeda tr\u1edf l\u1ea1i {position} c\u1ea3 hai EMA.",
        f"EMA34: {info['ema_fast']:.5f} | EMA89: {info['ema_slow']:.5f}",
        f"RSI H4: {info['rsi_h4']:.2f} | RSI D1: {info['rsi_d1']:.2f}",
        f"Ch\u1ea1m EMA89: n\u1ebfn M15 m\u1edf {vn_timestamp(info['touch_time'])}",
        f"M15 x\u00e1c nh\u1eadn \u0111\u00f3ng: {vn_timestamp(info['bar_close'])}",
    ]
    if structure:
        lines.extend([
            f"{structure['kind']} M5: {structure['level']:.5f} (swing tr\u01b0\u1edbc: {structure['previous_level']:.5f})",
            f"N\u1ebfn M5 ph\u00e1 \u0111\u00f3ng: {vn_timestamp(structure['break_bar_close'])}",
            f"Close M5 ph\u00e1: {structure['break_close']:.5f}",
        ])
    lines.extend([
        f"RSI H4/D1 d\u00f9ng n\u1ebfn \u0111\u00e3 \u0111\u00f3ng t\u1ea1i th\u1eddi \u0111i\u1ec3m M15 x\u00e1c nh\u1eadn.",
        f"\U0001f552 G\u1eedi: {now_vn_str()} | To\u00e0n b\u1ed9 gi\u1edd VN (UTC+7)",
    ])
    return "\n".join(lines)


def run_3489_module(name: str, ticker: str, frames: dict, state: dict,
                    now=None, sender=None, checkpoint=None) -> dict:
    """Replay recent closed patterns, qualify as-of, send each type once.

    Retry unsent events only while they remain inside MAX_SIGNAL_AGE_MINUTES.
    State keeps separate keys for base/structure, so a missing M5 response or
    a failed base delivery does not suppress the other notification.
    checkpoint is called after every acknowledged send (main saves locally).
    An ambiguous network timeout can still duplicate a Telegram delivery;
    Bot API sendMessage does not provide an application idempotency key.
    """
    now = as_utc(now)
    cutoff = now - pd.Timedelta(seconds=CANDLE_CLOSE_GRACE_SECONDS)
    oldest = now - pd.Timedelta(minutes=MAX_SIGNAL_AGE_MINUTES)
    stats = {"base_sent": 0, "structure_sent": 0, "delivery_failed": 0}
    if not (ENABLE_3489_BASE or ENABLE_3489_STRUCTURE):
        return stats
    if any(frames.get(tf) is None or frames[tf].empty for tf in ("15m", "4h", "1D")):
        print("  [3489] Missing M15/H4/D1; skipped. M5 is NOT required for type 1.")
        return stats
    d15 = closed_bars(frames["15m"], cutoff)
    events = detect_3489_patterns(d15)
    storage = state.setdefault("setup_3489", {}).setdefault(ticker, {"sent": {}})
    sent = storage.setdefault("sent", {})
    retention = now - pd.Timedelta(days=SENT_STATE_RETENTION_DAYS)
    for key, stamp in list(sent.items()):
        try:
            expired = as_utc(stamp) < retention
        except (ValueError, TypeError):
            expired = True
        if expired:
            del sent[key]
    deliver = send_telegram if sender is None else sender
    for event in events:
        event_time = as_utc(event["bar_close"])
        if event_time < oldest:
            continue
        info = qualify_3489(event, frames["4h"], frames["1D"])
        if info is None:
            continue
        event_id = f"{event['side']}|{event['bar_close']}"
        # Send the independent base branch BEFORE inspecting M5, so corrupt
        # M5 data cannot suppress an otherwise valid type-1 notification.
        for kind, enabled in (("base", ENABLE_3489_BASE), ("structure", ENABLE_3489_STRUCTURE)):
            key = f"{kind}|{event_id}"
            if not enabled or key in sent:
                continue
            detail = None
            if kind == "structure":
                try:
                    detail = check_structure_break(frames.get("5m"), event)
                except (ValueError, KeyError, TypeError, IndexError) as exc:
                    print(f"  [3489] M5 processing unavailable ({type(exc).__name__}); type 1 unaffected.")
                    continue
                if detail is None:
                    continue
            if deliver(format_3489_alert(name, ticker, info, detail)):
                sent[key] = now.isoformat()
                stats[f"{kind}_sent"] += 1
                if checkpoint is not None:
                    checkpoint()
                print(f"  [3489] Sent {kind} {event['side'].upper()} at {event['bar_close']}")
            else:
                stats["delivery_failed"] += 1
    storage["last_check"] = now.isoformat()
    if not any(stats.values()):
        print("  [3489] No new eligible alerts (or already sent).")
    return stats


# ============================================================================
# MODULE 3: LỊCH TIN TỨC FOREX FACTORY
# ============================================================================

def fetch_forexfactory_calendar():
    try:
        r = requests.get(FOREXFACTORY_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code == 200:
            return r.json()
        print(f"  [Lịch tin tức] HTTP {r.status_code} khi tải lịch Forex Factory")
        return None
    except Exception as e:
        print(f"  [Lịch tin tức] Lỗi khi tải lịch: {e}")
        return None


def parse_event_datetime(event: dict):
    raw = event.get("date")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def parse_calendar_number(raw):
    """Phân tích số từ chuỗi kiểu '1.2%', '150K', '-0.3', '-' ... Trả về None nếu không parse được."""
    if not raw or str(raw).strip() in ("-", ""):
        return None
    s = str(raw).strip().replace(",", "").replace("%", "")
    mult = 1
    if s and s[-1].upper() == "K":
        mult, s = 1_000, s[:-1]
    elif s and s[-1].upper() == "M":
        mult, s = 1_000_000, s[:-1]
    elif s and s[-1].upper() == "B":
        mult, s = 1_000_000_000, s[:-1]
    try:
        return float(s) * mult
    except (ValueError, TypeError):
        return None


def guess_calendar_direction(event: dict) -> str:
    """Dự đoán RẤT THÔ dựa trên so sánh Dự báo vs Kỳ trước - KHÔNG phải dự đoán
    thực sự vì kết quả thực tế (Actual) chỉ biết sau khi tin ra. Chỉ mang tính
    tham khảo về xu hướng kỳ vọng, không phải khuyến nghị giao dịch."""
    f = parse_calendar_number(event.get("forecast"))
    p = parse_calendar_number(event.get("previous"))
    if f is None or p is None:
        return "Không đủ số liệu (thiếu Dự báo hoặc Kỳ trước) để ước đoán xu hướng."
    if f > p:
        return "Dự báo > Kỳ trước → nếu số thực tế khớp/vượt dự báo, thường nghiêng về TĂNG cho đồng tiền này (dự đoán rất thô, chỉ tham khảo)."
    if f < p:
        return "Dự báo < Kỳ trước → nếu số thực tế khớp/thấp hơn dự báo, thường nghiêng về GIẢM cho đồng tiền này (dự đoán rất thô, chỉ tham khảo)."
    return "Dự báo bằng Kỳ trước → chưa có nghiêng rõ ràng (dự đoán rất thô, chỉ tham khảo)."


def format_calendar_reminder(event: dict, dt_vn: datetime) -> str:
    title = event.get("title", "(không rõ tên tin)")
    country = event.get("country", "")
    forecast = event.get("forecast") or "-"
    previous = event.get("previous") or "-"
    time_str = dt_vn.strftime("%H:%M")

    lines = [
        f"🔔 SẮP CÓ TIN QUAN TRỌNG (còn ~{CALENDAR_MINUTES_BEFORE} phút)",
        f"🔴 <b>{country} — {title}</b>",
        f"🕒 Giờ tin: {time_str} (giờ VN)",
        f"Dự báo: {forecast}  |  Kỳ trước: {previous}",
        f"📊 {guess_calendar_direction(event)}",
    ]
    return "\n".join(lines)


def format_calendar_digest(events_window: list, window_start: datetime, window_end: datetime) -> str:
    range_str = f"{window_start.strftime('%H:%M %d/%m')} → {window_end.strftime('%H:%M %d/%m')}"
    if not events_window:
        return f"📅 Không có tin QUAN TRỌNG (đỏ) nào trong 24h tới ({range_str}).\n🕒 {now_vn_str()} (giờ VN)"

    lines = [f"📅 <b>TIN QUAN TRỌNG (ĐỎ) TRONG 24H TỚI</b> ({range_str}):"]
    for event, dt_vn in sorted(events_window, key=lambda x: x[1]):
        title = event.get("title", "(không rõ tên tin)")
        country = event.get("country", "")
        forecast = event.get("forecast") or "-"
        previous = event.get("previous") or "-"
        lines.append(
            f"  🔴 {dt_vn.strftime('%H:%M %d/%m')} — {country} {title} "
            f"(Dự báo: {forecast}, Kỳ trước: {previous})"
        )
    lines.append(f"🕒 {now_vn_str()} (giờ VN)")
    return "\n".join(lines)


def run_calendar_module(state):
    now_vn = datetime.now(VN_TZ)
    year, week, _ = now_vn.isocalendar()
    week_key = f"{year}-W{week:02d}"

    cal = state.setdefault(
        "calendar",
        {"events": [], "fetched_week": None, "notified": {}, "digest_sent_date": None},
    )

    if cal.get("fetched_week") != week_key:
        print(f"\n=== [Lịch tin tức] Tuần mới ({week_key}) -> tải lịch Forex Factory ===")
        raw_events = fetch_forexfactory_calendar()
        if raw_events is not None:
            high_events = [e for e in raw_events if str(e.get("impact", "")).strip().lower() == "high"]
            cal["events"] = high_events
            cal["fetched_week"] = week_key
            cal["notified"] = {}
            print(f"  -> Đã tải {len(high_events)} tin QUAN TRỌNG (đỏ) cho tuần này")
        else:
            print("  -> Lỗi tải lịch, sẽ tự thử lại ở lần quét kế tiếp")

    # Toàn bộ tin đỏ đã cache trong tuần, kèm giờ VN tương ứng
    all_events = []
    for event in cal.get("events", []):
        dt = parse_event_datetime(event)
        if dt is None:
            continue
        all_events.append((event, dt.astimezone(VN_TZ)))

    # --- Tổng hợp 1 lần/ngày lúc 8h30 giờ VN: thống kê tin đỏ trong 24H TỚI
    # (từ 8h30 hôm nay đến 8h30 hôm sau) - khắc phục việc bỏ lỡ tin từ 0h-6h59
    # mà bản digest "hôm nay lúc 7h" trước đây gặp phải.
    today_str = now_vn.date().isoformat()
    if (now_vn.hour == CALENDAR_DIGEST_HOUR_VN and now_vn.minute >= CALENDAR_DIGEST_MINUTE_VN
            and cal.get("digest_sent_date") != today_str):
        window_start = now_vn.replace(hour=CALENDAR_DIGEST_HOUR_VN, minute=CALENDAR_DIGEST_MINUTE_VN,
                                       second=0, microsecond=0)
        window_end = window_start + timedelta(hours=24)
        events_window = [(e, dt_vn) for e, dt_vn in all_events if window_start <= dt_vn < window_end]
        send_telegram(format_calendar_digest(events_window, window_start, window_end))
        cal["digest_sent_date"] = today_str
        print(f"  -> [Lịch tin tức] Đã gửi tổng hợp 8h30 ({len(events_window)} tin đỏ trong 24h tới)")

    # --- Báo thêm trước mỗi tin khoảng 15 phút - áp dụng cho MỌI tin đã cache trong
    # tuần (không phụ thuộc digest), nên tin nào cũng được nhắc trước đúng giờ dù rơi
    # vào ngày nào hay có nằm trong cửa sổ digest gần nhất hay không.
    notified = cal.setdefault("notified", {})
    fired = 0

    for event, dt_vn in all_events:
        eid = f"{event.get('country')}|{event.get('title')}|{dt_vn.isoformat()}"
        diff_min = (dt_vn - now_vn).total_seconds() / 60

        key_before = eid + "|before"
        lo = CALENDAR_MINUTES_BEFORE - CALENDAR_ALERT_WINDOW_MIN
        hi = CALENDAR_MINUTES_BEFORE + CALENDAR_ALERT_WINDOW_MIN
        if lo <= diff_min <= hi and key_before not in notified:
            send_telegram(format_calendar_reminder(event, dt_vn))
            notified[key_before] = True
            fired += 1

    if fired:
        print(f"  -> [Lịch tin tức] Đã gửi {fired} cảnh báo trước-15-phút")
    else:
        print("  [Lịch tin tức] Không có tin nào cần báo trước lúc này")


# ============================================================================
# MODULE 4: CẢNH BÁO MỞ/ĐÓNG PHIÊN GIAO DỊCH
# ============================================================================

def prune_session_state(sess_state: dict, days=3):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    to_del = []
    for k in sess_state:
        try:
            date_part = k.rsplit("_", 1)[-1]
            d = datetime.fromisoformat(date_part).date()
            if d < cutoff:
                to_del.append(k)
        except Exception:
            pass
    for k in to_del:
        del sess_state[k]


def run_session_module(state):
    print("\n=== [Phiên giao dịch] Kiểm tra mở/đóng phiên ===")
    sess_state = state.setdefault("sessions", {})
    prune_session_state(sess_state)

    now_utc = datetime.now(timezone.utc)
    fired = 0

    for s in SESSIONS:
        tz = ZoneInfo(s["tz"])
        local_now = now_utc.astimezone(tz)
        date_str = local_now.date().isoformat()

        for kind, (hh, mm) in (("open", s["open"]), ("close", s["close"])):
            target = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            diff_min = (local_now - target).total_seconds() / 60
            key = f"{s['name']}_{kind}_{date_str}"

            if 0 <= diff_min <= SESSION_ALERT_WINDOW_MIN and key not in sess_state:
                label = "MỞ CỬA" if kind == "open" else "ĐÓNG CỬA"
                emoji = "🟢" if kind == "open" else "⚪"
                text = f"{emoji} Phiên <b>{s['name']}</b> vừa {label}\n🕒 {now_vn_str()} (giờ VN)"
                send_telegram(text)
                sess_state[key] = True
                fired += 1

    print(f"  -> Đã gửi {fired} cảnh báo phiên" if fired else "  -> Không có phiên nào vừa mở/đóng")


# ============================================================================
# MODULE 5: PING ĐỊNH KỲ
# ============================================================================

def run_ping_module(state):
    ping_state = state.setdefault("ping", {})
    now_vn = datetime.now(VN_TZ)
    date_str = now_vn.date().isoformat()

    for hour in PING_HOURS_VN:
        key = f"{date_str}_{hour}"
        if now_vn.hour == hour and key not in ping_state:
            text = f"✅ Hệ thống RSI Scanner vẫn đang hoạt động bình thường.\n🕒 {now_vn_str()} (giờ VN)"
            send_telegram(text)
            ping_state[key] = True
            print("\n=== [Ping] Đã gửi ping xác nhận hệ thống còn sống ===")

    cutoff = (now_vn - timedelta(days=3)).date().isoformat()
    for k in list(ping_state.keys()):
        if k.split("_")[0] < cutoff:
            del ping_state[k]


# ============================================================================
# MAIN
# ============================================================================

def main():
    state = load_state()
    changed_flags = {"changed": False}
    if yf is None:
        raise SystemExit("Missing yfinance. Run: python -m pip install -r requirements_setup3489.txt")
    # No cross-symbol phase: each instrument is now fully independent.
    required_tfs = TIMEFRAMES if ENABLE_RSI_MODULE else ["15m", "4h", "1D"]
    if ENABLE_3489_STRUCTURE and "5m" not in required_tfs:
        required_tfs = ["5m"] + required_tfs
    try:
        for name, ticker in SYMBOLS.items():
            print(f"\n=== Scanning {name} ({ticker}) ===")
            rsi_values, frames, error_tf = {}, {}, []
            for tf in required_tfs:
                df, rsi_series, err = fetch_ohlc_and_rsi_with_retry(ticker, tf)
                if err:
                    error_tf.append(tf)
                    continue
                frames[tf] = df
                latest = rsi_series.iloc[-1] if len(rsi_series) else float("nan")
                if pd.isna(latest) or not math.isfinite(float(latest)):
                    error_tf.append(tf)
                else:
                    rsi_values[tf] = float(latest)
                    print(f"  {tf}: RSI = {float(latest):.2f}")
            fail_state = state.setdefault("rsi_fail", {}).setdefault(ticker, {"fail_count": 0, "fail_notified": False})
            if error_tf:
                fail_state["fail_count"] += 1
                print(f"  Missing/invalid data: {error_tf}; failed scans={fail_state['fail_count']}")
                if fail_state["fail_count"] >= FAIL_ALERT_THRESHOLD and not fail_state["fail_notified"]:
                    ok = send_telegram(
                        f"\u26a0\ufe0f <b>{html.escape(name)}</b>: thi\u1ebfu d\u1eef li\u1ec7u "
                        f"{', '.join(error_tf)} trong {fail_state['fail_count']} l\u1ea7n qu\u00e9t li\u00ean ti\u1ebfp."
                    )
                    if ok:
                        fail_state["fail_notified"] = True
            else:
                fail_state.update(fail_count=0, fail_notified=False)
            if ENABLE_RSI_MODULE and all(tf in rsi_values for tf in TIMEFRAMES):
                run_rsi_module(name, ticker, rsi_values, state, changed_flags)
            # A missing 5m/1h frame must never block the base 3489 branch.
            try:
                run_3489_module(name, ticker, frames, state, checkpoint=lambda: save_state(state))
            except Exception as exc:
                # Do not print untrusted exception URLs or lose other instruments.
                print(f"  [3489] Processing failed: {type(exc).__name__}; retry next scan.")
            save_state(state)
        if ENABLE_CALENDAR_MODULE:
            run_calendar_module(state)
        if ENABLE_SESSION_MODULE:
            run_session_module(state)
        if ENABLE_PING_MODULE:
            run_ping_module(state)
    finally:
        save_state(state)
        print("\nSaved state.json")


if __name__ == "__main__":
    main()
