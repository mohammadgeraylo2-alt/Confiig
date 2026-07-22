"""
Signal Bot v18 - EMA Trend + RSI Pullback + ATR Strategy
استراتژی جدید (جایگزین کامل استراتژی خط روند/پین‌بار v17):

  1) روند (4H): EMA50 در برابر EMA200 → فیلتر جهت معامله (فقط هم‌جهت روند)
  2) تاییدیه ورود (1H): برگشت قیمت (pullback) به EMA20 + RSI(14) در ناحیه
     "ریست مومنتوم" (نه اشباع کامل) + کندل تاییدیه با بدنه قوی هم‌جهت روند
  3) استاپ‌لاس: بر پایه ATR(14) از نقطه ورود (نه عدد ثابت، نه ذهنی)
  4) تارگت: مضرب ثابت از ریسک (R:R) روی TP1/TP2 (بدون شکار سطوح دستی)

تغییرات نسبت به v17:
  ❌ حذف: رسم خط روند دستی (swing high/low + trendline fit)، پین‌بار،
           Order Block، سطوح کلیدی دستی برای TP
  ✅ اضافه: EMA50/EMA200 (فیلتر روند)، EMA20 (pullback)، RSI(14)، ATR(14)
  ✅ منطق کاملاً قانون‌محور، قابل بک‌تست و بدون overfitting به سطوح دستی

بقیه‌ی زیرساخت (دریافت داده از KuCoin، ارسال تلگرام، رسم چارت،
موتور بک‌تست، مدیریت ریسک) بدون تغییر باقی مانده است.
"""

import os
import asyncio
import logging
import time
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from telegram import Bot, InputMediaPhoto, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
import io

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID", "YOUR_CHAT_ID")

RISK_USD  = 5.0
LEVERAGE  = 5

CANDLE_TF_SECONDS = 3600  # کندل تاییدیه = 1 ساعت

# ✅ v18: پارامترهای استراتژی EMA + RSI + ATR
EMA_PULLBACK   = 20     # EMA سریع روی 1H برای pullback
EMA_FAST       = 50     # EMA روند روی 4H
EMA_SLOW       = 200    # EMA روند روی 4H
RSI_PERIOD     = 14
RSI_RESET_LOW  = 40     # محدوده ریست مومنتوم (نه اشباع کامل)
RSI_RESET_HIGH = 60
ATR_PERIOD     = 14
SL_ATR_MULT    = 1.5    # استاپ = ATR × این ضریب
TP1_RR         = 1.5    # تارگت ۱ = ریسک × این ضریب
TP2_RR         = 2.5    # تارگت ۲ = ریسک × این ضریب
MIN_RR         = 1.5    # حداقل R:R قابل قبول برای ارسال سیگنال

SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
    "ADA-USDT", "AVAX-USDT", "DOT-USDT", "MATIC-USDT", "LINK-USDT",
    "FTM-USDT", "NEAR-USDT", "APT-USDT", "ARB-USDT", "OP-USDT",
    "INJ-USDT", "SUI-USDT", "SEI-USDT", "TIA-USDT", "JUP-USDT",
    "WIF-USDT", "BONK-USDT", "DOGE-USDT", "LTC-USDT", "ATOM-USDT",
    "FIL-USDT", "AAVE-USDT", "UNI-USDT", "TON-USDT", "HBAR-USDT",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  KUCOIN DATA FETCH
# ─────────────────────────────────────────────
KUCOIN_BASE = "https://api.kucoin.com"

def get_klines(symbol: str, timeframe: str, days_back: int = 60) -> pd.DataFrame:
    """
    دریافت کندل از KuCoin با pagination
    """
    tf_map      = {"4h": "4hour", "1h": "1hour", "15m": "15min", "5m": "5min"}
    kc_tf       = tf_map.get(timeframe, "1hour")
    url         = f"{KUCOIN_BASE}/api/v1/market/candles"
    end_ts      = int(time.time())
    start_ts    = end_ts - days_back * 86400
    all_rows    = []
    current_end = end_ts

    while current_end > start_ts:
        params = {
            "symbol":  symbol,
            "type":    kc_tf,
            "startAt": start_ts,
            "endAt":   current_end,
        }
        try:
            r    = requests.get(url, params=params, timeout=10)
            data = r.json()
            if data.get("code") != "200000" or not data.get("data"):
                break
            rows = data["data"]
            if not rows:
                break
            all_rows.extend(rows)
            oldest_ts = int(rows[-1][0])
            if oldest_ts <= start_ts or len(rows) < 100:
                break
            current_end = oldest_ts - 1
            import time as _time; _time.sleep(0.2)
        except Exception as e:
            log.error(f"Kline fetch error {symbol} {timeframe}: {e}")
            break

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=["time","open","close","high","low","volume","turnover"])
    df = df.drop_duplicates(subset=["time"])
    for col in ["open","close","high","low","volume"]:
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["time"].astype(int), unit="s", utc=True)
    df = df.sort_values("time").reset_index(drop=True)
    return df

# ─────────────────────────────────────────────
#  ✅ INDICATORS (EMA / RSI / ATR) - v18
# ─────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    محاسبه اندیکاتورهای مورد نیاز استراتژی روی یک دیتافریم کندل.
    همه‌ی محاسبات "causal" هستند یعنی مقدار هر ردیف فقط به گذشته وابسته
    است - بنابراین برش‌زدن (slice) دیتافریم برای بک‌تست هم‌ارز با
    محاسبه‌ی مجدد روی همون بازه‌ست.
    """
    df = df.copy()

    df["ema20"]  = df["close"].ewm(span=EMA_PULLBACK, adjust=False).mean()
    df["ema50"]  = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    delta    = df["close"].diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50.0)

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / ATR_PERIOD, adjust=False).mean()

    return df

# ─────────────────────────────────────────────
#  ✅ TREND FILTER روی 4H - v18
# ─────────────────────────────────────────────
def get_trend_4h(df_4h: pd.DataFrame) -> str | None:
    """
    جهت روند بر اساس آخرین کندل بسته‌شده‌ی 4H:
      صعودی: EMA50 > EMA200  و قیمت بالای EMA50
      نزولی: EMA50 < EMA200  و قیمت زیر EMA50
    """
    if len(df_4h) < EMA_SLOW + 5:
        return None
    last = df_4h.iloc[-1]
    if pd.isna(last["ema200"]):
        return None
    if last["ema50"] > last["ema200"] and last["close"] > last["ema50"]:
        return "bullish"
    if last["ema50"] < last["ema200"] and last["close"] < last["ema50"]:
        return "bearish"
    return None

# ─────────────────────────────────────────────
#  ✅ ENTRY: PULLBACK + RSI + CONFIRM CANDLE (1H) - v18
# ─────────────────────────────────────────────
def check_entry_signal(df_1h: pd.DataFrame, trend: str) -> dict | None:
    """
    شرط ورود روی 1H (فقط با آخرین دو کندلِ بسته‌شده - بدون repaint):
      1) کندل pullback (ماقبل‌آخر): فاصله‌ی low/high تا EMA20 <= 0.5×ATR
      2) RSI کندل pullback در محدوده‌ی ریست مومنتوم (40 تا 60)
      3) کندل تاییدیه (آخرین کندل بسته‌شده): هم‌جهت روند، بدنه >= 50٪ رنج
         و بسته‌شدن در سمت درست EMA20
    """
    if len(df_1h) < EMA_SLOW + 5:
        return None

    pullback = df_1h.iloc[-2]
    confirm  = df_1h.iloc[-1]

    atr = pullback["atr"]
    if atr is None or pd.isna(atr) or atr <= 0:
        return None

    ema20     = pullback["ema20"]
    tolerance = atr * 0.5

    rng = confirm["high"] - confirm["low"]
    if rng <= 0:
        return None
    body        = abs(confirm["close"] - confirm["open"])
    strong_body = body >= rng * 0.5

    pullback_idx = len(df_1h) - 2
    confirm_idx  = len(df_1h) - 1

    if trend == "bullish":
        touched    = abs(pullback["low"] - ema20) <= tolerance
        rsi_ok     = RSI_RESET_LOW <= pullback["rsi"] <= RSI_RESET_HIGH
        confirm_ok = (confirm["close"] > confirm["open"]) and (confirm["close"] > ema20) and strong_body
        if touched and rsi_ok and confirm_ok:
            entry = confirm["close"]
            sl    = entry - atr * SL_ATR_MULT
            if sl >= entry:
                return None
            return {
                "signal": "BUY", "direction": "bullish",
                "entry": entry, "sl": sl, "atr": atr,
                "pullback_idx": pullback_idx, "confirm_idx": confirm_idx,
                "rsi_at_pullback": round(float(pullback["rsi"]), 1),
            }

    if trend == "bearish":
        touched    = abs(pullback["high"] - ema20) <= tolerance
        rsi_ok     = RSI_RESET_LOW <= pullback["rsi"] <= RSI_RESET_HIGH
        confirm_ok = (confirm["close"] < confirm["open"]) and (confirm["close"] < ema20) and strong_body
        if touched and rsi_ok and confirm_ok:
            entry = confirm["close"]
            sl    = entry + atr * SL_ATR_MULT
            if sl <= entry:
                return None
            return {
                "signal": "SELL", "direction": "bearish",
                "entry": entry, "sl": sl, "atr": atr,
                "pullback_idx": pullback_idx, "confirm_idx": confirm_idx,
                "rsi_at_pullback": round(float(pullback["rsi"]), 1),
            }

    return None

# ─────────────────────────────────────────────
#  ✅ TP بر پایه‌ی R:R ثابت (بدون سطوح دستی) - v18
# ─────────────────────────────────────────────
def get_tp_target(entry: float, sl: float, direction: str) -> tuple:
    risk = abs(entry - sl)
    if direction == "bullish":
        tp1 = entry + risk * TP1_RR
        tp2 = entry + risk * TP2_RR
    else:
        tp1 = entry - risk * TP1_RR
        tp2 = entry - risk * TP2_RR
    return tp1, tp2, TP1_RR, TP2_RR

def calc_position(entry: float, sl: float) -> dict:
    risk_pct         = abs(entry - sl) / entry
    position_size    = RISK_USD / risk_pct
    margin_required  = position_size / LEVERAGE
    return {
        "position_size_usd": round(position_size, 2),
        "margin_usd":        round(margin_required, 2),
        "risk_usd":          round(position_size * risk_pct, 2),
        "risk_pct":          round(risk_pct * 100, 3),
    }

# ─────────────────────────────────────────────
#  CHART RENDERER
# ─────────────────────────────────────────────
DARK_BG       = "#0d0f14"
BULL_COLOR    = "#26a69a"
BEAR_COLOR    = "#ef5350"
EMA20_COLOR   = "#FFB300"
EMA50_COLOR   = "#2196F3"
EMA200_COLOR  = "#E040FB"
PIN_COLOR     = "#FFD700"
CONFIRM_COLOR = "#FF6F00"
TP1_COLOR     = "#00E676"
TP2_COLOR     = "#69F0AE"
SL_COLOR      = "#FF1744"
ENTRY_COLOR   = "#FFEB3B"

def draw_candles(ax, df: pd.DataFrame, highlight: dict = None):
    ax.set_facecolor(DARK_BG)
    for i, row in df.iterrows():
        color = BULL_COLOR if row["close"] >= row["open"] else BEAR_COLOR
        ax.plot([i, i], [row["low"], row["high"]], color=color, linewidth=0.8, alpha=0.9)
        body_bottom = min(row["open"], row["close"])
        body_height = max(abs(row["close"] - row["open"]), (row["high"] - row["low"]) * 0.01)
        rect = plt.Rectangle((i - 0.35, body_bottom), 0.7, body_height,
                              facecolor=color, edgecolor=color, linewidth=0.5, alpha=0.95)
        ax.add_patch(rect)
    if highlight:
        pi = highlight.get("pullback_idx")
        if pi is not None and 0 <= pi < len(df):
            row = df.iloc[pi]
            ax.add_patch(plt.Rectangle((pi - 0.4, min(row["open"], row["close"])),
                                        0.8, abs(row["close"] - row["open"]),
                                        facecolor="none", edgecolor=PIN_COLOR,
                                        linewidth=2.5, zorder=10))
            ax.annotate("📍 PULLBACK", xy=(pi, row["high"]),
                        xytext=(pi, row["high"] * 1.001),
                        fontsize=7, color=PIN_COLOR, ha="center", fontweight="bold")
        ci = highlight.get("confirm_idx")
        if ci is not None and 0 <= ci < len(df):
            row = df.iloc[ci]
            ax.add_patch(plt.Rectangle((ci - 0.4, min(row["open"], row["close"])),
                                        0.8, abs(row["close"] - row["open"]),
                                        facecolor="none", edgecolor=CONFIRM_COLOR,
                                        linewidth=2.5, zorder=10))
            ax.annotate("✅ CONFIRM", xy=(ci, row["low"]),
                        xytext=(ci, row["low"] * 0.999),
                        fontsize=7, color=CONFIRM_COLOR, ha="center", fontweight="bold")

def render_chart_4h(df_4h: pd.DataFrame, trend: str, symbol: str) -> io.BytesIO:
    display_bars = min(120, len(df_4h))
    df_plot = df_4h.iloc[-display_bars:].reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(14, 7))
    fig.patch.set_facecolor(DARK_BG)
    draw_candles(ax, df_plot)
    xs = range(display_bars)
    ax.plot(xs, df_plot["ema50"],  color=EMA50_COLOR,  linewidth=1.6, label="EMA50")
    ax.plot(xs, df_plot["ema200"], color=EMA200_COLOR, linewidth=1.8, label="EMA200")
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors="white", labelsize=8)
    ax.spines[:].set_color("#2a2d36")
    ax.yaxis.set_major_formatter(plt.FormatStrFormatter("%.4f"))
    step    = max(1, display_bars // 10)
    xticks  = list(range(0, display_bars, step))
    xlabels = [df_plot["time"].iloc[i].strftime("%m/%d %H:%M") for i in xticks]
    ax.set_xticks(xticks); ax.set_xticklabels(xlabels, rotation=30, ha="right", fontsize=7, color="white")
    trend_fa = "📈 BULLISH" if trend == "bullish" else "📉 BEARISH"
    ax.set_title(f"{symbol} | 4H Trend (EMA50/EMA200) | {trend_fa}",
                 color="white", fontsize=12, fontweight="bold", pad=10)
    ax.grid(color="#1e2130", linewidth=0.5, alpha=0.7)
    ax.legend(facecolor="#1a1d27", edgecolor="#444", labelcolor="white", fontsize=9)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig); buf.seek(0)
    return buf

def render_chart_1h(df_1h: pd.DataFrame, signal: dict, symbol: str) -> io.BytesIO:
    """چارت 1H تاییدیه"""
    display_bars = min(60, len(df_1h))
    df_plot = df_1h.iloc[-display_bars:].reset_index(drop=True)
    offset  = len(df_1h) - display_bars
    highlight = {
        "pullback_idx": signal["pullback_idx"] - offset,
        "confirm_idx":  signal["confirm_idx"]  - offset,
    }
    fig, ax = plt.subplots(figsize=(14, 7))
    fig.patch.set_facecolor(DARK_BG)
    draw_candles(ax, df_plot, highlight=highlight)
    xs = range(display_bars)
    ax.plot(xs, df_plot["ema20"], color=EMA20_COLOR, linewidth=1.4, label="EMA20", alpha=0.9)

    entry = signal["entry"]
    sl    = signal["sl"]
    tp1   = signal.get("tp1", entry)
    tp2   = signal.get("tp2", entry)
    x0    = max(0, highlight["confirm_idx"] - 5)
    x1    = display_bars - 1
    for price, color, style, label in [
        (entry, ENTRY_COLOR, "--", f" ENTRY: {entry:.4f}"),
        (sl,    SL_COLOR,    ":",  f" SL: {sl:.4f}"),
        (tp1,   TP1_COLOR,   "-.", f" TP1: {tp1:.4f}"),
        (tp2,   TP2_COLOR,   "-.", f" TP2: {tp2:.4f}"),
    ]:
        ax.hlines(price, x0, x1, colors=color, linewidths=1.8, linestyles=style, zorder=7)
        ax.annotate(label, xy=(x1, price), color=color, fontsize=8, fontweight="bold", va="center")
    if signal["signal"] == "SELL":
        ax.axhspan(entry, sl, alpha=0.08, color=SL_COLOR)
        ax.axhspan(tp1, entry, alpha=0.08, color=TP1_COLOR)
    else:
        ax.axhspan(sl, entry, alpha=0.08, color=SL_COLOR)
        ax.axhspan(entry, tp1, alpha=0.08, color=TP1_COLOR)
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors="white", labelsize=8)
    ax.spines[:].set_color("#2a2d36")
    ax.yaxis.set_major_formatter(plt.FormatStrFormatter("%.4f"))
    step    = max(1, display_bars // 10)
    xticks  = list(range(0, display_bars, step))
    xlabels = [df_plot["time"].iloc[i].strftime("%m/%d %H:%M") for i in xticks]
    ax.set_xticks(xticks); ax.set_xticklabels(xlabels, rotation=30, ha="right", fontsize=7, color="white")
    rr1 = signal.get("rr1", 0)
    ax.set_title(
        f"{symbol} | 1H Entry | {'🔴 SELL' if signal['signal'] == 'SELL' else '🟢 BUY'} | R:R = {rr1:.1f}",
        color="white", fontsize=12, fontweight="bold", pad=10
    )
    ax.grid(color="#1e2130", linewidth=0.5, alpha=0.7)
    ax.legend(facecolor="#1a1d27", edgecolor="#444", labelcolor="white", fontsize=9)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig); buf.seek(0)
    return buf

# ─────────────────────────────────────────────
#  CAPTION BUILDER
# ─────────────────────────────────────────────
def build_caption(symbol: str, signal: dict, pos: dict, trend: str) -> str:
    sig_emoji = "🔴" if signal["signal"] == "SELL" else "🟢"
    dir_fa    = "فروش (SHORT)" if signal["signal"] == "SELL" else "خرید (LONG)"
    trend_fa  = "صعودی 📈" if trend == "bullish" else "نزولی 📉"
    now       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""
{sig_emoji} <b>سیگنال {dir_fa} | {symbol}</b>
━━━━━━━━━━━━━━━━━━━━
🕐 زمان: <code>{now}</code>

📊 <b>تحلیل روند (4H):</b>
• جهت روند: {trend_fa}
• فیلتر: EMA{EMA_FAST} / EMA{EMA_SLOW}

📍 <b>نقاط معامله (1H):</b>
• ورود: <code>{signal['entry']:.4f}</code>
• استاپ لاس (ATR × {SL_ATR_MULT}): <code>{signal['sl']:.4f}</code>
• تیک پرافیت ۱: <code>{signal.get('tp1', 0):.4f}</code>
• تیک پرافیت ۲: <code>{signal.get('tp2', 0):.4f}</code>
• RSI در لحظه‌ی pullback: {signal.get('rsi_at_pullback', '-')}

💰 <b>مدیریت ریسک:</b>
• ریسک: ${pos['risk_usd']} | لوریج: {LEVERAGE}x
• حجم معامله: ${pos['position_size_usd']}
• مارجین: ${pos['margin_usd']}
• R:R1 = {signal.get('rr1', 0):.1f} | R:R2 = {signal.get('rr2', 0):.1f}

📋 <b>دلایل ورود:</b>
• ✅ روند {trend_fa} تایید شده با EMA{EMA_FAST}/EMA{EMA_SLOW} در 4H
• ✅ Pullback قیمت به EMA{EMA_PULLBACK} در 1H
• ✅ RSI در ناحیه‌ی ریست مومنتوم ({RSI_RESET_LOW}-{RSI_RESET_HIGH})
• ✅ کندل تاییدیه با بدنه‌ی قوی هم‌جهت روند
• ✅ استاپ بر پایه ATR | تارگت با R:R ثابت (بدون سطح دستی)

⚠️ <b>هشدار:</b> صرفاً جنبه آموزشی دارد.
    """.strip()

# ─────────────────────────────────────────────
#  SIGNAL SCANNER
# ─────────────────────────────────────────────
async def scan_symbol(symbol: str, bot: Bot) -> bool:
    log.info(f"Scanning {symbol}...")

    df_4h = get_klines(symbol, "4h", days_back=90)
    if df_4h.empty or len(df_4h) < EMA_SLOW + 5:
        log.warning(f"{symbol}: not enough 4H data"); return False
    df_4h = add_indicators(df_4h)

    df_1h = get_klines(symbol, "1h", days_back=60)
    if df_1h.empty or len(df_1h) < EMA_SLOW + 5:
        log.warning(f"{symbol}: not enough 1H data"); return False
    df_1h = add_indicators(df_1h)

    trend = get_trend_4h(df_4h)
    if not trend:
        return False

    sig = check_entry_signal(df_1h, trend)
    if not sig:
        return False

    tp1, tp2, rr1, rr2 = get_tp_target(sig["entry"], sig["sl"], sig["direction"])
    sig.update({"tp1": tp1, "tp2": tp2, "rr1": rr1, "rr2": rr2})

    if rr1 < MIN_RR:
        log.info(f"{symbol}: R:R={rr1:.1f} too low, skip"); return False

    pos = calc_position(sig["entry"], sig["sl"])

    chart_4h = render_chart_4h(df_4h, trend, symbol)
    chart_1h = render_chart_1h(df_1h, sig, symbol)
    caption  = build_caption(symbol, sig, pos, trend)

    chart_4h.name = f"{symbol}_4h.png"
    chart_1h.name = f"{symbol}_1h.png"

    await bot.send_media_group(
        chat_id=CHAT_ID,
        media=[
            InputMediaPhoto(media=chart_4h, caption=caption, parse_mode=ParseMode.HTML),
            InputMediaPhoto(media=chart_1h),
        ]
    )
    log.info(f"✅ Signal sent for {symbol}!")
    return True

# ─────────────────────────────────────────────
#  CANDLE CLOSE TIMER (1H)
# ─────────────────────────────────────────────
def seconds_until_candle_close(tf_seconds: int = CANDLE_TF_SECONDS) -> float:
    now = time.time()
    return (int(now // tf_seconds) + 1) * tf_seconds - now

def current_candle_id(tf_seconds: int = CANDLE_TF_SECONDS) -> int:
    return int(time.time() // tf_seconds)

# ─────────────────────────────────────────────
#  BACKTEST ENGINE (برای /test)
# ─────────────────────────────────────────────
def simulate_trade(df_1h, signal, future_start_idx):
    entry     = signal["entry"]
    sl        = signal["sl"]
    tp1       = signal["tp1"]
    tp2       = signal["tp2"]
    direction = signal["direction"]
    hit_tp1   = False
    for i in range(future_start_idx, min(future_start_idx + 200, len(df_1h))):
        candle = df_1h.iloc[i]
        h, l   = candle["high"], candle["low"]
        if direction == "bearish":
            if h >= sl:                   return "SL",  i
            if not hit_tp1 and l <= tp1:  hit_tp1 = True
            if hit_tp1 and h >= entry:    return "TP1", i
            if hit_tp1 and l <= tp2:      return "TP2", i
        else:
            if l <= sl:                   return "SL",  i
            if not hit_tp1 and h >= tp1:  hit_tp1 = True
            if hit_tp1 and l <= entry:    return "TP1", i
            if hit_tp1 and h >= tp2:      return "TP2", i
    return "OPEN", -1

def run_backtest_symbol(symbol, df_4h_full, df_1h_full):
    """
    df_4h_full و df_1h_full باید از قبل با add_indicators() آماده شده باشند.
    """
    signals        = []
    min_candles_4h = EMA_SLOW + 5
    min_candles_1h = EMA_SLOW + 5
    if len(df_4h_full) < min_candles_4h or len(df_1h_full) < min_candles_1h:
        return signals

    step = 1
    for scan_idx in range(min_candles_1h, len(df_1h_full) - 1, step):
        scan_time = df_1h_full["time"].iloc[scan_idx]

        df_4h_closed = df_4h_full[df_4h_full["time"] <= scan_time]
        if len(df_4h_closed) < min_candles_4h:
            continue
        trend = get_trend_4h(df_4h_closed)
        if not trend:
            continue

        df_1h_window = df_1h_full.iloc[:scan_idx + 1]
        sig = check_entry_signal(df_1h_window, trend)
        if not sig:
            continue

        if signals:
            last_time = signals[-1]["time"]
            if (scan_time - last_time).total_seconds() < 6 * 3600:
                continue

        entry, sl = sig["entry"], sig["sl"]
        tp1, tp2, rr1, rr2 = get_tp_target(entry, sl, sig["direction"])
        if rr1 < MIN_RR:
            continue

        sig.update({
            "tp1": tp1, "tp2": tp2, "rr1": rr1, "rr2": rr2,
            "time": scan_time, "symbol": symbol,
        })
        result, _ = simulate_trade(df_1h_full, sig, scan_idx + 1)
        sig["result"] = result
        pos  = calc_position(entry, sl)
        risk = pos["risk_usd"]
        if result == "TP2":   pnl = risk * rr2
        elif result == "TP1": pnl = risk * rr1
        elif result == "SL":  pnl = -risk
        else:                 pnl = 0
        sig["pnl"]  = round(pnl, 2)
        sig["risk"] = round(risk, 2)
        signals.append(sig)
    return signals

def build_backtest_report(all_signals):
    total = len(all_signals)
    if total == 0:
        return "❌ هیچ سیگنالی در بازه‌ی بک‌تست یافت نشد."
    wins   = [s for s in all_signals if s["pnl"] > 0]
    losses = [s for s in all_signals if s["pnl"] < 0]
    opens  = [s for s in all_signals if s["result"] == "OPEN"]
    tp2s   = [s for s in all_signals if s["result"] == "TP2"]
    tp1s   = [s for s in all_signals if s["result"] == "TP1"]
    sls    = [s for s in all_signals if s["result"] == "SL"]
    total_pnl     = sum(s["pnl"] for s in all_signals)
    win_rate      = len(wins) / total * 100
    avg_win       = sum(s["pnl"] for s in wins)   / len(wins)   if wins   else 0
    avg_loss      = sum(s["pnl"] for s in losses) / len(losses) if losses else 0
    profit_factor = abs(sum(s["pnl"] for s in wins) / sum(s["pnl"] for s in losses)) if losses else float("inf")
    running = 0; peak = 0; max_dd = 0
    for s in all_signals:
        running += s["pnl"]
        if running > peak: peak = running
        dd = peak - running
        if dd > max_dd: max_dd = dd
    status = "✅ سودده" if total_pnl > 0 else "❌ زیانده"
    best  = "".join([f"• {s['symbol']} {s['signal']} → +${s['pnl']:.2f} ({s['result']})\n"
                     for s in sorted(wins, key=lambda x: x['pnl'], reverse=True)[:3]]) or "—\n"
    worst = "".join([f"• {s['symbol']} {s['signal']} → ${s['pnl']:.2f} ({s['result']})\n"
                     for s in sorted(losses, key=lambda x: x['pnl'])[:3]]) or "—\n"
    detail = f"📋 <b>جزئیات سیگنال‌ها:</b>\n━━━━━━━━━━━━\n"
    for s in all_signals:
        emoji = "✅" if s["pnl"] > 0 else ("❌" if s["pnl"] < 0 else "⏳")
        t = s["time"].strftime("%m/%d %H:%M")
        detail += f"{emoji} {s['symbol'].replace('-USDT','')} {s['signal']} | {t} | {s['result']} | ${s['pnl']:+.2f}\n"
    if len(detail) > 3500:
        detail = detail[:3500] + "\n..."
    report = f"""
🔬 <b>بک‌تست v18 (EMA Trend + RSI Pullback + ATR)</b>
{status} | ریسک: ${RISK_USD} | لوریج: {LEVERAGE}x
━━━━━━━━━━━━━━━━━━━━

📊 <b>آمار کلی:</b>
• کل سیگنال‌ها: {total}
• برنده: {len(wins)} | بازنده: {len(losses)} | باز: {len(opens)}
• TP2: {len(tp2s)} | TP1: {len(tp1s)} | SL: {len(sls)}

💰 <b>نتایج مالی:</b>
• سود/زیان کل: <b>${total_pnl:.2f}</b>
• نرخ برد: {win_rate:.1f}%
• میانگین برد: ${avg_win:.2f}
• میانگین ضرر: ${avg_loss:.2f}
• Profit Factor: {profit_factor:.2f}
• Max Drawdown: ${max_dd:.2f}

📈 <b>بهترین:</b>
{best}
📉 <b>بدترین:</b>
{worst}
{detail}""".strip()
    return report

def render_equity_curve(all_signals):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), gridspec_kw={"height_ratios": [3, 1]})
    fig.patch.set_facecolor(DARK_BG)
    cumulative = [0]
    for s in all_signals:
        cumulative.append(cumulative[-1] + s["pnl"])
    xs = range(len(cumulative))
    ax1.plot(xs, cumulative, color="#2196F3", linewidth=2)
    ax1.fill_between(xs, cumulative, 0, where=[c >= 0 for c in cumulative], color="#00E676", alpha=0.15)
    ax1.fill_between(xs, cumulative, 0, where=[c <  0 for c in cumulative], color="#FF1744", alpha=0.15)
    ax1.axhline(0, color="white", linewidth=0.8, alpha=0.5, linestyle="--")
    ax1.set_facecolor(DARK_BG); ax1.tick_params(colors="white")
    ax1.spines[:].set_color("#2a2d36")
    ax1.set_title("📈 Equity Curve - Backtest v18", color="white", fontsize=13, fontweight="bold")
    ax1.set_ylabel("Cumulative PnL ($)", color="white")
    ax1.grid(color="#1e2130", linewidth=0.5, alpha=0.7)
    colors = ["#00E676" if s["pnl"] > 0 else "#FF1744" for s in all_signals]
    ax2.bar(range(len(all_signals)), [s["pnl"] for s in all_signals], color=colors, alpha=0.85)
    ax2.axhline(0, color="white", linewidth=0.8, alpha=0.5)
    ax2.set_facecolor(DARK_BG); ax2.tick_params(colors="white")
    ax2.spines[:].set_color("#2a2d36")
    ax2.set_ylabel("Trade PnL ($)", color="white")
    ax2.grid(color="#1e2130", linewidth=0.5, alpha=0.5, axis="y")
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig); buf.seek(0)
    return buf

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bot     = context.bot
    await bot.send_message(
        chat_id=chat_id,
        text="🔬 <b>شروع بک‌تست روی بازار فعلی...</b>\n"
             f"📊 {len(SYMBOLS)} ارز | روند: EMA (4H) | ورود: Pullback+RSI (1H)\n"
             "⏳ لطفاً صبر کنید (۲-۴ دقیقه)",
        parse_mode=ParseMode.HTML
    )
    all_signals = []
    for idx, symbol in enumerate(SYMBOLS):
        log.info(f"[{idx+1}/{len(SYMBOLS)}] Backtesting {symbol}...")
        try:
            df_4h = get_klines(symbol, "4h", days_back=90)
            df_1h = get_klines(symbol, "1h", days_back=60)
            if df_4h.empty or df_1h.empty:
                continue
            if len(df_4h) < EMA_SLOW + 5 or len(df_1h) < EMA_SLOW + 5:
                continue
            df_4h = add_indicators(df_4h)
            df_1h = add_indicators(df_1h)
            sigs = run_backtest_symbol(symbol, df_4h, df_1h)
            all_signals.extend(sigs)
            await asyncio.sleep(0.5)
        except Exception as e:
            log.error(f"Error {symbol}: {e}")
    all_signals.sort(key=lambda x: x["time"])
    report = build_backtest_report(all_signals)
    if all_signals:
        equity_chart = render_equity_curve(all_signals)
        equity_chart.name = "equity.png"
        await bot.send_photo(chat_id=chat_id, photo=equity_chart,
                             caption=report, parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(chat_id=chat_id, text=report, parse_mode=ParseMode.HTML)
    log.info(f"✅ /test done | signals: {len(all_signals)}")

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
async def main():
    log.info("🚀 Signal Bot v18 started!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("test", cmd_test))

    bot = app.bot
    me  = await bot.get_me()
    log.info(f"Bot connected: @{me.username}")

    wait_first = seconds_until_candle_close()
    log.info(f"⏳ Waiting {wait_first:.0f}s for first 1H candle close...")

    await bot.send_message(
        chat_id=CHAT_ID,
        text=f"🤖 <b>Signal Bot v18 فعال شد!</b>\n"
             f"📊 استراتژی: روند EMA{EMA_FAST}/{EMA_SLOW} (4H) + Pullback به EMA{EMA_PULLBACK} + RSI (1H) + ATR SL/TP\n"
             f"🔍 اسکن {len(SYMBOLS)} ارز\n"
             f"⏱ اولین اسکن در {wait_first:.0f} ثانیه (بسته شدن کندل 1H)\n"
             f"💡 برای تست بازار: /test",
        parse_mode=ParseMode.HTML
    )

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        await asyncio.sleep(wait_first + 2)

        signals_sent = set()

        while True:
            candle_id   = current_candle_id()
            cycle_start = time.time()
            log.info(f"=== 1H Candle #{candle_id} | {datetime.now().strftime('%H:%M:%S')} ===")

            for symbol in SYMBOLS:
                try:
                    sig_key = f"{symbol}_{candle_id}"
                    if sig_key in signals_sent:
                        continue
                    sent = await scan_symbol(symbol, bot)
                    if sent:
                        signals_sent.add(sig_key)
                        await asyncio.sleep(2)
                    await asyncio.sleep(1.2)
                except Exception as e:
                    log.error(f"Error scanning {symbol}: {e}")

            signals_sent = {k for k in signals_sent if int(k.split("_")[-1]) >= candle_id - 3}

            elapsed   = time.time() - cycle_start
            wait_next = seconds_until_candle_close()
            log.info(f"✅ Cycle done in {elapsed:.0f}s | Next 1H candle in {wait_next:.0f}s")
            await asyncio.sleep(wait_next + 2)

if __name__ == "__main__":
    asyncio.run(main())
