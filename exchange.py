"""
لایه ارتباط با صرافی (Binance) از طریق ccxt
فقط از endpoint های عمومی استفاده می‌شه - نیازی به API Key نیست
"""
import asyncio
import logging
import time

import ccxt.async_support as ccxt
import pandas as pd
from config import EXCHANGE_ID
from market_data_validation import ohlcv_frame_from_raw, drop_unclosed_candle

logger = logging.getLogger(__name__)

# یک نمونهٔ مشترک ccxt برای کل پروسه: بازارها فقط یک‌بار دانلود می‌شوند و
# rate-limiter داخلی ccxt بین همهٔ درخواست‌های هم‌زمان مشترک است.
MARKETS_REFRESH_SECONDS = 6 * 3600

_shared_exchange = None
_markets_loaded_at = 0.0
_markets_lock = None


def _get_shared_exchange():
    global _shared_exchange
    if _shared_exchange is None:
        exchange_class = getattr(ccxt, EXCHANGE_ID)
        _shared_exchange = exchange_class({"enableRateLimit": True})
    return _shared_exchange


async def ensure_markets_loaded(force: bool = False):
    """بارگذاری/تازه‌سازی بازارها با قفل؛ اگر تازه‌سازی شکست بخورد، لیست قبلی حفظ می‌شود."""
    global _markets_loaded_at, _markets_lock
    exchange = _get_shared_exchange()
    fresh = exchange.markets and (time.monotonic() - _markets_loaded_at) < MARKETS_REFRESH_SECONDS
    if fresh and not force:
        return
    if _markets_lock is None:
        _markets_lock = asyncio.Lock()
    async with _markets_lock:
        fresh = exchange.markets and (time.monotonic() - _markets_loaded_at) < MARKETS_REFRESH_SECONDS
        if fresh and not force:
            return
        try:
            await exchange.load_markets(reload=bool(exchange.markets))
            _markets_loaded_at = time.monotonic()
        except Exception:
            if not exchange.markets:
                raise
            logger.warning("تازه‌سازی لیست بازارها ناموفق بود؛ لیست قبلی استفاده می‌شود.", exc_info=True)
            _markets_loaded_at = time.monotonic() - MARKETS_REFRESH_SECONDS + 300  # ۵ دقیقه بعد دوباره تلاش


async def close_shared_exchange():
    """فقط هنگام خاموش‌شدن ربات صدا زده شود."""
    global _shared_exchange
    if _shared_exchange is not None:
        try:
            await _shared_exchange.close()
        finally:
            _shared_exchange = None


class ExchangeClient:
    """
    پوستهٔ سبک روی نمونهٔ مشترک ccxt. ساختن آن هزینه‌ای ندارد و close()
    عمداً کاری نمی‌کند تا کدهای قبلی (try/finally: await client.close())
    بدون تغییر کار کنند. اتصال واقعی در close_shared_exchange بسته می‌شود.
    """

    def __init__(self):
        self.exchange = _get_shared_exchange()

    async def close(self):
        return None

    async def fetch_ohlcv_df(self, symbol: str, timeframe: str, limit: int = 100,
                             closed_only: bool = False) -> pd.DataFrame:
        """
        دریافت کندل‌ها و تبدیل به DataFrame
        ستون‌ها: timestamp, open, high, low, close, volume
        اعتبارسنجی سخت‌گیرانهٔ کل پاسخ؛ دادهٔ خراب، تکراری یا نامرتب
        حذف یا اصلاح نمی‌شود و تحلیل با خطای کنترل‌شده متوقف می‌شود.
        از پاسخ معتبر، حداکثر آخرین `limit` کندل انتخاب می‌شود.
        """
        if not closed_only:
            raw = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            return ohlcv_frame_from_raw(raw, limit=limit)
        # closed_only: یه کندل بیشتر می‌گیریم تا بعد از حذف کندل در حال
        # شکل‌گیری، باز هم `limit` کندل بسته‌شده داشته باشیم
        raw = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit + 1)
        df = ohlcv_frame_from_raw(raw, limit=limit + 1)
        df = drop_unclosed_candle(df, timeframe)
        return df.tail(limit).reset_index(drop=True)

    async def fetch_ticker_price(self, symbol: str) -> float:
        ticker = await self.exchange.fetch_ticker(symbol)
        return ticker["last"]

    async def validate_symbol(self, symbol: str) -> bool:
        """چک می‌کنه که نماد روی صرافی وجود داره یا نه (از لیست کش‌شدهٔ بازارها)"""
        try:
            await ensure_markets_loaded()
            return symbol in self.exchange.markets
        except Exception:
            return False

    async def load_markets(self):
        await ensure_markets_loaded(force=True)

    async def fetch_top_movers(self, top_n: int = 5, min_quote_volume: float = 3_000_000) -> dict:
        """
        برگردوندن پرطرفدارترین ارزهای صعودی و نزولی بر اساس تغییر ۲۴ساعته
        فقط جفت‌های USDT با حجم معاملات کافی در نظر گرفته می‌شن تا نویز حذف بشه.
        خروجی: {"gainers": [...], "losers": [...]}
        """
        tickers = await self.exchange.fetch_tickers()
        usdt_pairs = []
        for symbol, t in tickers.items():
            if not symbol.endswith("/USDT"):
                continue
            quote_volume = t.get("quoteVolume") or 0
            change = t.get("percentage")
            if change is None or quote_volume < min_quote_volume:
                continue
            usdt_pairs.append({
                "symbol": symbol,
                "change": change,
                "price": t.get("last"),
                "quote_volume": quote_volume,
            })

        gainers = sorted(usdt_pairs, key=lambda x: x["change"], reverse=True)[:top_n]
        losers = sorted(usdt_pairs, key=lambda x: x["change"])[:top_n]
        return {"gainers": gainers, "losers": losers}

    async def fetch_order_book_imbalance(self, symbol: str, depth: int = 20) -> dict | None:
        """
        فشار خرید/فروش لحظه‌ای رو از روی عمق اردربوک (نه کندل تاریخی)
        می‌سنجه - مجموع حجم سفارش‌های خرید (bid) در برابر فروش (ask) توی
        N سطح قیمتی بالای اردربوک. این کاملاً مستقل از اندیکاتورهای
        کندلی‌ه، پس زمینه‌ی تازه‌ای اضافه می‌کنه.
        خروجی: {"bid_ratio": float 0..1, "pressure": "buy"|"sell"|"neutral"}
        یا None اگه صرافی اردربوک رو پشتیبانی نکنه یا خطا بده.
        """
        try:
            book = await self.exchange.fetch_order_book(symbol, limit=depth)
        except Exception:
            return None

        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None

        bid_volume = sum(vol for _, vol in bids[:depth])
        ask_volume = sum(vol for _, vol in asks[:depth])
        total = bid_volume + ask_volume
        if total <= 0:
            return None

        bid_ratio = bid_volume / total
        return {"bid_ratio": bid_ratio, "bid_volume": bid_volume, "ask_volume": ask_volume}


def normalize_symbol(user_input: str) -> str:
    """
    تبدیل ورودی کاربر به فرمت استاندارد ccxt
    مثال: btcusdt -> BTC/USDT ، BTC -> BTC/USDT
    """
    s = user_input.strip().upper().replace("-", "").replace("_", "")
    if "/" in s:
        return s
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT"
    # اگه فقط اسم کوین رو داد (مثل BTC) پیش‌فرض USDT رو می‌ذاریم
    return f"{s}/USDT"
