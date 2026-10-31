from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import MAX_WATCHLIST_PER_USER
import database as db
from handlers.analysis import (
    run_single_timeframe_signal, _get_price_text, _timeframe_keyboard
)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    مسیریابی کلیک روی دکمه‌های شیشه‌ای
    فرمت callback_data: "action:symbol" یا "action:symbol:timeframe"
    """
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[0]

    # نتیجه‌ی پیگیری را خود handler فقط یک‌بار پاسخ می‌دهد.
    if action in ("track", "notrack"):
        try:
            if len(parts) != 2:
                raise ValueError("Invalid tracking callback")
            pending_id = int(parts[1])
            if not 0 < pending_id <= 9223372036854775807:
                raise ValueError("Invalid pending id")
        except ValueError:
            await query.answer("درخواست پیگیری نامعتبر است.", show_alert=True)
            return
        await _handle_track_decision(query, pending_id, confirm=action == "track")
        return

    await query.answer()

    if action == "tf" and len(parts) == 3:
        _, symbol, timeframe = parts
        user_id = query.from_user.id
        await query.edit_message_text(f"⏳ در حال تحلیل {symbol} روی تایم‌فریم {timeframe}...")
        try:
            text, chart_buf, keyboard = await run_single_timeframe_signal(symbol, timeframe, user_id=user_id)
        except Exception as e:
            await query.edit_message_text(f"❌ خطا در تحلیل: {e}")
            return
        if text is None:
            await query.edit_message_text(f"❌ نماد `{symbol}` پیدا نشد.", parse_mode=ParseMode.MARKDOWN)
            return
        if chart_buf is None:
            # حالت «داده‌ی تاریخی ناکافی» - فقط متن هشدار، بدون نمودار
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_timeframe_keyboard(symbol))
            return
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.message.reply_photo(photo=chart_buf)

    elif action == "chtf" and len(parts) == 2:
        symbol = parts[1]
        await query.edit_message_text(
            f"⏱ برای «{symbol}» کدوم تایم‌فریم رو تحلیل کنم؟",
            reply_markup=_timeframe_keyboard(symbol)
        )

    elif action == "price" and len(parts) == 2:
        symbol = parts[1]
        text = await _get_price_text(symbol)
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    elif action == "watch" and len(parts) == 2:
        symbol = parts[1]
        user_id = query.from_user.id
        max_watchlist = await db.get_int_setting("max_watchlist_per_user", MAX_WATCHLIST_PER_USER)
        count = await db.get_watchlist_count(user_id)
        if count >= max_watchlist:
            await query.message.reply_text(f"❌ حداکثر {max_watchlist} نماد می‌تونی واچ کنی.")
            return
        added = await db.add_to_watchlist(user_id, symbol)
        if added:
            await query.message.reply_text(f"✅ `{symbol}` به واچ‌لیست اضافه شد.", parse_mode=ParseMode.MARKDOWN)
        else:
            await query.message.reply_text(f"ℹ️ `{symbol}` از قبل توی واچ‌لیستت بود.", parse_mode=ParseMode.MARKDOWN)

    elif action == "delsig" and len(parts) == 2:
        perf_id = int(parts[1])
        user_id = query.from_user.id
        deleted = await db.delete_signal_performance(perf_id, user_id)
        if deleted:
            await query.edit_message_text("🗑 این سیگنال از لیست پایش حذف شد.")
        else:
            await query.edit_message_text("ℹ️ این سیگنال قبلاً حذف شده یا مال تو نیست.")

    elif action == "approve_access" and len(parts) == 2:
        await _handle_access_decision(query, context, int(parts[1]), approve=True)

    elif action == "reject_access" and len(parts) == 2:
        await _handle_access_decision(query, context, int(parts[1]), approve=False)

    elif action == "applycal" and len(parts) == 3:
        await _handle_apply_calibration(query, parts[1], parts[2])


async def _handle_access_decision(query, context, target_user_id: int, approve: bool):
    """
    ادمین با دکمه‌ی زیر نوتیفیکیشنِ درخواست دسترسی، تایید/رد کرده.
    توجه: query.answer() همین الان توی callback_router بالاتر صدا زده
    شده (تلگرام هر callback رو فقط یه‌بار میشه answer کرد)، پس اینجا
    دوباره answer نمی‌زنیم و فقط از edit/send_message برای فیدبک استفاده می‌کنیم.
    """
    from config import ADMIN_IDS
    if query.from_user.id not in ADMIN_IDS:
        return

    await db.delete_access_request(target_user_id)

    if approve:
        await db.add_to_whitelist(target_user_id, "تایید شده از دکمه‌ی نوتیفیکیشن")
        try:
            await query.edit_message_text(query.message.text + "\n\n✅ تایید شد.")
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text="✅ دسترسیت تایید شد! برای شروع دوباره /start رو بزن."
            )
        except Exception:
            pass
    else:
        try:
            await query.edit_message_text(query.message.text + "\n\n❌ رد شد.")
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text="متاسفانه درخواست دسترسیت در حال حاضر تایید نشد."
            )
        except Exception:
            pass


async def _handle_track_decision(query, pending_id: int, confirm: bool):
    """تصمیم اتمیک و پاسخ واحد؛ پاسخ تلگرام بعد از پایان تراکنش ارسال می‌شود."""
    import aiosqlite
    import logging
    try:
        result = await db.decide_pending_signal(pending_id, query.from_user.id, confirm)
    except aiosqlite.Error:
        logging.getLogger("handlers.callbacks").exception("خطای دیتابیس در تصمیم پیگیری %s", pending_id)
        await query.answer("خطای دیتابیس در ثبت تصمیم؛ کمی بعد دوباره تلاش کن.", show_alert=True)
        return

    outcome = result["outcome"]
    messages = {
        "accepted": "✅ در انتظار ورود فرضی ثبت شد، نه اجرای سفارش واقعی. وضعیت: /mystats و /mysignals",
        "declined": "باشه، این سیگنال پیگیری نمی‌شه.",
        "expired": "این درخواست منقضی شده (۲۴ ساعت از ثبت گذشته). دوباره سیگنال بگیر.",
        "invalid_timestamp": "زمان ثبت این درخواست معتبر نیست؛ دوباره سیگنال بگیر.",
        "missing": "این درخواست دیگر موجود نیست؛ ممکن است قبلاً رسیدگی یا حذف شده باشد.",
        "forbidden": "این دکمه مال تو نیست.",
        "invalid": "قیمت ورود، حد ضرر یا ترتیب تارگت‌های این سیگنال معتبر نیست؛ دوباره تحلیل بگیر.",
    }
    await query.answer(messages[outcome], show_alert=True)
    if outcome not in ("accepted", "declined", "expired", "invalid_timestamp"):
        return

    # ردیف دکمه‌های پیگیری رو از کیبورد پیام اصلی حذف می‌کنیم (تصمیم گرفته شده)
    try:
        current_markup = query.message.reply_markup
        if current_markup:
            new_rows = [
                row for row in current_markup.inline_keyboard
                if not any((btn.callback_data or "").startswith(("track:", "notrack:")) for btn in row)
            ]
            from telegram import InlineKeyboardMarkup
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows))
    except Exception:
        pass  # اگه پیام خیلی قدیمی بود و قابل ویرایش نبود، مهم نیست

async def _handle_apply_calibration(query, conf_str: str, atr_str: str):
    """
    ادمین روی دکمه‌ی «✅ اعمال» زیر گزارش کالیبراسیون زده.
    توجه: مثل _handle_access_decision، query.answer() همین الان توی
    callback_router بالاتر صدا زده شده، پس اینجا دوباره answer نمی‌زنیم.
    """
    from config import ADMIN_IDS
    if query.from_user.id not in ADMIN_IDS:
        return

    try:
        confidence_threshold_fraction = float(conf_str)
        atr_sl_mult = float(atr_str)
    except ValueError:
        return

    await db.set_setting("confidence_threshold_fraction", confidence_threshold_fraction)
    await db.set_setting("atr_sl_mult", atr_sl_mult)

    confirmation = (
        "\n\n✅ اعمال شد: آستانه اطمینان=" + f"{confidence_threshold_fraction:.2f}" +
        "، ضریب ATR (SL)=" + f"{atr_sl_mult:.1f}"
    )
    try:
        await query.edit_message_text(query.message.text + confirmation)
    except Exception:
        pass
