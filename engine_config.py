"""
تنظیمات موتور سیگنال (فاز ۳) - همه از جدول bot_settings و قابل تغییر از پنل وب.

هر بهبود فاز ۳ یه کلید روشن/خاموش داره تا اگه نتیجه‌ش رو نپسندیدی، بدون
ری‌دیپلوی از پنل وب (صفحه‌ی تنظیمات) برگردی به رفتار قبلی:

engine_closed_candles   (1) تحلیل فقط روی کندل‌های بسته‌شده (بدون repaint)
engine_ohlc_tracking    (1) پایش سیگنال‌ها با سقف/کف کندل‌های ۵ دقیقه‌ای به‌جای قیمت لحظه‌ای
engine_history_v2       (1) امتیاز «عملکرد تاریخی» بر پایه‌ی میانگین R (نه نرخ برد خام)
engine_trend_block      (0) امتیازدهی جدید «بلوک روند» - پیش‌فرض خاموش؛ امتیاز جدید
                            کنار امتیاز فعلی حساب و لاگ می‌شه (shadow) تا بعد از
                            مقایسه با بک‌تست روشنش کنی
backtest_live_model     (1) بک‌تست با همون مدل مدیریت معامله‌ی پایش زنده
fee_rate_pct            (0.1) کارمزد هر طرف معامله به درصد (Binance taker ≈ 0.1)
slippage_pct            (0.05) لغزش قیمت برای خروج‌های بازار (SL/timeout) به درصد
fee_filter_max_r        (0) اگه > 0: سیگنالی که هزینه‌ی رفت‌وبرگشتش بیشتر از این
                            کسر از ریسک (R) باشه صادر نمی‌شه. ۰ = خاموش
"""
import asyncio
import time
from dataclasses import dataclass

import database as db

_CACHE_TTL_SECONDS = 30.0

FLAG_DEFAULTS = {
    "engine_closed_candles": True,
    "engine_ohlc_tracking": True,
    "engine_history_v2": True,
    "engine_trend_block": False,
    "backtest_live_model": True,
}
FLOAT_DEFAULTS = {
    "fee_rate_pct": 0.1,
    "slippage_pct": 0.05,
    "fee_filter_max_r": 0.0,
}
HISTORY_SINCE_KEY = "history_v2_since"


@dataclass(frozen=True)
class EngineConfig:
    closed_candles: bool = True
    ohlc_tracking: bool = True
    history_v2: bool = True
    trend_block: bool = False
    backtest_live_model: bool = True
    fee_rate_pct: float = 0.1
    slippage_pct: float = 0.05
    fee_filter_max_r: float = 0.0
    history_since: str | None = None

    @property
    def scoring_mode(self) -> str:
        return "trend_block" if self.trend_block else "classic"

    @property
    def fee_rate(self) -> float:
        """کسر (نه درصد)"""
        return self.fee_rate_pct / 100.0

    @property
    def slippage(self) -> float:
        return self.slippage_pct / 100.0


DEFAULT_ENGINE_CONFIG = EngineConfig()

_cached: EngineConfig | None = None
_cached_at = 0.0
_lock = asyncio.Lock()


def parse_flag(raw, default: bool) -> bool:
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def parse_nonneg_float(raw, default: float) -> float:
    import math
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value >= 0 else default


def invalidate_cache():
    global _cached
    _cached = None


async def get_engine_config(force: bool = False) -> EngineConfig:
    """با کش کوتاه (۳۰ ثانیه) تا هر تحلیل چند کوئری اضافه نزنه"""
    global _cached, _cached_at
    if not force and _cached is not None and time.monotonic() - _cached_at < _CACHE_TTL_SECONDS:
        return _cached
    async with _lock:
        if not force and _cached is not None and time.monotonic() - _cached_at < _CACHE_TTL_SECONDS:
            return _cached
        try:
            settings = await db.get_all_settings()
        except Exception:
            settings = {}
        cfg = EngineConfig(
            closed_candles=parse_flag(settings.get("engine_closed_candles"), FLAG_DEFAULTS["engine_closed_candles"]),
            ohlc_tracking=parse_flag(settings.get("engine_ohlc_tracking"), FLAG_DEFAULTS["engine_ohlc_tracking"]),
            history_v2=parse_flag(settings.get("engine_history_v2"), FLAG_DEFAULTS["engine_history_v2"]),
            trend_block=parse_flag(settings.get("engine_trend_block"), FLAG_DEFAULTS["engine_trend_block"]),
            backtest_live_model=parse_flag(settings.get("backtest_live_model"), FLAG_DEFAULTS["backtest_live_model"]),
            fee_rate_pct=parse_nonneg_float(settings.get("fee_rate_pct"), FLOAT_DEFAULTS["fee_rate_pct"]),
            slippage_pct=parse_nonneg_float(settings.get("slippage_pct"), FLOAT_DEFAULTS["slippage_pct"]),
            fee_filter_max_r=parse_nonneg_float(settings.get("fee_filter_max_r"), FLOAT_DEFAULTS["fee_filter_max_r"]),
            history_since=settings.get(HISTORY_SINCE_KEY),
        )
        _cached, _cached_at = cfg, time.monotonic()
        return cfg
