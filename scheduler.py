"""
اسکن خودکار واچ‌لیست کاربران در پس‌زمینه
هر SCAN_INTERVAL ثانیه، همه‌ی نمادهای واچ‌شده تحلیل می‌شن و اگه سیگنال
نسبت به آخرین باری که ثبت شده تغییر کرده باشه (و NEUTRAL نباشه)، به
کاربرانی که اون نماد رو واچ کردن و autoscan فعاله پیام می‌فرسته.
"""
import logging
import asyncio
import math
import time
import aiosqlite
from collections import defaultdict
from telegram.ext import Application
from telegram.constants import ParseMode
from exchange import ExchangeClient
from signals import analyze_symbol, SIGNAL_EMOJI, SIGNAL_FA
import database as db
import backup
from news import news_item_key

logger = logging.getLogger(__name__)
NEWS_SEND_TIMEOUT_SECONDS = 60  # shorter than the DB reservation lease (10 minutes)
SCAN_CONCURRENCY = 5  # حداکثر نماد هم‌زمان در اسکن/قیمت‌گیری پس‌زمینه (rate-limit ccxt همچنان اعمال می‌شود)


async def _background_recipients(user_ids, kind: str) -> set[int]:
    """انتخاب اولیه؛ خطای خواندن سیاست، مجوز ارسال ایجاد نمی‌کند."""
    try:
        return await db.get_background_notification_recipients(user_ids, kind)
    except aiosqlite.Error:
        logger.exception("خواندن سیاست گیرندگان %s ناموفق بود؛ ارسال متوقف شد.", kind)
        return set()


async def _can_send_background(user_id: int, kind: str, symbol: str | None = None) -> bool:
    """بدون کش و درست پیش از send؛ خطای سیاست = عدم ارسال."""
    try:
        return await db.can_receive_background_notification(user_id, kind, symbol)
    except aiosqlite.Error:
        logger.exception("خواندن دسترسی اعلان %s برای %s ناموفق بود؛ پیام ارسال نمی‌شود.", kind, user_id)
        return False


async def scan_job(app: Application):
    logger.info("شروع اسکن خودکار واچ‌لیست‌ها...")
    pairs = await db.get_all_active_watch_pairs()  # [(user_id, symbol), ...]
    if not pairs:
        return
    allowed = await _background_recipients([uid for uid, _ in pairs], "scan")
    pairs = [(uid, symbol) for uid, symbol in pairs if uid in allowed]
    if not pairs:
        return

    symbol_to_users = defaultdict(list)
    for user_id, symbol in pairs:
        symbol_to_users[symbol].append(user_id)

    client = ExchangeClient()
    semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def _scan_symbol(symbol, user_ids):
        async with semaphore:
            try:
                result = await analyze_symbol(client, symbol)
            except Exception as e:
                logger.warning(f"خطا در تحلیل {symbol}: {e}")
                return

        last = await db.get_last_signal(
            symbol, source=db.SIGNAL_SOURCE_MULTI_TIMEFRAME
        )
        should_notify = (
            result.signal_type != "NEUTRAL"
            and (last is None or last["signal_type"] != result.signal_type)
        )

        await db.log_signal(
            symbol, result.signal_type, result.total_score, result.current_price,
            source=db.SIGNAL_SOURCE_MULTI_TIMEFRAME,
        )

        if not should_notify:
            return

        emoji = SIGNAL_EMOJI[result.signal_type]
        text = (
            f"{emoji} *هشدار سیگنال جدید*\n\n"
            f"نماد: `{symbol}`\n"
            f"قیمت: `{result.current_price:,.4f}`\n"
            f"سیگنال: *{SIGNAL_FA[result.signal_type]}*\n"
            f"امتیاز: `{result.total_score}`/`{result.max_possible_score}`\n\n"
            f"⚠️ توصیه مالی نیست."
        )
        for user_id in user_ids:
            if not await _can_send_background(user_id, "scan", symbol):
                continue
            try:
                await app.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.warning(f"ارسال پیام به {user_id} ناموفق بود: {e}")

    try:
        # چند نماد هم‌زمان (با سقف) به‌جای پشت‌سرهم؛ خطای یک نماد بقیه رو متوقف نمی‌کنه
        outcomes = await asyncio.gather(
            *(_scan_symbol(symbol, user_ids) for symbol, user_ids in symbol_to_users.items()),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.warning("خطای پیش‌بینی‌نشده در اسکن یک نماد: %s", outcome)
    finally:
        await client.close()
    logger.info("اسکن خودکار تمام شد.")


async def check_signal_performance_job(app: Application):
    """
    هر سیگنال بازی که از /signal صادر شده رو با قیمت لحظه‌ای مقایسه
    می‌کنه؛ اگه به سطح جدیدی (TP1/TP2/TP3/SL) نسبت به آخرین وضعیت
    ثبت‌شده رسیده باشه، وضعیتش رو به‌روز و به کاربر خبر می‌ده. فقط
    TP3، SL و BREAKEVEN نهایی‌ان (سیگنال می‌بنده)؛ TP1/TP2 سیگنال رو
    باز نگه می‌دارن چون ممکنه به سطح بعدی هم برسه.

    بعد از رسیدن به TP1 یا عبور مستقیم به TP2، اگه BREAKEVEN_AFTER_TP1
    فعال باشه و SL هنوز سربه‌سر نشده باشه، حد ضرر ثبت‌شده در پایش به
    نقطه‌ی ورود منتقل می‌شه (نه سفارش واقعی صرافی)؛ اگه بعداً به
    همون SL جدید (=entry) برخورد کنه، به‌جای SL_HIT، BREAKEVEN_HIT ثبت
    می‌شه (نه برد نه باخت).

    ثبت‌های جدید تا مشاهده‌ی محدوده‌ی ورود WAITING_ENTRY می‌مانند؛
    لمس SL پیش از ورود، ابطال مدل است نه باخت. قیمت ticker صرفاً یک
    مشاهده است و لمس‌های بین دو نوبت پایش یا اجرای سفارش را اثبات نمی‌کند.

    گذار فعال بر اساس رکورد تازه در تراکنش است؛ status/SL/closed_at با
    هم ثبت می‌شوند. خطای SQLite در یک گذار فعال، بقیه را متوقف نمی‌کند.
    اعلان بعد از commit است و تحویل یا ارسال مجدد آن تضمین نمی‌شود.
    محدودیت دسترسی، اعلان را می‌بندد نه پیشرفت سوابق پایش را؛ دسترسی
    بعد از ثبت گذار و درست پیش از ارسال دوباره بررسی می‌شود.
    هر نوبت فقط یک دسته‌ی چرخشی رزرو می‌شود؛ نشانگر پیش از شبکه ثبت
    می‌شود و موفقیت دریافت قیمت را اثبات نمی‌کند. سقف چرخه ثابت است.

    فاز ۳ (engine_ohlc_tracking=1): به‌جای یک قیمت لحظه‌ای، کندل‌های بسته‌شده‌ی
    ۵ دقیقه‌ای از آخرین نشانگر (monitor_cursor_ms) بررسی می‌شن تا لمس‌های بین
    دو نوبت جا نیفتن؛ رکوردهای قدیمی از همین لحظه وارد این مسیر می‌شن و اگه
    دریافت کندل ناموفق باشه، همون مسیر قیمت لحظه‌ای بالا اجرا می‌شه.
    """
    from config import MAX_OPEN_SIGNALS_PER_CHECK, BREAKEVEN_AFTER_TP1

    logger.info("شروع پایش عملکرد سیگنال‌های باز...")
    try:
        open_signals = await db.claim_signal_performance_batch(limit=MAX_OPEN_SIGNALS_PER_CHECK)
    except (aiosqlite.Error, ValueError):
        logger.exception("انتخاب دسته‌ی پایش ناموفق بود؛ این نوبت بدون دریافت قیمت متوقف می‌شود.")
        return
    if not open_signals:
        return

    from engine_config import get_engine_config
    engine_cfg = await get_engine_config()

    client = ExchangeClient()
    semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)
    now_ms = int(time.time() * 1000)

    # --- فاز ۳: پایش کندلی (سقف/کف کندل‌های ۵ دقیقه‌ای از آخرین بررسی) ---
    cursors = {}
    candle_signals, ticker_signals = [], []
    if engine_cfg.ohlc_tracking:
        try:
            cursors = await db.get_monitor_cursors([sig["id"] for sig in open_signals])
        except aiosqlite.Error:
            logger.exception("خواندن نشانگر پایش کندلی ناموفق بود؛ این نوبت با قیمت لحظه‌ای.")
            cursors = {}
        for sig in open_signals:
            cursor = cursors.get(sig["id"])
            if cursor is None:
                # رکورد قدیمی (قبل از فاز ۳): از همین الان کندلی پایش می‌شه؛ این نوبت با تیکر
                try:
                    await db.set_monitor_cursor(sig["id"], now_ms)
                except aiosqlite.Error:
                    pass
                ticker_signals.append(sig)
            else:
                candle_signals.append(sig)
    else:
        ticker_signals = list(open_signals)

    candles_by_symbol = {}
    if candle_signals:
        since_by_symbol = {}
        for sig in candle_signals:
            c = cursors[sig["id"]]
            since_by_symbol[sig["symbol"]] = min(c, since_by_symbol.get(sig["symbol"], c))

        async def _fetch_candles(symbol, since_ms):
            async with semaphore:
                try:
                    candles_by_symbol[symbol] = await _fetch_closed_monitor_candles(client, symbol, since_ms, now_ms)
                except Exception as e:
                    logger.warning(f"دریافت کندل‌های پایش {symbol} ناموفق بود (برگشت به قیمت لحظه‌ای): {e}")

        await asyncio.gather(*(_fetch_candles(sym, since) for sym, since in since_by_symbol.items()))
        for sig in list(candle_signals):
            if sig["symbol"] not in candles_by_symbol:
                candle_signals.remove(sig)
                ticker_signals.append(sig)

    prices = {}
    if ticker_signals:
        async def _fetch_price(symbol):
            async with semaphore:
                try:
                    prices[symbol] = await client.fetch_ticker_price(symbol)
                except Exception as e:
                    logger.warning(f"دریافت قیمت {symbol} ناموفق بود: {e}")

        await asyncio.gather(*(_fetch_price(symbol) for symbol in {s["symbol"] for s in ticker_signals}))
    await client.close()

    for sig in candle_signals:
        try:
            await _track_signal_with_candles(app, sig, candles_by_symbol[sig["symbol"]],
                                             cursors[sig["id"]], BREAKEVEN_AFTER_TP1)
        except aiosqlite.Error:
            logger.exception("پایش کندلی سیگنال %s ناموفق بود؛ سایر سیگنال‌ها ادامه دارند.", sig["id"])

    for sig in ticker_signals:
        price = prices.get(sig["symbol"])
        if not db.is_valid_price(price):
            continue

        if sig["status"] == "WAITING_ENTRY":
            if await db.invalidate_waiting_entry(sig["id"], price):
                entry_note = (
                    "سیگنال پیش از ورود فرضی باطل شد؛ قیمت مشاهده‌شده به SL رسیده یا از آن عبور کرده است.\n"
                    "این وضعیت برد یا باخت معامله نیست؛ مسیر قیمت بین دو مشاهده معلوم نیست."
                )
            elif await db.activate_signal_entry(sig["id"], price):
                entry_note = ENTRY_ACTIVATED_NOTE
            else:
                continue
            await _notify_entry(app, sig, entry_note, price)
            continue  # TP/SL فقط در مشاهده‌های بعد از فعال‌سازی پایش می‌شوند.

        try:
            committed = await db.advance_signal_performance(
                sig["id"], price, BREAKEVEN_AFTER_TP1
            )
        except aiosqlite.Error:
            logger.exception("ثبت گذار پایش سیگنال %s ناموفق بود؛ سایر سیگنال‌ها ادامه دارند.", sig["id"])
            continue
        if committed is None:
            continue
        await _notify_transition(app, committed, price)

    logger.info("پایش عملکرد سیگنال‌ها تمام شد.")


# ==================== فاز ۳: پایش کندلی ====================

MONITOR_TIMEFRAME = "5m"
MONITOR_TF_MS = 5 * 60 * 1000
MONITOR_MAX_CANDLES = 1000
TERMINAL_STATUSES = ("TP3_HIT", "SL_HIT", "BREAKEVEN_HIT", "ENTRY_INVALIDATED")

ENTRY_ACTIVATED_NOTE = (
    "ورود فرضی در مدل پایش فعال شد؛ این پیام تأیید اجرای سفارش واقعی نیست.\n"
    "Entry برنامه‌ریزی‌شده تغییر نکرده و قیمت مشاهده‌شده جدا ثبت شده است."
)


async def _fetch_closed_monitor_candles(client, symbol: str, since_ms: int, now_ms: int) -> list:
    """کندل‌های بسته‌شده‌ی ۵ دقیقه‌ای از since_ms: [(open_ms, o, h, l, c), ...]"""
    from market_data_validation import ohlcv_frame_from_raw
    raw = await client.exchange.fetch_ohlcv(symbol, timeframe=MONITOR_TIMEFRAME, since=int(since_ms),
                                            limit=MONITOR_MAX_CANDLES)
    df = ohlcv_frame_from_raw(raw, limit=MONITOR_MAX_CANDLES)  # اعتبارسنجی سخت‌گیرانه (همون مسیر تحلیل)
    out = []
    for ts, o, h, l, c in zip(df["timestamp"], df["open"], df["high"], df["low"], df["close"]):
        open_ms = int(ts.value // 1_000_000)
        if open_ms + MONITOR_TF_MS <= now_ms:
            out.append((open_ms, float(o), float(h), float(l), float(c)))
    return out


async def _notify_entry(app, sig: dict, entry_note: str, price: float):
    if not await _can_send_background(sig["user_id"], "performance"):
        return
    try:
        await app.bot.send_message(
            chat_id=sig["user_id"],
            text=(
                f"📌 *پایش سیگنال {sig['symbol']}* ({sig['timeframe']})\n"
                f"{entry_note}\n"
                f"قیمت مشاهده‌شده: `{price:,.4f}`\n"
                f"Entry برنامه‌ریزی‌شده: `{sig['entry']:,.4f}`\n\n"
                "برای مشاهده وضعیت: /mysignals و /mystats"
            ),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.warning(f"ارسال وضعیت ورود به {sig['user_id']} ناموفق بود: {e}")


async def _notify_transition(app, sig: dict, price: float):
    candidate_status = sig["status"]
    breakeven_note = ""
    if sig["breakeven_changed"]:
        breakeven_note = "\n\n🛡 حد ضرر ثبت‌شده در پایش به نقطه‌ی سربه‌سر (Break-even) منتقل شد؛ این تغییر، سفارش واقعی صرافی را تغییر نمی‌دهد."

    emoji = {"TP1_HIT": "🎯", "TP2_HIT": "🎯", "TP3_HIT": "🎯",
             "SL_HIT": "🛑", "BREAKEVEN_HIT": "🛡"}[candidate_status]
    status_fa = {
        "TP1_HIT": "به TP1 رسید", "TP2_HIT": "به TP2 رسید",
        "TP3_HIT": "به TP3 رسید (سیگنال بسته شد ✅)",
        "SL_HIT": "به حد ضرر خورد (سیگنال بسته شد ❌)",
        "BREAKEVEN_HIT": "به حد ضرر سربه‌سرِ مدل پایش برگشت (بسته شد ⚪️؛ کارمزد/لغزش محاسبه نشده)",
    }[candidate_status]
    if not await _can_send_background(sig["user_id"], "performance"):
        return
    try:
        await app.bot.send_message(
            chat_id=sig["user_id"],
            text=(
                f"{emoji} *بروزرسانی سیگنال {sig['symbol']}* ({sig['timeframe']})\n"
                f"وضعیت: {status_fa}\n"
                f"قیمت مشاهده‌شده: `{price:,.4f}`\n"
                f"ورود: `{sig['entry']:,.4f}`"
                f"{breakeven_note}\n\n"
                f"برای دیدن آمار کلی: /mystats"
            ),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.warning(f"ارسال بروزرسانی عملکرد به {sig['user_id']} ناموفق بود: {e}")


async def _track_signal_with_candles(app, sig: dict, candles: list, cursor_ms: int,
                                     breakeven_enabled: bool):
    """
    کندل‌به‌کندل از آخرین نشانگر جلو می‌ره (هم‌سان با بک‌تست live_model):
    - در انتظار ورود: گپ open به آن‌سوی SL = ابطال؛ لمس Entry = ورود با
      min(open, entry) (برای SELL: max)؛ توی همون کندل فقط SL چک می‌شه
    - فعال: اول بدترین قیمت کندل (SL/سربه‌سر)، بعد بهترین (TP1/TP2/TP3)
    گذارها از همون توابع اتمیک دیتابیس (activate/advance) ثبت می‌شن.
    """
    is_buy = sig["direction"] == "BUY"
    status = sig["status"]
    new_cursor = cursor_ms
    for open_ms, o, h, l, c in candles:
        if open_ms < cursor_ms:
            continue
        new_cursor = open_ms + MONITOR_TF_MS
        adverse = l if is_buy else h
        favorable = h if is_buy else l

        if status == "WAITING_ENTRY":
            sl, entry = sig["sl"], sig["entry"]
            if (is_buy and o <= sl) or (not is_buy and o >= sl):
                if await db.invalidate_waiting_entry(sig["id"], o):
                    await _notify_entry(app, sig, (
                        "سیگنال پیش از ورود فرضی باطل شد؛ قیمت با گپ از SL عبور کرد.\n"
                        "این وضعیت برد یا باخت معامله نیست."), o)
                    status = "ENTRY_INVALIDATED"
                break
            if not ((is_buy and l <= entry) or (not is_buy and h >= entry)):
                continue
            fill = min(o, entry) if is_buy else max(o, entry)
            if not await db.activate_signal_entry(sig["id"], fill):
                break  # وضعیت عوض شده (مثلاً هم‌زمان)؛ دست نمی‌زنیم
            await _notify_entry(app, sig, ENTRY_ACTIVATED_NOTE, fill)
            status = "OPEN"
            # کندل ورود: ترتیب داخل کندل معلوم نیست؛ فقط SL (محافظه‌کارانه)
            if (is_buy and adverse <= sl) or (not is_buy and adverse >= sl):
                committed = await db.advance_signal_performance(sig["id"], adverse, breakeven_enabled)
                if committed:
                    await _notify_transition(app, committed, adverse)
                    status = committed["status"]
                    if status in TERMINAL_STATUSES:
                        break
            continue

        for price in (adverse, favorable):
            committed = await db.advance_signal_performance(sig["id"], price, breakeven_enabled)
            if committed:
                await _notify_transition(app, committed, price)
                status = committed["status"]
                if status in TERMINAL_STATUSES:
                    break
        if status in TERMINAL_STATUSES:
            break

    if new_cursor != cursor_ms and status not in TERMINAL_STATUSES:
        await db.set_monitor_cursor(sig["id"], new_cursor)


async def news_auto_job(app: Application):
    """Up to three unseen high-impact items per user, with durable owned claims."""
    try:
        import news as news_module
        user_ids = await db.get_users_with_news_auto()
        allowed = await _background_recipients(user_ids, "news")
        user_ids = [uid for uid in user_ids if uid in allowed]
        if not user_ids:
            return
        items = await news_module.get_combined_news(crypto_limit=6, forex_limit=4)
        candidates = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            impact = item.get("impact")
            if (type(impact) not in (int, float) or not 80 <= impact <= 100
                    or not math.isfinite(impact)):
                continue
            key = news_item_key(item)
            if key is not None:
                candidates.setdefault(key, item)
        if not candidates:
            return
        header = "🚨 *خبر یا رویداد مهم بازار — تأثیر تخمینی بالا*\n\n"
        for uid in user_ids:
            claim = None
            telegram_succeeded = False
            try:
                claim = await db.claim_news_delivery(uid, list(candidates), limit=3)
                if claim is None:
                    continue
                selected = [candidates[key] for key in claim["news_keys"]]
                full = header + news_module.format_news_message(selected, max_items=3)
                if not await db.is_news_delivery_claim_current(uid, claim["token"], claim["news_keys"]):
                    continue
                if not await _can_send_background(uid, "news"):
                    continue
                await asyncio.wait_for(
                    app.bot.send_message(chat_id=uid, text=full, parse_mode="Markdown",
                                         disable_web_page_preview=True),
                    timeout=NEWS_SEND_TIMEOUT_SECONDS,
                )
                telegram_succeeded = True
                if not await db.mark_news_delivery_sent(uid, claim["token"], claim["news_keys"]):
                    logger.warning("خبر به %s ارسال شد، اما مالکیت ثبت موفقیت دیگر معتبر نیست.", uid)
            except Exception as e:
                if telegram_succeeded:
                    logger.warning("ارسال خبر به %s موفق بود، اما ثبت DB ناموفق/نامعلوم است: %s", uid, e)
                else:
                    logger.warning("تلاش ارسال خبر خودکار به %s ناموفق: %s", uid, e)
            finally:
                # Do not immediately release a possibly delivered message on DB failure.
                if claim is not None and not telegram_succeeded:
                    try:
                        await db.release_news_delivery_claim(uid, claim["token"], claim["news_keys"])
                    except aiosqlite.Error:
                        logger.exception("آزادسازی رزرو خبر %s ناموفق بود؛ انقضای رزرو امکان retry می‌دهد.", uid)
    except Exception as e:
        logger.warning(f"news_auto_job failed: {e}")


async def backup_job(app: Application):
    """
    بکاپ خودکار دوره‌ای دیتابیس. اگه موفق بود چیزی به کسی اطلاع
    نمی‌ده (تا اسپم نشه)؛ ولی اگه شکست بخوره، به همه‌ی ادمین‌ها خبر
    می‌ده - چون شکست مکرر بکاپ یعنی موقع نیاز واقعی (خرابی دیسک، حذف
    اشتباهی و...) داده‌ای برای بازگردانی وجود نداره.
    """
    from config import ADMIN_IDS

    try:
        path = await backup.create_backup()
        logger.info(f"بکاپ خودکار دوره‌ای موفق: {path}")
    except Exception as e:
        logger.error(f"بکاپ خودکار دوره‌ای ناموفق بود: {e}")
        for admin_id in ADMIN_IDS:
            if not await _can_send_background(admin_id, "backup"):
                continue
            try:
                await app.bot.send_message(
                    chat_id=admin_id,
                    text=f"⚠️ بکاپ خودکار دیتابیس ناموفق بود:\n`{e}`\n\nلطفاً وضعیت دیسک/فضای ذخیره‌سازی رو چک کن.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass
