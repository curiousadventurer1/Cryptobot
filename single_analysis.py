"""
موتور تحلیل حرفه‌ای تک‌تایم‌فریمی

روش کار:
هر اندیکاتور یه رأی وزن‌دار (bull/bear/neutral) میده. جمع امتیاز وزن‌دار
جهت قالب رو مشخص می‌کنه و درصد اطمینان از نسبت |امتیاز| به حداکثر
امتیاز ممکن به دست میاد. فقط اندیکاتورهایی که هم‌جهت با سیگنال نهایی
هستن توی لیست «دلایل» نمایش داده می‌شن.

اندیکاتورها و وزن پایه‌شون:
- EMA 12/26 Cross          وزن 2   (تقاطع کوتاه‌مدت/میان‌مدت)
- روند EMA50 (فیلتر روند)   وزن 1   (قیمت بالا/پایین EMA50)
- SMA 20/50 Trend          وزن 2   (روند میان‌مدت)
- MACD Cross               وزن 1
- MACD Histogram Momentum  وزن 1   (هیستوگرام در حال قوی‌تر شدنه یا ضعیف‌تر شدن)
- RSI Zone                 وزن 1   (بالای ۵۵ صعودی، زیر ۴۵ نزولی، بینابین خنثی)
- موقعیت باند بولینگر       وزن 1   (نزدیکی به باند بالا/پایین)
- اسپایک حجم                وزن 1   (هم‌جهت با کندل آخر)
- روند OBV                 وزن 1   (جریان تجمیعی پول - تاییدکننده‌ی مهم)
- الگوی کندل‌استیک          وزن 1
- واگرایی RSI/قیمت          وزن 2   (سیگنال بازگشتی قوی، وقتی معتبر تشخیص داده بشه)

حداکثر امتیاز پایه = 2+1+2+1+1+1+1+1+1+1+2 = 14 (واگرایی همیشه رأی نمی‌ده)

** فاز ۱ تقویت موتور (اضافه‌شده): **
- وزن اندیکاتورهای روندی (EMA/SMA/MACD/OBV) و بازگشت‌به‌میانگین (RSI/بولینگر)
  دیگه ثابت نیست؛ بر اساس قدرت روند (ADX) به‌صورت پویا تنظیم می‌شه: توی
  بازار پرروند، اندیکاتورهای روندی وزن بیشتر می‌گیرن و بالعکس توی بازار
  رنج/بی‌روند، RSI/بولینگر وزن بیشتری می‌گیرن. پس max_score دیگه همیشه
  دقیقاً ۱۴ نیست - بسته به رژیم بازار تفاوت می‌کنه (که کاملاً عادیه).
- آستانه‌ی صدور سیگنال (confidence_threshold_fraction) با رژیم نوسان
  (ATR percentile) تطبیق پیدا می‌کنه - توی نوسان بالا/شدید سخت‌گیرتر می‌شیم.
- یه لایه‌ی دوم امتیازدهی (apply_context_confluence) اضافه شده که
  اطلاعات زمینه‌ای (هم‌راستایی تایم‌فریم بالاتر، همبستگی BTC، فشار
  اردربوک) رو - که قبلاً فقط نمایشی بودن - به یه تعدیل روی درصد اطمینان
  نهایی تبدیل می‌کنه (بدون تغییر جهت سیگنال یا سطوح ورود/SL/TP).
- الگوهای کندلی بیشتری تشخیص داده می‌شن: ستاره‌ی صبح/عصر (Morning/Evening
  Star) و دوجی برگشتی، علاوه بر پوشا/چکش/ستاره‌ی تیرانداز قبلی.

** فاز ۲ تقویت موتور (اضافه‌شده): **
- واگرایی RSI/قیمت تعمیم داده شد به MACD Histogram و OBV هم (وزن 2 هر
  کدوم، جدا از واگرایی RSI) - چون این دو اندیکاتور برخلاف RSI مقیاس ثابت
  ۰-۱۰۰ ندارن، آستانه‌ی تشخیص به‌جای عدد مطلق، نسبی و بر پایه‌ی انحراف
  معیار خود اندیکاتور توی بازه‌ی اخیره (MIN_DIVERGENCE_INDICATOR_GAP_STD_MULT).
- یادگیری از تاریخچه‌ی عملکرد سیگنال‌های گذشته (signal_performance): اگه
  برای یه ترکیب دقیق نماد+تایم‌فریم+جهت حداقل ۵ سیگنال قطعی‌شده (نه در
  انتظار) توی تاریخچه موجود باشه، نرخ برد تاریخی از طریق
  apply_context_confluence روی درصد اطمینان نهایی هم تأثیر می‌گذاره (بونوس
  در نرخ برد بالا، جزا در نرخ برد پایین).

حد ضرر (SL): ترکیب ATR و آخرین Swing High/Low - هرکدوم منطقی‌تر و
نزدیک‌تر به ساختار قیمت بود انتخاب می‌شه (نه صرفاً یه ضریب ثابت).

تارگت‌های سود (TP1/TP2/TP3): بر پایه‌ی نسبت ریسک‌به‌ریوارد ۱:۱، ۱:۲، ۱:۳
نسبت به فاصله‌ی ورود تا حد ضرر (R-multiple) - روش استاندارد مدیریت ریسک
در معامله‌گری حرفه‌ای.
"""
from dataclasses import dataclass, field
import pandas as pd
from trade_validation import validate_signal_output
from market_data_validation import validate_ohlcv_frame

SWING_LOOKBACK = 20          # تعداد کندل برای تشخیص آخرین سقف/کف
ATR_SL_MULT = 1.5            # ضریب پیش‌فرض ATR برای حد ضرر
MAX_STRUCTURE_SL_ATR_MULT = 3.0  # حداکثر فاصله‌ی مجاز SL ساختاری (بر حسب ATR) تا نامنطقی نشه
RR_TARGETS = [1.0, 2.0, 3.0]     # نسبت‌های ریسک‌به‌ریوارد برای TP1/TP2/TP3

ENTRY_PULLBACK_ATR_MULT = 0.3      # فاصله‌ی پیش‌فرض ورود پیشنهادی از قیمت فعلی (بر حسب ATR)
MAX_ENTRY_PULLBACK_ATR_MULT = 1.2  # حداکثر فاصله‌ی مجاز پولبک تا EMA12 (وگرنه فیرواقعی می‌شه)

MIN_CANDLES_FOR_ANALYSIS = 100
MIN_CANDLES_REQUIRED = 60  # کمتر از این تعداد کندل واقعی، اندیکاتورهایی مصل SMA50 معتبر نیستن

TIMEFRAME_LABELS_FA = {
    "1m": "۱ دقیقه",
    "5m": "۵ دقیقه",
    "15m": "۱۵ دقیقه",
    "30m": "۳۰ دقیقه",
    "1h": "۱ ساعته",
    "4h": "۴ ساعته",
    "1d": "روزانه",
    "1w": "هفتگی",
}


def add_extended_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """محاسبه‌ی همه‌ی اندیکاتورهای لازم برای موتور حرفه‌ای"""
    validate_ohlcv_frame(df, min_candles=MIN_CANDLES_REQUIRED)
    from ta.momentum import RSIIndicator
    from ta.trend import MACD, EMAIndicator, SMAIndicator
    from ta.volatility import AverageTrueRange, BollingerBands
    from ta.volume import OnBalanceVolumeIndicator

    close = df["close"]

    df["rsi"] = RSIIndicator(close=close, window=14).rsi()

    macd_calc = MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
    df["macd"] = macd_calc.macd()
    df["macd_signal"] = macd_calc.macd_signal()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    df["sma20"] = SMAIndicator(close=close, window=20).sma_indicator()
    df["sma50"] = SMAIndicator(close=close, window=50).sma_indicator()
    df["ema12"] = EMAIndicator(close=close, window=12).ema_indicator()
    df["ema26"] = EMAIndicator(close=close, window=26).ema_indicator()
    df["ema50"] = EMAIndicator(close=close, window=50).ema_indicator()

    df["atr"] = AverageTrueRange(high=df["high"], low=df["low"], close=close, window=14).average_true_range()

    from ta.trend import ADXIndicator
    adx_calc = ADXIndicator(high=df["high"], low=df["low"], close=close, window=14)
    df["adx"] = adx_calc.adx()

    bb = BollingerBands(close=close, window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()

    df["obv"] = OnBalanceVolumeIndicator(close=close, volume=df["volume"]).on_balance_volume()

    df["volume_sma"] = df["volume"].rolling(window=20).mean()

    return df


# ---------- تشخیص الگوی کندل‌استیک (سبک و مستقل، بدون نیاز به ta) ----------

def _detect_last_candle_pattern(df: pd.DataFrame) -> dict | None:
    """
    بررسی چند کندل آخر برای الگوهای پرکاربرد؛ خروجی {"name":.., "bias": 1|-1} یا None

    فاز ۱ تقویت موتور: علاوه بر پوشا/چکش/ستاره‌ی تیرانداز (قبلاً موجود
    بود)، الگوهای ستاره‌ی صبح/عصر (۳کندلی) و دوجی برگشتی هم اضافه شدن.
    """
    if len(df) < 2:
        return None
    last = df.iloc[-1]
    prev = df.iloc[-2]

    last_body = abs(last["close"] - last["open"])
    last_range = last["high"] - last["low"]
    prev_body = abs(prev["close"] - prev["open"])
    if last_range == 0:
        return None

    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]
    last_bullish = last["close"] > last["open"]
    prev_bullish = prev["close"] > prev["open"]

    # --- ستاره‌ی صبح/عصر (۳کندلی) - اولویت اول چون معمولاً معتبرترن ---
    if len(df) >= 3:
        first = df.iloc[-3]
        first_body = abs(first["close"] - first["open"])
        first_bullish = first["close"] > first["open"]
        mid_range = prev["high"] - prev["low"]
        mid_is_small = (prev_body <= mid_range * 0.35) if mid_range > 0 else False

        if (first_body > 0 and not first_bullish and mid_is_small and last_bullish
                and last_body > first_body * 0.5
                and last["close"] > (first["open"] + first["close"]) / 2):
            return {"name": "ستاره‌ی صبح (Morning Star)", "bias": 1}

        if (first_body > 0 and first_bullish and mid_is_small and not last_bullish
                and last_body > first_body * 0.5
                and last["close"] < (first["open"] + first["close"]) / 2):
            return {"name": "ستاره‌ی عصر (Evening Star)", "bias": -1}

    # --- پوشا (Engulfing) ---
    if prev_body > 0:
        if (not prev_bullish and last_bullish
                and last["close"] >= prev["open"] and last["open"] <= prev["close"]
                and last_body > prev_body):
            return {"name": "پوشای صعودی (Bullish Engulfing)", "bias": 1}
        if (prev_bullish and not last_bullish
                and last["open"] >= prev["close"] and last["close"] <= prev["open"]
                and last_body > prev_body):
            return {"name": "پوشای نزولی (Bearish Engulfing)", "bias": -1}

    # --- چکش / ستاره‌ی تیرانداز ---
    if last_body > 0 and last_body / last_range < 0.35:
        if lower_wick > last_body * 2 and upper_wick < last_body * 0.5:
            return {"name": "چکش (Hammer)", "bias": 1}
        if upper_wick > last_body * 2 and lower_wick < last_body * 0.5:
            return {"name": "ستاره‌ی تیرانداز (Shooting Star)", "bias": -1}

    # --- دوجی برگشتی (بعد از چند کندل هم‌جهت) ---
    if last_range > 0 and (last_body / last_range) < 0.1 and len(df) >= 4:
        prior = df.iloc[-4:-1]
        prior_up = (prior["close"] > prior["open"]).sum() >= 2 and prior["close"].iloc[-1] > prior["close"].iloc[0]
        prior_down = (prior["close"] < prior["open"]).sum() >= 2 and prior["close"].iloc[-1] < prior["close"].iloc[0]
        if prior_up:
            return {"name": "دوجی برگشتی بعد از روند صعودی (احتیاط)", "bias": -1}
        if prior_down:
            return {"name": "دوجی برگشتی بعد از روند نزولی (احتیاط)", "bias": 1}

    return None


# ---------- تشخیص واگرایی RSI/قیمت ----------

DIVERGENCE_LOOKBACK = 30
PIVOT_WINDOW = 3  # تعداد کندل هر طرف برای تایید سقف/کف محلی
MIN_RSI_DIVERGENCE_GAP = 5.0    # حداقل اختلاف RSI بین دو پیوت تا واگرایی معتبر باشه (فیلتر نویز)
MIN_PRICE_MOVE_ATR_MULT = 0.5   # حداقل فاصله‌ی قیمتی بین دو پیوت (بر حسب ATR) تا معنادار باشه
MIN_DIVERGENCE_INDICATOR_GAP_STD_MULT = 0.5  # حداقل اختلاف اندیکاتور (بر حسب انحراف معیار) برای واگرایی MACD/OBV - چون این‌ها برخلاف RSI مقیاس ثابت ۰-۱۰۰ ندارن


def _find_pivots(series: pd.Series, window: int, find_highs: bool) -> list[int]:
    """اندیس‌های سقف یا کف‌های محلی رو برمی‌گردونه (مقایسه با window کندل هر طرف)"""
    idxs = []
    n = len(series)
    for i in range(window, n - window):
        segment = series.iloc[i - window: i + window + 1]
        center = series.iloc[i]
        if find_highs and center == segment.max() and (segment == center).sum() == 1:
            idxs.append(i)
        elif not find_highs and center == segment.min() and (segment == center).sum() == 1:
            idxs.append(i)
    return idxs


def _detect_rsi_divergence(df: pd.DataFrame) -> dict | None:
    """
    واگرایی صعودی: قیمت کف پایین‌تر می‌سازه ولی RSI کف بالاتر می‌سازه
    (نشونه‌ی تضعیف فشار فروش، احتمال برگشت صعودی)
    واگرایی نزولی: قیمت سقف بالاتر می‌سازه ولی RSI سقف پایین‌تر می‌سازه
    (نشونه‌ی تضعیف فشار خرید، احتمال برگشت نزولی)

    پیوت آخر (نزدیک‌ترین به الان) با «مرجع‌دارترین» پیوت قبلی مقایسه می‌شه -
    یعنی برای کف‌ها، پایین‌ترین کف قبلی (نه صرفاً یکی‌مونده‌به‌آخر که ممکنه
    یه نوسان جزئی و کم‌اهمیت باشه)، و برای سقف‌ها، بالاترین سقف قبلی.
    """
    recent = df.tail(DIVERGENCE_LOOKBACK).reset_index(drop=True)
    if len(recent) < 2 * PIVOT_WINDOW + 5 or recent["rsi"].isna().any():
        return None

    atr_val = float(df["atr"].iloc[-1]) if not pd.isna(df["atr"].iloc[-1]) else 0.0
    min_price_gap = MIN_PRICE_MOVE_ATR_MULT * atr_val if atr_val > 0 else 0.0

    low_pivots = _find_pivots(recent["low"], PIVOT_WINDOW, find_highs=False)
    if len(low_pivots) >= 2:
        i_last = low_pivots[-1]
        prior_candidates = low_pivots[:-1]
        i_ref = min(prior_candidates, key=lambda i: recent["low"].iloc[i])  # پایین‌ترین کف قبلی
        price_gap = recent["low"].iloc[i_ref] - recent["low"].iloc[i_last]
        rsi_gap = recent["rsi"].iloc[i_last] - recent["rsi"].iloc[i_ref]
        if price_gap > min_price_gap and rsi_gap > MIN_RSI_DIVERGENCE_GAP:
            return {"type": "bullish", "bias": 1}

    high_pivots = _find_pivots(recent["high"], PIVOT_WINDOW, find_highs=True)
    if len(high_pivots) >= 2:
        i_last = high_pivots[-1]
        prior_candidates = high_pivots[:-1]
        i_ref = max(prior_candidates, key=lambda i: recent["high"].iloc[i])  # بالاترین سقف قبلی
        price_gap = recent["high"].iloc[i_last] - recent["high"].iloc[i_ref]
        rsi_gap = recent["rsi"].iloc[i_ref] - recent["rsi"].iloc[i_last]
        if price_gap > min_price_gap and rsi_gap > MIN_RSI_DIVERGENCE_GAP:
            return {"type": "bearish", "bias": -1}

    return None


def _detect_generic_indicator_divergence(df: pd.DataFrame, indicator_col: str) -> dict | None:
    """
    نسخه‌ی عمومی‌شده‌ی _detect_rsi_divergence برای اندیکاتورهایی مثل
    MACD Histogram یا OBV که برخلاف RSI مقیاس ثابت ۰ تا ۱۰۰ ندارن. به‌جای
    آستانه‌ی مطلق (MIN_RSI_DIVERGENCE_GAP)، از انحراف معیار خود اندیکاتور
    توی بازه‌ی اخیر به‌عنوان آستانه‌ی نسبی استفاده می‌کنه.
    """
    recent = df.tail(DIVERGENCE_LOOKBACK).reset_index(drop=True)
    if len(recent) < 2 * PIVOT_WINDOW + 5 or recent[indicator_col].isna().any():
        return None

    atr_val = float(df["atr"].iloc[-1]) if not pd.isna(df["atr"].iloc[-1]) else 0.0
    min_price_gap = MIN_PRICE_MOVE_ATR_MULT * atr_val if atr_val > 0 else 0.0
    indicator_std = float(recent[indicator_col].std())
    min_indicator_gap = MIN_DIVERGENCE_INDICATOR_GAP_STD_MULT * indicator_std if indicator_std > 0 else 0.0

    low_pivots = _find_pivots(recent["low"], PIVOT_WINDOW, find_highs=False)
    if len(low_pivots) >= 2:
        i_last = low_pivots[-1]
        prior_candidates = low_pivots[:-1]
        i_ref = min(prior_candidates, key=lambda i: recent["low"].iloc[i])
        price_gap = recent["low"].iloc[i_ref] - recent["low"].iloc[i_last]
        indicator_gap = recent[indicator_col].iloc[i_last] - recent[indicator_col].iloc[i_ref]
        if price_gap > min_price_gap and indicator_gap > min_indicator_gap:
            return {"type": "bullish", "bias": 1}

    high_pivots = _find_pivots(recent["high"], PIVOT_WINDOW, find_highs=True)
    if len(high_pivots) >= 2:
        i_last = high_pivots[-1]
        prior_candidates = high_pivots[:-1]
        i_ref = max(prior_candidates, key=lambda i: recent["high"].iloc[i])
        price_gap = recent["high"].iloc[i_last] - recent["high"].iloc[i_ref]
        indicator_gap = recent[indicator_col].iloc[i_ref] - recent[indicator_col].iloc[i_last]
        if price_gap > min_price_gap and indicator_gap > min_indicator_gap:
            return {"type": "bearish", "bias": -1}

    return None


def _detect_macd_divergence(df: pd.DataFrame) -> dict | None:
    """واگرایی بین MACD Histogram و قیمت (نسخه‌ی تعمیم‌یافته‌ی واگرایی RSI)"""
    return _detect_generic_indicator_divergence(df, "macd_hist")


def _detect_obv_divergence(df: pd.DataFrame) -> dict | None:
    """واگرایی بین OBV و قیمت (نسخه‌ی تعمیم‌یافته‌ی واگرایی RSI)"""
    return _detect_generic_indicator_divergence(df, "obv")


# ---------- محاسبه‌ی نقطه‌ی ورود پیشنهادی (نه صرفاً قیمت لحظه‌ای) ----------

def _suggest_entry(price: float, atr: float, direction: str, ema12: float) -> tuple[float, str]:
    """
    به‌جای پیشنهاد قیمت لحظه‌ای به‌عنوان نقطه‌ی ورود (که یعنی خرید/فروش در
    همون لحظه، بدون فاصله از نوسان آنی)، یه نقطه‌ی ورود کمی منطقی‌تر
    پیشنهاد می‌ده: یا پولبک به EMA12 (اگه فاصله‌ش معقول بود)، یا حداقل
    یه فاصله‌ی کوچک (۰.۳ ATR) از قیمت فعلی - تا در اوج/کف لحظه‌ای وارد نشی.
    """
    if direction == "BUY":
        buffer_entry = price - ENTRY_PULLBACK_ATR_MULT * atr
        if ema12 and 0 < (price - ema12) <= MAX_ENTRY_PULLBACK_ATR_MULT * atr:
            entry = max(buffer_entry, ema12)  # هرکدوم به قیمت نزدیک‌تره (احتمال پرشدن سفارش بیشتر)
            basis = f"پولبک به EMA12 (${ema12:,.4f})" if ema12 < 1 else f"پولبک به EMA12 (${ema12:,.2f})"
        else:
            entry = buffer_entry
            basis = f"{ENTRY_PULLBACK_ATR_MULT}× ATR پایین‌تر از قیمت فعلی (اجتناب از ورود در اوج لحظه‌ای)"
        return entry, basis

    if direction == "SELL":
        buffer_entry = price + ENTRY_PULLBACK_ATR_MULT * atr
        if ema12 and 0 < (ema12 - price) <= MAX_ENTRY_PULLBACK_ATR_MULT * atr:
            entry = min(buffer_entry, ema12)
            basis = f"پولبک به EMA12 (${ema12:,.4f})" if ema12 < 1 else f"پولبک به EMA12 (${ema12:,.2f})"
        else:
            entry = buffer_entry
            basis = f"{ENTRY_PULLBACK_ATR_MULT}× ATR بالاتر از قیمت فعلی (اجتناب از ورود در کف لحظه‌ای)"
        return entry, basis

    return price, None


# ---------- محاسبه‌ی سطوح ورود/حد ضرر/تارگت ----------

# ---------- فیبوناچی (تاییدکننده‌ی SL/ورود) ----------

def _calc_fibonacci_confirmation(swing_low: float, swing_high: float, price_to_check: float,
                                  atr: float) -> str | None:
    """
    اگه یه قیمت مشخص (مثلاً SL یا نقطه‌ی ورود) نزدیک یکی از سطوح
    فیبوناچی رتریسمنت (بر پایه‌ی آخرین Swing) باشه، یه متن تاییدکننده
    برمی‌گردونه؛ وگرنه None. این صرفاً جنبه‌ی اطلاعاتیه، توی امتیازدهی
    دخالت نداره - فقط اعتبار بیشتری به سطح محاسبه‌شده می‌ده.
    """
    from config import FIBONACCI_LEVELS, FIBONACCI_TOLERANCE_ATR_MULT

    diff = swing_high - swing_low
    if diff <= 0 or atr <= 0:
        return None

    tolerance = FIBONACCI_TOLERANCE_ATR_MULT * atr
    for level in FIBONACCI_LEVELS:
        fib_price = swing_high - diff * level
        if abs(price_to_check - fib_price) <= tolerance:
            return f"نزدیک سطح فیبوناچی {level:.3f} (${fib_price:,.4f})"
    return None


# ---------- تشخیص رژیم نوسان (بر پایه‌ی صدک ATR) ----------

def _detect_volatility_regime(df: pd.DataFrame) -> dict:
    """
    ATR فعلی رو نسبت به تاریخچه‌ی خودش (نه یه عدد مطلق) می‌سنجد - چون
    نوسان «بالا» برای BTC و یه شیت‌کوین کوچیک کاملاً متفاوته. خروجی:
    سطح (کم/عادی/بالا/شدید) + درصد صدک + ضریب پیشنهادی کاهش حجم پوزیشن.
    """
    from config import (
        VOLATILITY_LOOKBACK, VOLATILITY_HIGH_PERCENTILE, VOLATILITY_EXTREME_PERCENTILE,
        VOLATILITY_HIGH_RISK_MULT, VOLATILITY_EXTREME_RISK_MULT,
    )

    atr_series = df["atr"].tail(VOLATILITY_LOOKBACK).dropna()
    if len(atr_series) < 20:
        return {"level": "نامشخص", "percentile": None, "risk_mult": 1.0}

    current_atr = atr_series.iloc[-1]
    percentile = float((atr_series < current_atr).mean() * 100)

    if percentile >= VOLATILITY_EXTREME_PERCENTILE:
        return {"level": "شدید 🔥", "percentile": percentile, "risk_mult": VOLATILITY_EXTREME_RISK_MULT}
    if percentile >= VOLATILITY_HIGH_PERCENTILE:
        return {"level": "بالا ⚠️", "percentile": percentile, "risk_mult": VOLATILITY_HIGH_RISK_MULT}
    return {"level": "عادی", "percentile": percentile, "risk_mult": 1.0}


# ---------- وزن‌دهی پویا بر اساس قدرت روند (ADX) - فاز ۱ تقویت موتور ----------

def _adx_adjusted_weight(base_weight: float, weight_category: str, adx_val: float) -> float:
    """
    وزن هر اندیکاتور رو بر اساس قدرت روند (ADX) تنظیم می‌کنه: توی بازار
    پرروند، اندیکاتورهای روندی (EMA/SMA/MACD/OBV) قابل‌اعتمادترن؛ توی
    بازار رنج/بی‌روند، اندیکاتورهای بازگشت‌به‌میانگین (RSI/بولینگر)
    قابل‌اعتمادترن. اندیکاتورهای مستقل (حجم/الگو/واگرایی) دست‌نخورده
    می‌مونن - این‌ها فارِف از رژیم بازار معتبرن.

    weight_category یه برچسب داخلیه، جدا از category نمایشی توی votes
    (که فقط برای گروه‌بندی متن دلایل استفاده می‌شه).
    """
    from config import (
        ADX_TREND_THRESHOLD, ADX_RANGE_THRESHOLD,
        ADX_TREND_WEIGHT_BOOST, ADX_TREND_WEIGHT_CUT,
        ADX_MEANREV_WEIGHT_BOOST, ADX_MEANREV_WEIGHT_CUT,
    )
    if weight_category == "trend":
        if adx_val >= ADX_TREND_THRESHOLD:
            return base_weight * ADX_TREND_WEIGHT_BOOST
        if adx_val < ADX_RANGE_THRESHOLD:
            return base_weight * ADX_TREND_WEIGHT_CUT
        return base_weight
    if weight_category == "mean_reversion":
        if adx_val < ADX_RANGE_THRESHOLD:
            return base_weight * ADX_MEANREV_WEIGHT_BOOST
        if adx_val >= ADX_TREND_THRESHOLD:
            return base_weight * ADX_MEANREV_WEIGHT_CUT
        return base_weight
    return base_weight


def _calc_levels(df: pd.DataFrame, price: float, atr: float, direction: str, ema12: float,
                  atr_sl_mult: float = None, rr_targets: list = None) -> dict:
    """
    محاسبه‌ی نقطه‌ی ورود پیشنهادی، حد ضرر (ترکیب ATR و Swing High/Low) و
    ۳ تارگت سود بر پایه‌ی R-multiple (نسبت به فاصله‌ی ورود تا حد ضرر)

    atr_sl_mult و rr_targets اختیاریان - اگه داده نشن از مقادیر پیش‌فرض
    همین فایل استفاده می‌شه؛ پنل وب می‌تونه این‌ها رو override کنه.
    """
    atr_sl_mult = atr_sl_mult if atr_sl_mult is not None else ATR_SL_MULT
    rr_targets = rr_targets if rr_targets is not None else RR_TARGETS

    recent = df.tail(SWING_LOOKBACK)
    swing_low = float(recent["low"].min())
    swing_high = float(recent["high"].max())

    entry, entry_basis = _suggest_entry(price, atr, direction, ema12)

    if direction == "BUY":
        atr_sl = entry - atr_sl_mult * atr
        structure_sl = swing_low - 0.3 * atr
        # اگه حد ضرر ساختاری منطقی‌تر (نزدیک‌تر به قیمت) و در بازه‌ی معقول بود، اون رو انتخاب کن
        if structure_sl < entry and (entry - structure_sl) <= MAX_STRUCTURE_SL_ATR_MULT * atr:
            sl = structure_sl
            sl_basis = "زیر آخرین کف قیمتی (Swing Low)"
        else:
            sl = atr_sl
            sl_basis = f"{atr_sl_mult}× ATR"
        risk = entry - sl
        tps = [entry + risk * rr for rr in rr_targets]
    elif direction == "SELL":
        atr_sl = entry + atr_sl_mult * atr
        structure_sl = swing_high + 0.3 * atr
        if structure_sl > entry and (structure_sl - entry) <= MAX_STRUCTURE_SL_ATR_MULT * atr:
            sl = structure_sl
            sl_basis = "بالای آخرین سقف قیمتی (Swing High)"
        else:
            sl = atr_sl
            sl_basis = f"{atr_sl_mult}× ATR"
        risk = sl - entry
        tps = [entry - risk * rr for rr in rr_targets]
    else:
        entry, entry_basis, sl, sl_basis, tps, risk = price, None, None, None, [], None

    # Validate output without changing score, direction, level formulas or target order.
    validate_signal_output(direction, price, entry, sl, tps)
    fib_confirmation = None
    if sl is not None:
        fib_confirmation = _calc_fibonacci_confirmation(swing_low, swing_high, sl, atr)

    return {
        "entry": entry, "entry_basis": entry_basis, "sl": sl, "sl_basis": sl_basis,
        "tps": tps, "risk": risk, "support": swing_low, "resistance": swing_high,
        "fib_confirmation": fib_confirmation,
    }


# ---------- نتیجه‌ی نهایی ----------

@dataclass
class SingleTFResult:
    symbol: str
    timeframe: str
    price: float
    rsi: float
    macd: float
    macd_signal: float
    sma20: float
    sma50: float
    ema12: float
    ema26: float
    ema50: float
    atr: float
    adx: float
    score: float
    max_score: float
    direction: str  # "BUY" | "SELL" | "NEUTRAL"
    reasons: list = field(default_factory=list)
    pattern: dict | None = None
    divergence: dict | None = None
    macd_divergence: dict | None = None
    obv_divergence: dict | None = None
    entry: float = 0.0
    entry_basis: str = None
    sl: float = None
    sl_basis: str = None
    tps: list = field(default_factory=list)
    risk: float = None
    quote_volume_24h: float = 0.0
    price_change_24h_percent: float = 0.0
    liquidity_level: str = "نامشخص"
    fib_confirmation: str = None
    volatility_level: str = "نامشخص"
    volatility_percentile: float = None
    volatility_risk_mult: float = 1.0
    support: float = None
    resistance: float = None
    # --- فاز ۱ تقویت موتور: نتیجه‌ی هم‌نوایی زمینه‌ای ---
    context_adjusted_confidence_percent: int = None
    context_notes: list = field(default_factory=list)

    @property
    def confidence_percent(self) -> int:
        if self.max_score == 0:
            return 0
        return int(min(abs(self.score) / self.max_score, 1.0) * 100)

    @property
    def risk_reward_text(self) -> str:
        return "۱:۱ / ۱:۲ / ۱:۳"


def _classify_liquidity(quote_volume_24h: float) -> str:
    if quote_volume_24h >= 50_000_000:
        return "بالا 🟢"
    if quote_volume_24h >= 5_000_000:
        return "متوسط 🟡"
    return "پایین 🔴 (ریسک اسپرد/لغزش قیمت بیشتر)"


DEFAULT_CONFIDENCE_THRESHOLD_FRACTION = 0.25  # حداقل فاصله‌ی امتیاز از صفر (نسبت به حداکثر) برای صدور سیگنال قطعی


def build_single_result(df: pd.DataFrame, symbol: str, timeframe: str,
                         confidence_threshold_fraction: float = None,
                         atr_sl_mult: float = None, rr_targets: list = None) -> SingleTFResult:
    """
    df باید خروجی add_extended_indicators باشه (شامل حداقل ۵۰ کندل معتبر)

    سه پارامتر آخر اختیاریان و از پنل وب/تنظیمات دیتابیس قابل override
    هستن؛ اگه داده نشن، از مقادیر پیش‌فرض همین فایل استفاده می‌شه.
    """
    from config import VOLATILITY_THRESHOLD_HIGH_MULT, VOLATILITY_THRESHOLD_EXTREME_MULT

    confidence_threshold_fraction = (
        confidence_threshold_fraction if confidence_threshold_fraction is not None
        else DEFAULT_CONFIDENCE_THRESHOLD_FRACTION
    )
    last = df.iloc[-1]
    price = float(last["close"])
    atr = float(last["atr"]) if not pd.isna(last["atr"]) else 0.0

    pattern = _detect_last_candle_pattern(df)

    adx_val = float(last["adx"]) if not pd.isna(last.get("adx", float("nan"))) else 0.0
    volatility = _detect_volatility_regime(df)

    # آستانه‌ی صدور سیگنال رو با توجه به نوسان فعلی بازار تنظیم می‌کنیم:
    # توی نوسان بالا/شدید، برای جلوگیری از سیگنال‌های نویزی، سخت‌گیرتر می‌شیم
    effective_threshold_fraction = confidence_threshold_fraction
    if volatility["level"].startswith("شدید"):
        effective_threshold_fraction *= VOLATILITY_THRESHOLD_EXTREME_MULT
    elif volatility["level"].startswith("بالا"):
        effective_threshold_fraction *= VOLATILITY_THRESHOLD_HIGH_MULT
    effective_threshold_fraction = min(effective_threshold_fraction, 0.9)  # سقف منطقی

    # هر آیتم: (وزن, حالت صعودی؟, متن دلیل صعودی, متن دلیل نزولی, دسته)
    # دسته‌ی "trend" یعنی این اندیکاتور صرفاً یه نمای دیگه از همون روند
    # قیمته (به‌شدت با بقیه‌ی دسته‌ی trend هم‌بسته‌ست) - این‌ها بعداً توی
    # پیام به یه خط ترکیبی خلاصه می‌شن تا لیست دلایل برای ارزهای متفاوت
    # یکسان و تکراری به‌نظر نرسه. دسته‌ی "unique" یعنی سیگنال مستقل و
    # متمایزکننده‌ست (این‌ها همیشه جدا نمایش داده می‌شن).
    # (این دسته‌بندی فقط برای فرمت متن دلایله؛ وزن‌دهی پویای ADX از
    # weight_category جدا مشخص می‌شه - پایین‌تر توضیح داده شده.)
    votes = []

    votes.append((
        _adx_adjusted_weight(2, "trend", adx_val),
        last["ema12"] > last["ema26"], "کراس صعودی EMA 12/26", "کراس نزولی EMA 12/26", "trend"
    ))

    if not pd.isna(last["ema50"]):
        votes.append((
            _adx_adjusted_weight(1, "trend", adx_val),
            price > last["ema50"], "قیمت بالای EMA50 (روند صعودی)", "قیمت زیر EMA50 (روند نزولی)", "trend"
        ))

    votes.append((
        _adx_adjusted_weight(2, "trend", adx_val),
        last["sma20"] > last["sma50"], "روند صعودی SMA (20>50)", "روند نزولی SMA (20<50)", "trend"
    ))

    votes.append((
        _adx_adjusted_weight(1, "trend", adx_val),
        last["macd"] > last["macd_signal"], "کراس صعودی MACD", "کراس نزولی MACD", "trend"
    ))

    if len(df) >= 4 and not df["macd_hist"].iloc[-3:].isna().any():
        hist_trend_up = df["macd_hist"].iloc[-1] > df["macd_hist"].iloc[-3]
        votes.append((
            _adx_adjusted_weight(1, "trend", adx_val),
            hist_trend_up, "هیستوگرام MACD در حال تقویت", "هیستوگرام MACD در حال تضعیف", "trend"
        ))

    rsi_val = float(last["rsi"]) if not pd.isna(last["rsi"]) else 50.0
    if rsi_val >= 55:
        votes.append((_adx_adjusted_weight(1, "mean_reversion", adx_val), True, f"RSI در ناحیه‌ی صعودی ({rsi_val:.1f})", "", "unique"))
    elif rsi_val <= 45:
        votes.append((_adx_adjusted_weight(1, "mean_reversion", adx_val), False, "", f"RSI در ناحیه‌ی نزولی ({rsi_val:.1f})", "unique"))
    # بین ۴۵ تا ۵۵: رأی نمی‌ده (خنثی)

    if not pd.isna(last["bb_upper"]) and not pd.isna(last["bb_lower"]):
        band_width = last["bb_upper"] - last["bb_lower"]
        if band_width > 0:
            dist_lower = abs(price - last["bb_lower"]) / band_width
            dist_upper = abs(price - last["bb_upper"]) / band_width
            w = _adx_adjusted_weight(1, "mean_reversion", adx_val)
            if dist_lower < 0.15:
                votes.append((w, True, "قیمت نزدیک باند پایین بولینگر (اشباع فروش)", "", "unique"))
            elif dist_upper < 0.15:
                votes.append((w, False, "", "قیمت نزدیک باند بالای بولینگر (اشباع خرید)", "unique"))

    if not pd.isna(last["volume_sma"]) and last["volume_sma"] > 0 and len(df) >= 21:
        if last["volume"] >= last["volume_sma"] * 1.5:
            candle_bull = last["close"] > last["open"]
            votes.append((1, candle_bull, "اسپایک حجم هم‌جهت با کندل صعودی", "اسپایک حجم هم‌جهت با کندل نزولی", "unique"))

    if len(df) >= 15 and not df["obv"].iloc[-15:].isna().any():
        obv_trend_up = df["obv"].iloc[-1] > df["obv"].iloc[-15]
        votes.append((
            _adx_adjusted_weight(1, "trend", adx_val),
            obv_trend_up, "روند صعودی OBV (تایید جریان پول خرید)", "روند نزولی OBV (تایید جریان پول فروش)", "unique"
        ))

    if pattern:
        if pattern["bias"] > 0:
            votes.append((1, True, f"الگوی کندلی: {pattern['name']}", "", "unique"))
        elif pattern["bias"] < 0:
            votes.append((1, False, "", f"الگوی کندلی: {pattern['name']}", "unique"))

    divergence = _detect_rsi_divergence(df)
    if divergence:
        if divergence["bias"] > 0:
            votes.append((2, True, "🔀 واگرایی صعودی RSI/قیمت (سیگنال بازگشتی قوی)", "", "unique"))
        else:
            votes.append((2, False, "", "🔀 واگرایی نزولی RSI/قیمت (سیگنال بازگشتی قوی)", "unique"))

    macd_divergence = _detect_macd_divergence(df)
    if macd_divergence:
        if macd_divergence["bias"] > 0:
            votes.append((2, True, "🔀 واگرایی صعودی MACD/قیمت (سیگنال بازگشتی قوی)", "", "unique"))
        else:
            votes.append((2, False, "", "🔀 واگرایی نزولی MACD/قیمت (سیگنال بازگشتی قوی)", "unique"))

    obv_divergence = _detect_obv_divergence(df)
    if obv_divergence:
        if obv_divergence["bias"] > 0:
            votes.append((2, True, "🔀 واگرایی صعودی OBV/قیمت (سیگنال بازگشتی قوی)", "", "unique"))
        else:
            votes.append((2, False, "", "🔀 واگرایی نزولی OBV/قیمت (سیگنال بازگشتی قوی)", "unique"))

    score = 0.0
    max_score = 0.0
    for weight, is_bull, bull_text, bear_text, category in votes:
        max_score += weight
        if is_bull:
            score += weight
        else:
            score -= weight

    # آستانه: حداقل بخشی از حداکثر امتیاز فاصله از صفر لازمه تا سیگنال
    # قطعی صادر بشه (این بخش با رژیم نوسان بازار تطبیق پیدا کرده - بالاتر)
    threshold = max_score * effective_threshold_fraction
    if score >= threshold:
        direction = "BUY"
    elif score <= -threshold:
        direction = "SELL"
    else:
        direction = "NEUTRAL"

    reasons = []
    if direction in ("BUY", "SELL"):
        matching_trend = []
        matching_unique = []
        for weight, is_bull, bull_text, bear_text, category in votes:
            matches_direction = (direction == "BUY" and is_bull) or (direction == "SELL" and not is_bull)
            if not matches_direction:
                continue
            text = bull_text if direction == "BUY" else bear_text
            if not text:
                continue
            if category == "trend":
                matching_trend.append(text)
            else:
                matching_unique.append(text)

        # اگه ۳ یا بیشتر اندیکاتور روندی هم‌جهت بودن (که خیلی وقت‌ها توی
        # بازار پرروند اتفاق می‌افته)، به‌جای تکرار، یه خط ترکیبی
        # می‌سازیم تا پیام برای ارزهای متفاوت متمایزتر به‌نظر برسد و
        # اندیکاتورهای منحصربه‌فرد (RSI/OBV/حجم/الگو) بیشتر دیده بشن.
        if len(matching_trend) >= 3:
            dir_word = "صعودی" if direction == "BUY" else "نزولی"
            reasons.append(f"روند {dir_word} در {len(matching_trend)} اندیکاتور (EMA/SMA/MACD) تایید شد")
        else:
            reasons.extend(matching_trend)

        # دلایل منحصربه‌فرد رو اول لیست می‌ذاریم چون اطلاعات بیشتری دارن
        reasons = matching_unique + reasons

    levels = _calc_levels(
        df, price, atr, direction, float(last["ema12"]) if not pd.isna(last["ema12"]) else None,
        atr_sl_mult=atr_sl_mult, rr_targets=rr_targets
    )

    return SingleTFResult(
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        rsi=rsi_val,
        macd=float(last["macd"]) if not pd.isna(last["macd"]) else 0.0,
        macd_signal=float(last["macd_signal"]) if not pd.isna(last["macd_signal"]) else 0.0,
        sma20=float(last["sma20"]) if not pd.isna(last["sma20"]) else 0.0,
        sma50=float(last["sma50"]) if not pd.isna(last["sma50"]) else 0.0,
        ema12=float(last["ema12"]) if not pd.isna(last["ema12"]) else 0.0,
        ema26=float(last["ema26"]) if not pd.isna(last["ema26"]) else 0.0,
        ema50=float(last["ema50"]) if not pd.isna(last["ema50"]) else 0.0,
        atr=atr,
        adx=adx_val,
        score=score,
        max_score=max_score,
        direction=direction,
        reasons=reasons,
        pattern=pattern,
        divergence=divergence,
        macd_divergence=macd_divergence,
        obv_divergence=obv_divergence,
        entry=levels["entry"],
        entry_basis=levels["entry_basis"],
        sl=levels["sl"],
        sl_basis=levels["sl_basis"],
        tps=levels["tps"],
        risk=levels["risk"],
        fib_confirmation=levels["fib_confirmation"],
        volatility_level=volatility["level"],
        volatility_percentile=volatility["percentile"],
        volatility_risk_mult=volatility["risk_mult"],
        support=levels["support"],
        resistance=levels["resistance"],
    )


# ---------- هم‌نوایی زمینه‌ای (Context Confluence) - فاز ۱ تقویت موتور ----------

def apply_context_confluence(result: SingleTFResult, higher_tf_info: dict = None,
                              btc_corr_info: dict = None, order_book_info: dict = None,
                              historical_performance_info: dict = None,
                              second_higher_tf_info: dict = None) -> SingleTFResult:
    """
    اطلاعات زمینه‌ای (هم‌راستایی تایم‌فریم بالاتر، همبستگی BTC، فشار
    اردربوک، عملکرد تاریخی سیگنال‌های مشابه) که قبلاً فقط توی پیام نمایش
    داده می‌شدن (یا اصلاً وجود نداشتن) رو به یه تعدیل روی درصد اطمینان
    نهایی تبدیل می‌کنه. جهت سیگنال و سطوح ورود/SL/TP که از قبل بر
    پایه‌ی خود موتور محاسبه شدن، دست‌نخورده می‌مونن - این تابع فقط «چقدر بهش
    مطمئن باشیم» رو تعدیل می‌کنه.

    نتیجه توی result.context_adjusted_confidence_percent ذخیره می‌شه؛
    اگه هیچ‌کدام از پنج ورودی موجود نبود، برابر confidence_percent اصلی می‌مونه.
    """
    from config import (
        CONTEXT_HTF_ALIGN_BONUS, CONTEXT_HTF_CONFLICT_PENALTY,
        CONTEXT_BTC_HIGH_CORR_PENALTY, CONTEXT_ORDERBOOK_BONUS, CONTEXT_ORDERBOOK_PENALTY,
        ORDER_BOOK_IMBALANCE_THRESHOLD,
        CONTEXT_HISTORICAL_WIN_RATE_BONUS_THRESHOLD, CONTEXT_HISTORICAL_WIN_RATE_PENALTY_THRESHOLD,
        CONTEXT_HISTORICAL_PERFORMANCE_BONUS, CONTEXT_HISTORICAL_PERFORMANCE_PENALTY,
        CONTEXT_MULTI_TF_FULL_ALIGN_BONUS, CONTEXT_MULTI_TF_FULL_CONFLICT_PENALTY,
    )

    base = result.confidence_percent
    if result.direction == "NEUTRAL":
        result.context_adjusted_confidence_percent = base
        return result

    adjustment = 0
    notes = []

    if higher_tf_info:
        if higher_tf_info["aligned"]:
            if higher_tf_info["higher_tf_direction"] != "NEUTRAL":
                adjustment += CONTEXT_HTF_ALIGN_BONUS
                notes.append("هم‌جهت با تایم‌فریم بالاتر")
        else:
            adjustment -= CONTEXT_HTF_CONFLICT_PENALTY
            notes.append("مخالف روند تایم‌فریم بالاتر")

    if higher_tf_info and second_higher_tf_info:
        first_aligned = higher_tf_info["aligned"] and higher_tf_info["higher_tf_direction"] != "NEUTRAL"
        second_aligned = second_higher_tf_info["aligned"] and second_higher_tf_info["higher_tf_direction"] != "NEUTRAL"
        if first_aligned and second_aligned:
            adjustment += CONTEXT_MULTI_TF_FULL_ALIGN_BONUS
            notes.append("هم‌جهت با هر دو تایم‌فریم بالاتر")
        elif not higher_tf_info["aligned"] and not second_higher_tf_info["aligned"]:
            adjustment -= CONTEXT_MULTI_TF_FULL_CONFLICT_PENALTY
            notes.append("مخالف هر دو تایم‌فریم بالاتر — احتیاط جدی")

    if btc_corr_info and btc_corr_info.get("high_correlation"):
        adjustment -= CONTEXT_BTC_HIGH_CORR_PENALTY
        notes.append("همبستگی بالا با BTC (استقلال کمتر)")

    if order_book_info:
        bid_ratio = order_book_info["bid_ratio"]
        book_supports_buy = bid_ratio >= ORDER_BOOK_IMBALANCE_THRESHOLD
        book_supports_sell = bid_ratio <= (1 - ORDER_BOOK_IMBALANCE_THRESHOLD)
        if result.direction == "BUY" and book_supports_buy:
            adjustment += CONTEXT_ORDERBOOK_BONUS
            notes.append("اردربوک هم‌جهت (فشار خرید)")
        elif result.direction == "SELL" and book_supports_sell:
            adjustment += CONTEXT_ORDERBOOK_BONUS
            notes.append("اردربوک هم‌جهت (فشار فروش)")
        elif result.direction == "BUY" and book_supports_sell:
            adjustment -= CONTEXT_ORDERBOOK_PENALTY
            notes.append("اردربوک مخالف (فشار فروش)")
        elif result.direction == "SELL" and book_supports_buy:
            adjustment -= CONTEXT_ORDERBOOK_PENALTY
            notes.append("اردربوک مخالف (فشار خرید)")

    if historical_performance_info:
        win_rate = historical_performance_info["win_rate"]
        sample_size = historical_performance_info["sample_size"]
        if win_rate >= CONTEXT_HISTORICAL_WIN_RATE_BONUS_THRESHOLD:
            adjustment += CONTEXT_HISTORICAL_PERFORMANCE_BONUS
            notes.append(f"عملکرد تاریخی قوی برای این نوع سیگنال ({win_rate:.0f}% برد از {sample_size} نمونه)")
        elif win_rate <= CONTEXT_HISTORICAL_WIN_RATE_PENALTY_THRESHOLD:
            adjustment -= CONTEXT_HISTORICAL_PERFORMANCE_PENALTY
            notes.append(f"عملکرد تاریخی ضعیف برای این نوع سیگنال ({win_rate:.0f}% برد از {sample_size} نمونه)")

    result.context_adjusted_confidence_percent = max(0, min(100, base + adjustment))
    result.context_notes = notes
    return result
