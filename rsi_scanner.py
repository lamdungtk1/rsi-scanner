# -*- coding: utf-8 -*-
"""
RSI Scanner + 4 module cảnh báo bổ sung - chạy mỗi 10 phút qua GitHub Actions.

Các module trong file này:
  1. RSI SETUP (CANH SELL / CANH BUY) - có nhãn MẠNH.
  2. LỊCH TIN TỨC Forex Factory - tổng hợp tin đỏ trong 24h tới lúc 8h30 sáng, và
     báo thêm 1 lần trước mỗi tin khoảng 15 phút. Dữ liệu tải 1 lần/tuần.
  3. CẢNH BÁO MỞ/ĐÓNG PHIÊN giao dịch (Sydney/Tokyo/London/New York).
  4. PING ĐỊNH KỲ mỗi ngày để xác nhận hệ thống còn sống.
  5. HỆ THỐNG EMA34/89 riêng: khung M15, pullback chạm EMA89 rồi bật lại cắt cả
     EMA34 và EMA89, xác nhận thêm bằng RSI H4 + D1, có bộ lọc hỗ trợ/kháng cự
     và R:R tối thiểu, cùng xác nhận chéo giữa các mã tương quan.

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

# --- Hệ thống EMA34/89 riêng của bạn (module 6) ---
# Luật: khung M15, xu hướng xác định bởi EMA34 vs EMA89. Với setup BUY: EMA34>EMA89
# (uptrend); giá lao xuống chạm/cắt EMA89 (pullback sâu); rồi lao lên cắt lại CẢ HAI
# đường EMA34 và EMA89 (xác nhận pullback kết thúc, xu hướng tiếp diễn); đồng thời
# RSI khung H4 và D1 đều > 50 (xác nhận đà tăng lớn hơn). Setup SELL làm ngược lại.
EMA_CROSS_TIMEFRAME = "15m"
EMA_CROSS_FAST = 34
EMA_CROSS_SLOW = 89
EMA_CROSS_TOUCH_LOOKBACK = 20   # số nến M15 tìm ngược để tìm điểm chạm EMA89 trước khi bật lên/xuống lại
EMA_CROSS_RSI_HTF_1 = "4h"      # khung lớn thứ nhất dùng lọc RSI (theo đúng yêu cầu: H4)
EMA_CROSS_RSI_HTF_2 = "1D"      # khung lớn thứ hai dùng lọc RSI (theo đúng yêu cầu: D1)
EMA_CROSS_RSI_THRESHOLD = 50

# --- Bộ lọc hợp lưu bổ sung (khắc phục entry ngược hỗ trợ/kháng cự, đảm bảo R:R) ---
SR_SWING_ORDER_NEAR = 4        # số nến mỗi bên để xác nhận đỉnh/đáy trên khung M15 (dùng đặt SL)
SR_LOOKBACK_NEAR = 150         # số nến M15 gần nhất được xét để tìm đỉnh/đáy gần (SL)
SR_SWING_ORDER_FAR = 5         # số nến mỗi bên để xác nhận đỉnh/đáy trên khung 4h (dùng lọc entry + đặt TP)
SR_LOOKBACK_FAR = 180          # số nến 4h gần nhất được xét (~30 ngày) để tìm vùng hỗ trợ/kháng cự lớn
SR_PROXIMITY_ATR_MULT = 0.5    # nếu giá cách 1 vùng hỗ trợ/kháng cự đối nghịch < 0.5 x ATR -> HUỶ tín hiệu

SL_BUFFER_ATR_MULT = 0.2       # đệm thêm ngoài điểm chạm EMA89 khi đặt Stop Loss
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
# MODULE 6: HỆ THỐNG EMA34/89 RIÊNG (pullback chạm EMA89 rồi bật lại cả 2 đường)
# ============================================================================

def calc_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def get_trend_regime(close: pd.Series):
    """Trả về 'up' / 'down' / 'none' dựa trên EMA34 vs EMA89 - dùng cho xác nhận chéo tương quan."""
    if close.dropna().shape[0] < EMA_CROSS_SLOW + 1:
        return None
    ema_fast = calc_ema(close, EMA_CROSS_FAST)
    ema_slow = calc_ema(close, EMA_CROSS_SLOW)
    if pd.isna(ema_fast.iloc[-1]) or pd.isna(ema_slow.iloc[-1]):
        return None
    if ema_fast.iloc[-1] > ema_slow.iloc[-1]:
        return "up"
    if ema_fast.iloc[-1] < ema_slow.iloc[-1]:
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


def check_ema_cross_system(df_15m: pd.DataFrame, df_4h: pd.DataFrame, rsi_h4, rsi_d1):
    """Trả về ('buy'|'sell'|None, info_dict) theo đúng luật bạn mô tả + 2 bộ lọc hợp lưu:
      1. Xu hướng M15: EMA34 > EMA89 (tăng) hoặc EMA34 < EMA89 (giảm).
      2. Giá lao xuống chạm/cắt EMA89 (pullback sâu) trong vài nến gần đây - với setup
         BUY; ngược lại (lao lên chạm/cắt EMA89) với setup SELL.
      3. Rồi giá bật lại, ĐÓNG CỬA vượt qua CẢ HAI đường EMA34 và EMA89 ở nến gần nhất
         (và đây phải là lần đóng cửa VƯỢT ĐẦU TIÊN - tránh báo lại nhiều nến liền khi
         giá đã ở trên/dưới cả 2 đường từ trước).
      4. RSI khung H4 VÀ D1 đều cùng chiều (>50 cho BUY, <50 cho SELL) - đúng yêu cầu gốc.
      5. (Bổ sung) Giá không được nằm sát 1 vùng hỗ trợ/kháng cự đối nghịch (0.5x ATR).
      6. (Bổ sung) SL đặt ngay dưới/trên điểm chạm EMA89 (có đệm ATR), TP nhắm vào vùng
         hỗ trợ/kháng cự đối diện gần nhất - chỉ báo nếu R:R ước tính >= RR_TARGET_MIN."""
    close, high, low = df_15m["Close"], df_15m["High"], df_15m["Low"]
    n = len(df_15m)
    min_needed = EMA_CROSS_SLOW + EMA_CROSS_TOUCH_LOOKBACK + 5
    if n < min_needed or rsi_h4 is None or rsi_d1 is None:
        return None, None

    ema_fast = calc_ema(close, EMA_CROSS_FAST)
    ema_slow = calc_ema(close, EMA_CROSS_SLOW)
    atr = calc_atr(df_15m, 14)

    last, prev = n - 1, n - 2

    def above_both(i):
        return close.iloc[i] > ema_fast.iloc[i] and close.iloc[i] > ema_slow.iloc[i]

    def below_both(i):
        return close.iloc[i] < ema_fast.iloc[i] and close.iloc[i] < ema_slow.iloc[i]

    trend_up = ema_fast.iloc[last] > ema_slow.iloc[last]
    trend_down = ema_fast.iloc[last] < ema_slow.iloc[last]

    setup = None
    touch_idx = None

    if trend_up and above_both(last) and not above_both(prev):
        for i in range(prev, max(prev - EMA_CROSS_TOUCH_LOOKBACK, 0), -1):
            if ema_fast.iloc[i] > ema_slow.iloc[i] and low.iloc[i] <= ema_slow.iloc[i]:
                touch_idx = i
                break
        if touch_idx is not None and rsi_h4 > EMA_CROSS_RSI_THRESHOLD and rsi_d1 > EMA_CROSS_RSI_THRESHOLD:
            setup = "buy"

    elif trend_down and below_both(last) and not below_both(prev):
        for i in range(prev, max(prev - EMA_CROSS_TOUCH_LOOKBACK, 0), -1):
            if ema_fast.iloc[i] < ema_slow.iloc[i] and high.iloc[i] >= ema_slow.iloc[i]:
                touch_idx = i
                break
        if touch_idx is not None and rsi_h4 < EMA_CROSS_RSI_THRESHOLD and rsi_d1 < EMA_CROSS_RSI_THRESHOLD:
            setup = "sell"

    if setup is None or pd.isna(atr.iloc[last]):
        return None, None

    entry = float(close.iloc[last])
    current_atr = float(atr.iloc[last])

    supports_near, resistances_near = find_swing_levels(df_15m, SR_SWING_ORDER_NEAR, SR_LOOKBACK_NEAR)
    supports_far, resistances_far = [], []
    if df_4h is not None and len(df_4h) >= SR_LOOKBACK_FAR:
        supports_far, resistances_far = find_swing_levels(df_4h, SR_SWING_ORDER_FAR, SR_LOOKBACK_FAR)

    if setup == "buy":
        adverse = nearest_adverse_level(entry, resistances_near + resistances_far, current_atr, SR_PROXIMITY_ATR_MULT)
        if adverse is not None:
            return None, None

        touch_price = float(low.iloc[touch_idx])
        sl = touch_price - SL_BUFFER_ATR_MULT * current_atr
        risk = entry - sl
        if risk <= 0:
            return None, None

        target_level = nearest_target_level(entry, resistances_far + resistances_near, "up")
        min_tp = entry + RR_TARGET_MIN * risk
        tp = target_level if (target_level is not None and target_level >= min_tp) else min_tp
        rr = (tp - entry) / risk

    else:  # sell
        adverse = nearest_adverse_level(entry, supports_near + supports_far, current_atr, SR_PROXIMITY_ATR_MULT)
        if adverse is not None:
            return None, None

        touch_price = float(high.iloc[touch_idx])
        sl = touch_price + SL_BUFFER_ATR_MULT * current_atr
        risk = sl - entry
        if risk <= 0:
            return None, None

        target_level = nearest_target_level(entry, supports_far + supports_near, "down")
        min_tp = entry - RR_TARGET_MIN * risk
        tp = target_level if (target_level is not None and target_level <= min_tp) else min_tp
        rr = (entry - tp) / risk

    if rr < RR_TARGET_MIN - 1e-9:
        return None, None

    info = {
        "entry": entry, "sl": float(sl), "tp": float(tp), "rr": float(rr),
        "ema_fast": float(ema_fast.iloc[last]), "ema_slow": float(ema_slow.iloc[last]),
        "touch_price": touch_price, "touch_bars_ago": last - touch_idx,
        "rsi_h4": float(rsi_h4), "rsi_d1": float(rsi_d1),
    }
    return setup, info


def format_ema_cross_alert(name: str, setup: str, info: dict, is_new: bool, correlation_note: str = None) -> str:
    dots = "🟢🟢🟢" if setup == "buy" else "🔴🔴🔴"
    label = "EMA34/89 - VÀO LỆNH BUY" if setup == "buy" else "EMA34/89 - VÀO LỆNH SELL"
    trang_thai = "🆕 MỚI XUẤT HIỆN" if is_new else "🔁 ĐANG TIẾP DIỄN"

    lines = [
        f"{dots} <b>{name}</b> — {label} (khung {EMA_CROSS_TIMEFRAME})",
        trang_thai,
        f"Entry: {info['entry']:.5f}",
        f"SL: {info['sl']:.5f}  |  TP: {info['tp']:.5f}  |  R:R ≈ 1:{info['rr']:.2f}",
        f"EMA{EMA_CROSS_FAST}: {info['ema_fast']:.5f} | EMA{EMA_CROSS_SLOW}: {info['ema_slow']:.5f}",
        f"Điểm chạm EMA{EMA_CROSS_SLOW} gần nhất: {info['touch_price']:.5f} ({info['touch_bars_ago']} nến M15 trước)",
        f"RSI H4: {info['rsi_h4']:.1f} | RSI D1: {info['rsi_d1']:.1f}",
    ]

    if correlation_note:
        lines.append(correlation_note)

    lines.append(f"🕒 {now_vn_str()} (giờ VN)")
    return "\n".join(lines)


def run_ema_cross_module(name, ticker, df_15m, setup, info, correlation_note, state):
    ema_state = state.setdefault("ema_cross", {}).setdefault(ticker, {})
    bar_ts = df_15m.index[-1].isoformat()
    key = f"{setup}_ts"
    is_new = ema_state.get(key) != bar_ts

    send_telegram(format_ema_cross_alert(name, setup, info, is_new, correlation_note))
    print(f"  -> [EMA34/89] ĐÃ GỬI CẢNH BÁO ({setup.upper()}, {'MỚI' if is_new else 'TIẾP DIỄN'}, R:R 1:{info['rr']:.2f})")

    ema_state[key] = bar_ts


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

    daily_closes = {}      # ticker -> Series giá đóng cửa 1D (dùng tính tương quan)
    trend_by_ticker = {}   # ticker -> 'up'/'down'/'none' theo EMA34/89 khung H1 (dùng xác nhận chéo)
    name_by_ticker = {}
    ema_cross_candidates = []  # (name, ticker, df_15m, setup, info) - chờ gửi sau khi có tương quan

    # --- Pha 1: quét từng mã (RSI setup + thu thập dữ liệu cho hệ thống EMA34/89 & tương quan) ---
    for name, ticker in SYMBOLS.items():
        name_by_ticker[ticker] = name
        print(f"\n=== Đang quét {name} ({ticker}) ===")
        rsi_values = {}
        error_tf = []
        df_15m = None
        df_4h = None
        df_1h_for_trend = None

        for tf in TIMEFRAMES:
            df, rsi_series, err = fetch_ohlc_and_rsi_with_retry(ticker, tf)
            if err:
                error_tf.append(tf)
            else:
                rsi_values[tf] = float(rsi_series.dropna().iloc[-1])
                print(f"  {tf}: RSI = {rsi_values[tf]:.2f}")
                if tf == "15m":
                    df_15m = df
                elif tf == "4h":
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

        setup, info = check_ema_cross_system(df_15m, df_4h, rsi_values.get("4h"), rsi_values.get("1D"))
        if setup is not None:
            ema_cross_candidates.append((name, ticker, df_15m, setup, info))
            print(f"  -> [EMA34/89] Ứng viên tín hiệu {setup.upper()} (R:R ước tính 1:{info['rr']:.2f}) - chờ xác nhận chéo")
        else:
            print("  -> [EMA34/89] Chưa đủ điều kiện (hoặc bị lọc bởi hỗ trợ-kháng cự / R:R)")

        changed_flags["changed"] = True  # last_check luôn đổi -> luôn lưu lại cho gọn

    # --- Pha 2: tính tương quan giữa các mã rồi mới gửi cảnh báo EMA34/89 ---
    corr_matrix = compute_correlation_matrix(daily_closes)
    for name, ticker, df_15m, setup, info in ema_cross_candidates:
        correlation_note = get_correlation_note(ticker, setup, corr_matrix, trend_by_ticker, name_by_ticker)
        run_ema_cross_module(name, ticker, df_15m, setup, info, correlation_note, state)

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
