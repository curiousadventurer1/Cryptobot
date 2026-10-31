"""Strict OHLCV input checks; never sort, repair or silently discard bad candles."""
import math
from numbers import Real

import pandas as pd


OHLCV_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


class MarketDataError(ValueError):
    """Controlled validation failure without embedding the provider's payload."""

    def __init__(self, code, *, row=None, column=None, actual_candles=None, min_candles=None):
        self.code = code
        self.row = row
        self.column = column
        self.actual_candles = actual_candles
        self.min_candles = min_candles
        super().__init__(code + (f" at candle {row}" if row is not None else ""))


def market_data_warning(error):
    """Plain, Markdown-safe Persian warning; no raw prices or exception dump."""
    reasons = {
        "INVALID_ROWS": "ساختار دادهٔ دریافتی صحیح نیست.",
        "INVALID_COLUMNS": "ستون‌های لازمِ دادهٔ کندلی کامل و یکتا نیستند.",
        "INVALID_PRICE": "قیمت کندل باید عدد مثبت و متناهی باشد.",
        "INVALID_VOLUME": "حجم کندل باید عدد متناهی و غیرمنفی باشد.",
        "INVALID_OHLC": "سقف و کف کندل با قیمت بازشدن یا بسته‌شدن سازگار نیست.",
        "INVALID_TIMESTAMP": "زمان کندل معتبر نیست.",
        "DUPLICATE_TIMESTAMP": "زمان تکراری در کندل‌ها وجود دارد.",
        "UNORDERED_TIMESTAMP": "ترتیب زمانی کندل‌ها صحیح نیست.",
        "INVALID_LIMIT": "تعداد کندلِ درخواستی معتبر نیست.",
    }
    if error.code == "INSUFFICIENT_CANDLES":
        reason = f"فقط {error.actual_candles} کندل موجود است؛ حداقل {error.min_candles} کندل لازم است."
    else:
        reason = reasons.get(error.code, "دادهٔ کندلی معتبر نیست.")
    return (
        f"⚠️ تحلیل به‌دلیل دادهٔ کندلی نامعتبر یا ناکافی متوقف شد.\n{reason}\n"
        "سیگنال قابل‌پیگیری صادر و ثبت نشد. کمی بعد دوباره تلاش کن یا تایم‌فریم دیگری انتخاب کن."
    )


def _valid_number(value, *, positive):
    # Check before pandas coercion: a bool or numeric string is not an API price.
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        numeric = float(value)
    except (ValueError, TypeError, OverflowError):
        return False
    return math.isfinite(numeric) and (numeric > 0 if positive else numeric >= 0)


def _check_values(values, row):
    for column, value in zip(OHLCV_COLUMNS[1:], values):
        positive = column != "volume"
        if not _valid_number(value, positive=positive):
            raise MarketDataError("INVALID_PRICE" if positive else "INVALID_VOLUME",
                                  row=row, column=column)
    opening, high, low, closing, _ = values
    if not (low <= min(opening, closing) <= max(opening, closing) <= high):
        raise MarketDataError("INVALID_OHLC", row=row)


def validate_ohlcv_frame(df, *, min_candles=0):
    """Validate raw columns in supplied order, without mutating the DataFrame.

    Extra indicator columns (including expected warmup NaNs) are ignored.
    Gaps may be legitimate on illiquid markets and are neither filled nor rejected.
    This function makes no decision about freshness or the still-open last candle.
    """
    if not isinstance(df, pd.DataFrame) or any(
        list(df.columns).count(name) != 1 for name in OHLCV_COLUMNS
    ):
        raise MarketDataError("INVALID_COLUMNS")
    timestamps = df["timestamp"]
    if not pd.api.types.is_datetime64_any_dtype(timestamps) or timestamps.isna().any():
        raise MarketDataError("INVALID_TIMESTAMP")
    epoch = pd.Timestamp(0, tz=timestamps.dt.tz)
    if (timestamps < epoch).any():
        raise MarketDataError("INVALID_TIMESTAMP")
    if timestamps.duplicated().any():
        raise MarketDataError("DUPLICATE_TIMESTAMP")
    if not timestamps.is_monotonic_increasing:
        raise MarketDataError("UNORDERED_TIMESTAMP")
    # Do not use iterrows: it can coerce bools/integers across columns.
    for row, values in enumerate(df.loc[:, list(OHLCV_COLUMNS[1:])].itertuples(index=False, name=None), 1):
        _check_values(values, row)
    if len(df) < min_candles:
        raise MarketDataError("INSUFFICIENT_CANDLES",
                              actual_candles=len(df), min_candles=min_candles)
    return df


def ohlcv_frame_from_raw(raw, *, limit):
    """Convert ccxt six-field rows only after strict type/value checks.

    ccxt timestamps are integral Unix milliseconds. Preserve existing naive-UTC
    DataFrame representation. Validate the whole batch BEFORE selecting its tail.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise MarketDataError("INVALID_LIMIT")
    if not isinstance(raw, (list, tuple)):
        raise MarketDataError("INVALID_ROWS")
    max_timestamp_ms = pd.Timestamp.max.value // 1_000_000
    previous = None
    seen = set()
    for row_number, candle in enumerate(raw, 1):
        if not isinstance(candle, (list, tuple)) or len(candle) != 6:
            raise MarketDataError("INVALID_ROWS", row=row_number)
        timestamp = candle[0]
        if not _valid_number(timestamp, positive=False):
            raise MarketDataError("INVALID_TIMESTAMP", row=row_number)
        if timestamp != int(timestamp) or timestamp > max_timestamp_ms:
            raise MarketDataError("INVALID_TIMESTAMP", row=row_number)
        if timestamp in seen:
            raise MarketDataError("DUPLICATE_TIMESTAMP", row=row_number)
        if previous is not None and timestamp < previous:
            raise MarketDataError("UNORDERED_TIMESTAMP", row=row_number)
        seen.add(timestamp)
        previous = timestamp
        _check_values(candle[1:], row_number)
    df = pd.DataFrame(raw, columns=list(OHLCV_COLUMNS))
    try:
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", errors="raise")
    except (ValueError, TypeError, OverflowError) as error:
        raise MarketDataError("INVALID_TIMESTAMP") from error
    validate_ohlcv_frame(df)
    return df.tail(limit).reset_index(drop=True)