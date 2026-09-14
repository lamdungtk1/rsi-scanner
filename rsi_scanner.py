# -*- coding: utf-8 -*-
"""
RSI Scanner - Quét RSI(14) trên 5 khung thời gian (5m/15m/1h/4h/1D)
cho 6 mã: EUR/USD, GBP/USD, USD/JPY, Vàng, US30, US100.

Điều kiện báo (2 setup):

  CANH SELL:
    - 5m  > 50
    - 15m trong khoảng 40–60
    - 1h, 4h, 1D đều < 50

  CANH BUY:
    - 5m  < 50
    - 15m trong khoảng 40–60
    - 1h, 4h, 1D đều > 50

Cứ mỗi lần quét (10 phút/lần) mà mã đó vẫn đang thoả 1 trong 2 điều kiện
trên thì vẫn gửi tin tiếp (không chỉ báo 1 lần duy nhất khi mới xuất hiện).
Mỗi tin nhắn có kèm nhãn "🆕 MỚI XUẤT HIỆN" (lần quét trước chưa thoả,
lần này mới thoả) hoặc "🔁 ĐANG TIẾP DIỄN" (đã báo từ lần quét trước, lần
này vẫn còn thoả). Giờ hiển thị trong tin nhắn là giờ Việt Nam (UTC+7).

Có lưu trạng thái vào state.json giữa các lần chạy (dùng để theo dõi lỗi
kéo dài), có thử lại khi Yahoo Finance lỗi tạm thời.
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import yfinance as yf
import requests

VN_TZ = timezone(timedelta(hours=7))

STATE_FILE = "state.json"

# Mã trên Yahoo Finance. Nếu 1 mã không lấy được dữ liệu, bạn có thể
# đổi ticker ở đây (ví dụ Vàng có thể thử "XAUUSD=X" thay cho "GC=F").
SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "Vàng (XAU/USD)": "GC=F",
    "US30 (Dow Jones)": "YM=F",
    "US100 (Nasdaq)": "NQ=F",
}

TIMEFRAMES = ["5m", "15m", "1h", "4h", "1D"]

RSI_PERIOD = 14
MAX_RETRIES = 3          # 1 lần đầu + 2 lần thử lại
RETRY_DELAY_SEC = 5
FAIL_ALERT_THRESHOLD = 6  # ~1 giờ liên tục lỗi (mỗi lần quét cách nhau 10 phút) mới báo lỗi

# Ngưỡng cho 2 setup - sửa ở đây nếu muốn đổi ngưỡng
SELL_5M_MIN = 50
SELL_15M_RANGE = (40, 60)
BUY_5M_MAX = 50
BUY_15M_RANGE = (40, 60)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


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
        json.dump(state, f, ensure_ascii=False, indent=2)


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


def fetch_close_series(ticker: str, timeframe: str) -> pd.Series:
    """Lấy giá đóng cửa cho 1 khung thời gian. Khung 4h được tự tổng hợp từ dữ liệu 1h."""
    if timeframe == "5m":
        interval, period = "5m", "5d"
    elif timeframe == "15m":
        interval, period = "15m", "5d"
    elif timeframe == "1h":
        interval, period = "60m", "1mo"
    elif timeframe == "4h":
        interval, period = "60m", "3mo"  # sẽ resample về 4h bên dưới
    elif timeframe == "1D":
        interval, period = "1d", "6mo"
    else:
        raise ValueError(f"Khung thời gian không hỗ trợ: {timeframe}")

    df = yf.download(ticker, interval=interval, period=period, progress=False, auto_adjust=False)

    if df is None or df.empty:
        raise RuntimeError(f"Không có dữ liệu cho {ticker} khung {timeframe}")

    # yfinance đôi khi trả về cột dạng MultiIndex, chuẩn hoá lại cho chắc
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    if timeframe == "4h":
        df = df.resample("4h").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum",
        }).dropna()

    return df["Close"].dropna()


def get_rsi_with_retry(ticker: str, timeframe: str):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            close = fetch_close_series(ticker, timeframe)
            rsi_series = calc_rsi(close)
            rsi_value = float(rsi_series.dropna().iloc[-1])
            return rsi_value, None
        except Exception as e:
            last_err = str(e)
            print(f"  -> Lỗi lần {attempt}/{MAX_RETRIES} ({ticker}, {timeframe}): {last_err}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC)
    return None, last_err


def check_setup(rsi: dict):
    """Trả về 'sell', 'buy' hoặc None dựa trên 5 giá trị RSI đã lấy được."""
    r5, r15, r1h, r4h, r1d = (rsi["5m"], rsi["15m"], rsi["1h"], rsi["4h"], rsi["1D"])

    is_sell = (
        r5 > SELL_5M_MIN
        and SELL_15M_RANGE[0] <= r15 <= SELL_15M_RANGE[1]
        and r1h < 50 and r4h < 50 and r1d < 50
    )
    is_buy = (
        r5 < BUY_5M_MAX
        and BUY_15M_RANGE[0] <= r15 <= BUY_15M_RANGE[1]
        and r1h > 50 and r4h > 50 and r1d > 50
    )

    if is_sell:
        return "sell"
    if is_buy:
        return "buy"
    return None


def format_alert(name: str, setup: str, rsi_values: dict, is_new: bool) -> str:
    if setup == "sell":
        emoji, label = "🔴", "CANH SELL"
    else:
        emoji, label = "🟢", "CANH BUY"

    trang_thai = "🆕 MỚI XUẤT HIỆN" if is_new else "🔁 ĐANG TIẾP DIỄN"

    lines = [f"{emoji} <b>{name}</b> — {label}", trang_thai]
    for tf in TIMEFRAMES:
        v = rsi_values.get(tf)
        lines.append(f"  • {tf}: {v:.1f}" if v is not None else f"  • {tf}: (n/a)")
    now = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M")
    lines.append(f"🕒 {now} (giờ VN)")
    return "\n".join(lines)


def main():
    state = load_state()
    changed = False

    for name, ticker in SYMBOLS.items():
        key = ticker
        print(f"\n=== Đang quét {name} ({ticker}) ===")
        rsi_values = {}
        error_tf = []

        for tf in TIMEFRAMES:
            rsi_value, err = get_rsi_with_retry(ticker, tf)
            if rsi_value is None:
                error_tf.append(tf)
            else:
                rsi_values[tf] = rsi_value
                print(f"  {tf}: RSI = {rsi_value:.2f}")

        sym_state = state.get(key, {"status": None, "fail_count": 0, "fail_notified": False})

        if error_tf:
            sym_state["fail_count"] = sym_state.get("fail_count", 0) + 1
            print(f"  Thiếu dữ liệu khung: {error_tf} (fail_count={sym_state['fail_count']})")
            if sym_state["fail_count"] >= FAIL_ALERT_THRESHOLD and not sym_state.get("fail_notified"):
                send_telegram(
                    f"⚠️ <b>{name}</b>: không lấy được dữ liệu Yahoo Finance cho khung "
                    f"{', '.join(error_tf)} trong hơn 1 giờ liên tục. Vui lòng kiểm tra lại mã "
                    f"hoặc nguồn dữ liệu."
                )
                sym_state["fail_notified"] = True
                changed = True
            state[key] = sym_state
            continue
        else:
            if sym_state.get("fail_count", 0) > 0 or sym_state.get("fail_notified"):
                sym_state["fail_count"] = 0
                sym_state["fail_notified"] = False
                changed = True

        setup = check_setup(rsi_values)
        old_status = sym_state.get("status")

        if setup is not None:
            is_new = (setup != old_status)
            send_telegram(format_alert(name, setup, rsi_values, is_new))
            trang_thai_log = "MỚI XUẤT HIỆN" if is_new else "ĐANG TIẾP DIỄN"
            print(f"  -> ĐÃ GỬI CẢNH BÁO ({'CANH SELL' if setup == 'sell' else 'CANH BUY'}, {trang_thai_log})")
        else:
            print("  -> Chưa thoả điều kiện Canh Sell / Canh Buy, không báo")

        if setup != old_status:
            changed = True

        sym_state["status"] = setup
        sym_state["last_rsi"] = rsi_values
        sym_state["last_check"] = datetime.now(VN_TZ).isoformat()
        state[key] = sym_state

    if changed:
        save_state(state)
        print("\nĐã lưu state.json")
    else:
        print("\nKhông có gì thay đổi -> không cần ghi lại state.json")


if __name__ == "__main__":
    main()
