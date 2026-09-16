# -*- coding: utf-8 -*-
"""
RSI Scanner + 4 module cảnh báo bổ sung - chạy mỗi 10 phút qua GitHub Actions.

Các module trong file này:
  1. RSI SETUP (CANH SELL / CANH BUY) - có nhãn MẠNH.
  2. LỊCH TIN TỨC Forex Factory - tổng hợp tin đỏ hôm nay lúc 7h sáng, và
     báo thêm 1 lần trước mỗi tin khoảng 15 phút. Dữ liệu tải 1 lần/tuần.
  3. CẢNH BÁO MỞ/ĐÓNG PHIÊN giao dịch (Sydney/Tokyo/London/New York).
  4. PING ĐỊNH KỲ mỗi ngày để xác nhận hệ thống còn sống.
  5. SONIC R (EMA34 Dragon + SMA89/SMA200 Trend + CCI lọc entry) trên khung H1.

Toàn bộ giờ hiển thị là giờ Việt Nam (UTC+7). Trạng thái được lưu chung
trong 1 file state.json (nhiều "ngăn" riêng cho từng module) để commit
lại vào repo không cần sửa workflow.
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
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
CALENDAR_DIGEST_HOUR_VN = 7   # giờ VN gửi tổng hợp tin đỏ hôm nay
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

# --- Sonic R (module 6) ---
# Hệ thống Sonic R bản gốc: EMA34 "Dragon" + SMA89/SMA200 "Trend" + CCI(14) lọc entry.
# Chạy trên khung 1 giờ (H1) - đây là khung phổ biến nhất mà bản gốc Sonic R dùng làm
# khung giao dịch chính cho cả 3 chỉ báo.
SONIC_TIMEFRAME = "1h"
SONIC_HISTORY_PERIOD = "6mo"  # cần đủ dữ liệu để tính SMA200 (đặt dư ra cho chắc)
SONIC_EMA_DRAGON = 34
SONIC_SMA_TREND_FAST = 89
SONIC_SMA_TREND_SLOW = 200
SONIC_CCI_PERIOD = 14
SONIC_CCI_EXTREME = 200  # CCI vượt mức này gần đây -> cảnh báo có thể là pha đảo chiều mạnh, không phải pullback thường

# --- Bộ lọc hợp lưu bổ sung cho Sonic R (khắc phục entry ngược hỗ trợ/kháng cự) ---
SONIC_HTF_TIMEFRAME = "4h"     # khung lớn hơn dùng để xác nhận xu hướng (lọc tín hiệu ngược xu hướng lớn)
SONIC_HTF_CONFIRM = True       # bắt buộc khung 4h phải cùng chiều xu hướng với H1 mới cho tín hiệu

SR_SWING_ORDER_NEAR = 4        # số nến mỗi bên để xác nhận đỉnh/đáy trên khung H1 (dùng đặt SL)
SR_LOOKBACK_NEAR = 150         # số nến H1 gần nhất được xét để tìm đỉnh/đáy gần (SL)
SR_SWING_ORDER_FAR = 5         # số nến mỗi bên để xác nhận đỉnh/đáy trên khung 4h (dùng lọc entry + đặt TP)
SR_LOOKBACK_FAR = 180          # số nến 4h gần nhất được xét (~30 ngày) để tìm vùng hỗ trợ/kháng cự lớn
SR_PROXIMITY_ATR_MULT = 0.5    # nếu giá cách 1 vùng hỗ trợ/kháng cự đối nghịch < 0.5 x ATR -> HUỶ tín hiệu

SL_BUFFER_ATR_MULT = 0.2       # đệm thêm ngoài đỉnh/đáy gần nhất khi đặt Stop Loss
RR_TARGET_MIN = 2.0            # chỉ báo tín hiệu nếu R:R ước tính đạt tối thiểu 1:2

# --- Xác nhận chéo giữa các mã tương quan ---
CORRELATION_LOOKBACK_DAYS = 30  # số ngày gần nhất dùng để tính tương quan (dựa trên % thay đổi giá đóng cửa ngày)
CORRELATION_THRESHOLD = 0.7     # |tương quan| >= ngưỡng này mới được coi là "tương quan mạnh"


# ============================================================================
# TIỆN ÍCH DÙNG CHUNG: STATE + TELEGRAM
# ============================================================================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)


def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[CẢNH BÁO] Thiếu TELEGRAM_TOKEN hoặc TELEGRAM_CHAT_ID -> không gửi được tin nhắn.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code != 200:
            print(f"[LỖI] Gửi Telegram thất bại: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[LỖI] Gửi Telegram gặp ngoại lệ: {e}")
    finally:
        time.sleep(TELEGRAM_DELAY_SEC)


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


def fetch_ohlc(ticker: str, timeframe: str, period_override: str = None) -> pd.DataFrame:
    """Lấy dữ liệu OHLC cho 1 khung thời gian. Khung 4h được tự tổng hợp từ dữ liệu 1h."""
    if timeframe == "5m":
        interval, period = "5m", "5d"
    elif timeframe == "15m":
        interval, period = "15m", "5d"
    elif timeframe == "1h":
        interval, period = "60m", "1mo"
    elif timeframe == "4h":
        interval, period = "60m", "3mo"
    elif timeframe == "1D":
        interval, period = "1d", "6mo"
    else:
        raise ValueError(f"Khung thời gian không hỗ trợ: {timeframe}")

    if period_override:
        period = period_override

    df = yf.download(ticker, interval=interval, period=period, progress=False, auto_adjust=False)

    if df is None or df.empty:
        raise RuntimeError(f"Không có dữ liệu cho {ticker} khung {timeframe}")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    if timeframe == "4h":
        df = df.resample("4h").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum",
        }).dropna()

    return df[["Open", "High", "Low", "Close"]].dropna()


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
    """Trả về (df_ohlc, rsi_series, err). Giữ nguyên cả OHLC (không chỉ Close) để
    có thể tái sử dụng High/Low cho việc tìm vùng hỗ trợ/kháng cự ở chỗ khác mà
    không cần gọi thêm Yahoo Finance. Có thử lại khi lỗi tạm thời."""
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
# MODULE 6: SONIC R (EMA34 Dragon + SMA89/SMA200 Trend + CCI lọc entry)
# ============================================================================

def calc_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def calc_sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


def calc_cci(df: pd.DataFrame, period: int = SONIC_CCI_PERIOD) -> pd.Series:
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    sma_tp = tp.rolling(period).mean()
    mean_dev = tp.rolling(period).apply(lambda x: (x - x.mean()).abs().mean())
    return (tp - sma_tp) / (0.015 * mean_dev)


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def get_trend_regime(close: pd.Series):
    """Trả về 'up' / 'down' / 'none' theo đúng luật xu hướng Sonic R (EMA34 vs SMA89 vs SMA200)."""
    if close.dropna().shape[0] < SONIC_SMA_TREND_SLOW + 1:
        return None
    ema34 = calc_ema(close, SONIC_EMA_DRAGON)
    sma89 = calc_sma(close, SONIC_SMA_TREND_FAST)
    sma200 = calc_sma(close, SONIC_SMA_TREND_SLOW)
    if pd.isna(ema34.iloc[-1]) or pd.isna(sma89.iloc[-1]) or pd.isna(sma200.iloc[-1]):
        return None
    if ema34.iloc[-1] > sma89.iloc[-1] > sma200.iloc[-1]:
        return "up"
    if ema34.iloc[-1] < sma89.iloc[-1] < sma200.iloc[-1]:
        return "down"
    return "none"


def find_swing_levels(df: pd.DataFrame, order: int, lookback: int):
    """Trả về (danh sách giá đáy/support, danh sách giá đỉnh/resistance) đã xác nhận
    trong `lookback` nến gần nhất, dùng High/Low thực của nến (không phải Close)."""
    d = df.tail(lookback)
    h, l = d["High"].values, d["Low"].values
    n = len(d)
    supports, resistances = [], []
    for i in range(order, n - order):
        window_h = h[i - order:i + order + 1]
        window_l = l[i - order:i + order + 1]
        if h[i] == window_h.max():
            resistances.append(float(h[i]))
        if l[i] == window_l.min():
            supports.append(float(l[i]))
    return sorted(set(supports)), sorted(set(resistances))


def nearest_adverse_level(price: float, levels: list, atr: float, mult: float):
    """Trả về mức giá gần nhất trong phạm vi atr*mult quanh price, hoặc None nếu không có."""
    if not levels or atr is None or pd.isna(atr) or atr <= 0:
        return None
    threshold = atr * mult
    nearby = [lvl for lvl in levels if abs(lvl - price) <= threshold]
    if not nearby:
        return None
    return min(nearby, key=lambda lvl: abs(lvl - price))


def nearest_target_level(price: float, levels: list, direction: str):
    """direction='up': tìm mức gần nhất PHÍA TRÊN price (làm TP cho lệnh BUY).
    direction='down': tìm mức gần nhất PHÍA DƯỚI price (làm TP cho lệnh SELL)."""
    if direction == "up":
        candidates = [lvl for lvl in levels if lvl > price]
        return min(candidates) if candidates else None
    candidates = [lvl for lvl in levels if lvl < price]
    return max(candidates) if candidates else None


def check_sonic_r(df_1h: pd.DataFrame, df_4h: pd.DataFrame):
    """Trả về ('buy'|'sell'|None, info_dict) theo luật Sonic R + các bộ lọc hợp lưu bổ sung:
      1. Xu hướng H1: EMA34 > SMA89 > SMA200 (tăng) hoặc ngược lại (giảm).
      2. "Chạm Dragon": giá chạm EMA34 ở 1 trong 2 nến gần nhất.
      3. CCI(14) cắt qua đường 0 đúng chiều, VÀ nến xác nhận phải ĐÓNG CỬA vượt qua Dragon
         theo đúng chiều (chặt hơn chỉ "chạm").
      4. Xu hướng khung 4h phải cùng chiều với H1 (lọc tín hiệu ngược xu hướng lớn).
      5. Giá không được nằm sát 1 vùng hỗ trợ/kháng cự đối nghịch (trong phạm vi 0.5x ATR)
         - đây là bộ lọc trực tiếp xử lý việc "bán ở hỗ trợ mạnh" / "mua ở kháng cự mạnh"
         hoặc "giá vừa tạo đáy/đỉnh" mà bạn phản ánh.
      6. SL đặt dưới/trên đỉnh-đáy gần nhất (có đệm ATR), TP nhắm vào vùng hỗ trợ/kháng cự
         đối diện gần nhất - chỉ chấp nhận nếu R:R ước tính >= RR_TARGET_MIN (mặc định 1:2)."""
    close, high, low = df_1h["Close"], df_1h["High"], df_1h["Low"]
    ema34 = calc_ema(close, SONIC_EMA_DRAGON)
    sma89 = calc_sma(close, SONIC_SMA_TREND_FAST)
    sma200 = calc_sma(close, SONIC_SMA_TREND_SLOW)
    cci = calc_cci(df_1h)
    atr = calc_atr(df_1h, 14)

    valid = ema34.notna() & sma89.notna() & sma200.notna() & cci.notna() & atr.notna()
    if valid.sum() < 3:
        return None, None

    last, prev = -1, -2

    trend_up = ema34.iloc[last] > sma89.iloc[last] > sma200.iloc[last]
    trend_down = ema34.iloc[last] < sma89.iloc[last] < sma200.iloc[last]

    dragon_tag = False
    for i in (last, prev):
        if low.iloc[i] <= ema34.iloc[i] <= high.iloc[i]:
            dragon_tag = True
            break

    cci_cross_up = cci.iloc[prev] < 0 and cci.iloc[last] > 0
    cci_cross_down = cci.iloc[prev] > 0 and cci.iloc[last] < 0
    close_confirm_up = close.iloc[last] > ema34.iloc[last]
    close_confirm_down = close.iloc[last] < ema34.iloc[last]

    setup = None
    if trend_up and dragon_tag and cci_cross_up and close_confirm_up:
        setup = "buy"
    elif trend_down and dragon_tag and cci_cross_down and close_confirm_down:
        setup = "sell"

    if setup is None:
        return None, None

    # --- Lọc hợp lưu 1: xu hướng khung lớn hơn (4h) phải đồng thuận ---
    htf_trend = None
    if df_4h is not None:
        htf_trend = get_trend_regime(df_4h["Close"])
    if SONIC_HTF_CONFIRM and htf_trend is not None:
        if setup == "buy" and htf_trend != "up":
            return None, None
        if setup == "sell" and htf_trend != "down":
            return None, None

    entry = float(close.iloc[last])
    current_atr = float(atr.iloc[last])

    supports_near, resistances_near = find_swing_levels(df_1h, SR_SWING_ORDER_NEAR, SR_LOOKBACK_NEAR)
    supports_far, resistances_far = [], []
    if df_4h is not None and len(df_4h) >= SR_LOOKBACK_FAR:
        supports_far, resistances_far = find_swing_levels(df_4h, SR_SWING_ORDER_FAR, SR_LOOKBACK_FAR)

    if setup == "buy":
        # --- Lọc hợp lưu 2: không mua sát kháng cự (giá vừa tạo đỉnh gần đây) ---
        adverse = nearest_adverse_level(entry, resistances_near + resistances_far, current_atr, SR_PROXIMITY_ATR_MULT)
        if adverse is not None:
            return None, None

        near_lows = [lvl for lvl in supports_near if lvl < entry]
        structure_sl = max(near_lows) if near_lows else (entry - 2 * current_atr)
        sl = structure_sl - SL_BUFFER_ATR_MULT * current_atr
        risk = entry - sl
        if risk <= 0:
            return None, None

        target_level = nearest_target_level(entry, resistances_far + resistances_near, "up")
        min_tp = entry + RR_TARGET_MIN * risk
        tp = target_level if (target_level is not None and target_level >= min_tp) else min_tp
        rr = (tp - entry) / risk

    else:  # sell
        # --- Lọc hợp lưu 2: không bán sát hỗ trợ (giá vừa tạo đáy gần đây) ---
        adverse = nearest_adverse_level(entry, supports_near + supports_far, current_atr, SR_PROXIMITY_ATR_MULT)
        if adverse is not None:
            return None, None

        near_highs = [lvl for lvl in resistances_near if lvl > entry]
        structure_sl = min(near_highs) if near_highs else (entry + 2 * current_atr)
        sl = structure_sl + SL_BUFFER_ATR_MULT * current_atr
        risk = sl - entry
        if risk <= 0:
            return None, None

        target_level = nearest_target_level(entry, supports_far + supports_near, "down")
        min_tp = entry - RR_TARGET_MIN * risk
        tp = target_level if (target_level is not None and target_level <= min_tp) else min_tp
        rr = (entry - tp) / risk

    # --- Lọc hợp lưu 3: bắt buộc R:R ước tính đạt tối thiểu (mặc định 1:2) ---
    if rr < RR_TARGET_MIN - 1e-9:
        return None, None

    # Cảnh báo mềm (không chặn tín hiệu): CCI vừa đạt cực đoan ngay trước đó, khả năng
    # đây là 1 pha đảo chiều mạnh (giá "giật" 1 mạch) chứ không phải pullback lành mạnh.
    recent_cci = cci.tail(8)
    extreme_recent = (
        (setup == "buy" and (recent_cci < -SONIC_CCI_EXTREME).any())
        or (setup == "sell" and (recent_cci > SONIC_CCI_EXTREME).any())
    )

    info = {
        "entry": entry, "sl": float(sl), "tp": float(tp), "rr": float(rr),
        "ema34": float(ema34.iloc[last]), "sma89": float(sma89.iloc[last]),
        "sma200": float(sma200.iloc[last]), "cci": float(cci.iloc[last]),
        "htf_trend": htf_trend, "extreme_recent": bool(extreme_recent),
    }
    return setup, info


def format_sonic_alert(name: str, setup: str, info: dict, is_new: bool, correlation_note: str = None) -> str:
    dots = "🟢🟢🟢" if setup == "buy" else "🔴🔴🔴"
    label = "SONIC R - VÀO LỆNH BUY" if setup == "buy" else "SONIC R - VÀO LỆNH SELL"
    trang_thai = "🆕 MỚI XUẤT HIỆN" if is_new else "🔁 ĐANG TIẾP DIỄN"

    lines = [
        f"{dots} <b>{name}</b> — {label} (khung {SONIC_TIMEFRAME})",
        trang_thai,
        f"Entry: {info['entry']:.5f}",
        f"SL: {info['sl']:.5f}  |  TP: {info['tp']:.5f}  |  R:R ≈ 1:{info['rr']:.2f}",
        f"EMA34: {info['ema34']:.5f} | SMA89: {info['sma89']:.5f} | SMA200: {info['sma200']:.5f}",
    ]

    cci_line = f"CCI(14): {info['cci']:.1f}"
    if info.get("extreme_recent"):
        cci_line += "  ⚠️ CCI vừa cực đoan gần đây - có thể là đảo chiều mạnh chứ không phải pullback thường, cân nhắc kỹ"
    lines.append(cci_line)

    if info.get("htf_trend") in ("up", "down"):
        lines.append(f"Xu hướng khung {SONIC_HTF_TIMEFRAME}: {'TĂNG' if info['htf_trend'] == 'up' else 'GIẢM'} (đã xác nhận hợp lưu)")

    if correlation_note:
        lines.append(correlation_note)

    lines.append(f"🕒 {now_vn_str()} (giờ VN)")
    return "\n".join(lines)


def run_sonic_module(name, ticker, df_1h, setup, info, correlation_note, state):
    sonic_state = state.setdefault("sonic", {}).setdefault(ticker, {})
    bar_ts = df_1h.index[-1].isoformat()
    key = f"{setup}_ts"
    is_new = sonic_state.get(key) != bar_ts

    send_telegram(format_sonic_alert(name, setup, info, is_new, correlation_note))
    print(f"  -> [Sonic R] ĐÃ GỬI CẢNH BÁO ({setup.upper()}, {'MỚI' if is_new else 'TIẾP DIỄN'}, R:R 1:{info['rr']:.2f})")

    sonic_state[key] = bar_ts


# ============================================================================
# MODULE 6b: XÁC NHẬN CHÉO GIỮA CÁC MÃ TƯƠNG QUAN
# ============================================================================

def compute_correlation_matrix(daily_closes: dict):
    """daily_closes: {ticker: pd.Series giá đóng cửa ngày}. Trả về ma trận tương quan
    dựa trên % thay đổi giá đóng cửa ngày, hoặc None nếu không đủ dữ liệu."""
    usable = {t: s for t, s in daily_closes.items() if s is not None and len(s) > 5}
    if len(usable) < 2:
        return None
    df = pd.DataFrame(usable)
    df = df.tail(CORRELATION_LOOKBACK_DAYS + 5)
    returns = df.pct_change().dropna(how="all")
    if returns.shape[0] < 5:
        return None
    return returns.corr()


def get_correlation_note(ticker: str, setup: str, corr_matrix, trend_by_ticker: dict, name_by_ticker: dict):
    """Trả về 1 dòng chú thích xác nhận chéo (hoặc None nếu không tính được)."""
    if corr_matrix is None or ticker not in corr_matrix.columns:
        return None

    row = corr_matrix[ticker].drop(index=ticker, errors="ignore").dropna()
    strong = row[row.abs() >= CORRELATION_THRESHOLD].sort_values(key=lambda s: -s.abs())
    if strong.empty:
        return "ℹ️ Không có cặp nào tương quan đủ mạnh để xác nhận chéo lúc này."

    target_dir = "up" if setup == "buy" else "down"
    confirms, conflicts = [], []

    for other_ticker, corr_val in strong.items():
        other_trend = trend_by_ticker.get(other_ticker)
        if other_trend is None or other_trend == "none":
            continue
        expected_dir = other_trend if corr_val > 0 else ("down" if other_trend == "up" else "up")
        other_name = name_by_ticker.get(other_ticker, other_ticker)
        tag = f"{other_name} ({corr_val:+.2f})"
        if expected_dir == target_dir:
            confirms.append(tag)
        else:
            conflicts.append(tag)

    parts = []
    if confirms:
        parts.append("✅ Xác nhận chéo: " + ", ".join(confirms[:3]))
    if conflicts:
        parts.append("⚠️ Tương quan nhưng đang ngược chiều: " + ", ".join(conflicts[:3]))
    if not parts:
        return "ℹ️ Có mã tương quan mạnh nhưng chưa đủ dữ liệu xu hướng để xác nhận."
    return "\n".join(parts)


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


def format_calendar_digest(events_today: list, now_vn: datetime) -> str:
    date_str = now_vn.strftime("%Y-%m-%d")
    if not events_today:
        return f"📅 Hôm nay ({date_str}) không có tin QUAN TRỌNG (đỏ) nào.\n🕒 {now_vn_str()} (giờ VN)"

    lines = [f"📅 <b>TIN QUAN TRỌNG (ĐỎ) HÔM NAY</b> ({date_str}):"]
    for event, dt_vn in sorted(events_today, key=lambda x: x[1]):
        title = event.get("title", "(không rõ tên tin)")
        country = event.get("country", "")
        forecast = event.get("forecast") or "-"
        previous = event.get("previous") or "-"
        lines.append(
            f"  🔴 {dt_vn.strftime('%H:%M')} — {country} {title} "
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

    # Lọc ra các tin đỏ diễn ra trong hôm nay (giờ VN)
    events_today = []
    for event in cal.get("events", []):
        dt = parse_event_datetime(event)
        if dt is None:
            continue
        dt_vn = dt.astimezone(VN_TZ)
        if dt_vn.date() == now_vn.date():
            events_today.append((event, dt_vn))

    # --- Tổng hợp 1 lần vào 7h sáng giờ VN: liệt kê toàn bộ tin đỏ hôm nay ---
    today_str = now_vn.date().isoformat()
    if now_vn.hour == CALENDAR_DIGEST_HOUR_VN and cal.get("digest_sent_date") != today_str:
        send_telegram(format_calendar_digest(events_today, now_vn))
        cal["digest_sent_date"] = today_str
        print(f"  -> [Lịch tin tức] Đã gửi tổng hợp 7h sáng ({len(events_today)} tin đỏ hôm nay)")

    # --- Báo thêm trước mỗi tin khoảng 15 phút ---
    notified = cal.setdefault("notified", {})
    fired = 0

    for event, dt_vn in events_today:
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

    daily_closes = {}      # ticker -> Series giá đóng cửa 1D (dùng tính tương quan)
    trend_by_ticker = {}   # ticker -> 'up'/'down'/'none' theo khung H1 (dùng xác nhận chéo)
    name_by_ticker = {}
    sonic_candidates = []  # (name, ticker, df_1h, setup, info) - chờ gửi sau khi có tương quan

    # --- Pha 1: quét từng mã (RSI setup + thu thập dữ liệu cho Sonic R & tương quan) ---
    for name, ticker in SYMBOLS.items():
        name_by_ticker[ticker] = name
        print(f"\n=== Đang quét {name} ({ticker}) ===")
        rsi_values = {}
        error_tf = []
        df_4h = None
        df_1h_for_trend = None

        for tf in TIMEFRAMES:
            df, rsi_series, err = fetch_ohlc_and_rsi_with_retry(ticker, tf)
            if err:
                error_tf.append(tf)
            else:
                rsi_values[tf] = float(rsi_series.dropna().iloc[-1])
                print(f"  {tf}: RSI = {rsi_values[tf]:.2f}")
                if tf == "4h":
                    df_4h = df
                elif tf == "1h":
                    df_1h_for_trend = df
                elif tf == "1D":
                    daily_closes[ticker] = df["Close"]

        fail_state = state.setdefault("rsi_fail", {}).setdefault(ticker, {"fail_count": 0, "fail_notified": False})

        if error_tf:
            fail_state["fail_count"] += 1
            print(f"  Thiếu dữ liệu khung: {error_tf} (fail_count={fail_state['fail_count']})")
            if fail_state["fail_count"] >= FAIL_ALERT_THRESHOLD and not fail_state["fail_notified"]:
                send_telegram(
                    f"⚠️ <b>{name}</b>: không lấy được dữ liệu Yahoo Finance cho khung "
                    f"{', '.join(error_tf)} trong hơn 1 giờ liên tục. Vui lòng kiểm tra lại mã "
                    f"hoặc nguồn dữ liệu."
                )
                fail_state["fail_notified"] = True
                changed_flags["changed"] = True
            continue
        else:
            if fail_state["fail_count"] > 0 or fail_state["fail_notified"]:
                fail_state["fail_count"] = 0
                fail_state["fail_notified"] = False
                changed_flags["changed"] = True

        run_rsi_module(name, ticker, rsi_values, state, changed_flags)

        if df_1h_for_trend is not None:
            trend_by_ticker[ticker] = get_trend_regime(df_1h_for_trend["Close"])

        sonic_df, sonic_err = fetch_ohlc_with_retry(
            ticker, SONIC_TIMEFRAME, period_override=SONIC_HISTORY_PERIOD,
            min_bars=SONIC_SMA_TREND_SLOW + 10,
        )
        if sonic_err:
            print(f"  -> [Sonic R] Lỗi lấy dữ liệu: {sonic_err}")
        else:
            setup, info = check_sonic_r(sonic_df, df_4h)
            if setup is not None:
                sonic_candidates.append((name, ticker, sonic_df, setup, info))
                print(f"  -> [Sonic R] Ứng viên tín hiệu {setup.upper()} (R:R ước tính 1:{info['rr']:.2f}) - chờ xác nhận chéo")
            else:
                print("  -> [Sonic R] Chưa đủ điều kiện (hoặc bị lọc bởi hợp lưu 4h / hỗ trợ-kháng cự / R:R)")

        changed_flags["changed"] = True  # last_check luôn đổi -> luôn lưu lại cho gọn

    # --- Pha 2: tính tương quan giữa các mã rồi mới gửi cảnh báo Sonic R ---
    corr_matrix = compute_correlation_matrix(daily_closes)
    for name, ticker, sonic_df, setup, info in sonic_candidates:
        correlation_note = get_correlation_note(ticker, setup, corr_matrix, trend_by_ticker, name_by_ticker)
        run_sonic_module(name, ticker, sonic_df, setup, info, correlation_note, state)

    # --- Module 3, 4, 5: chạy 1 lần cho toàn hệ thống (không theo từng mã) ---
    run_calendar_module(state)
    run_session_module(state)
    run_ping_module(state)
    changed_flags["changed"] = True

    if changed_flags["changed"]:
        save_state(state)
        print("\nĐã lưu state.json")
    else:
        print("\nKhông có gì thay đổi -> không cần ghi lại state.json")


if __name__ == "__main__":
    main()
