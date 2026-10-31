from datetime import date, datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
import database as db
import news as news_module


def _today():
    return datetime.now(news_module.TEHRAN_TZ).date()


def _news_keyboard(day=None):
    rows = [[InlineKeyboardButton("📰 اخبار بازار", callback_data="news:refresh"),
             InlineKeyboardButton("📅 تقویم اقتصادی", callback_data="news:calendar")]]
    if day is not None:
        today = _today(); navigation=[]
        if day > today:
            navigation.append(InlineKeyboardButton("روز قبل",callback_data="news:calendar:"+(day-timedelta(days=1)).isoformat()))
        navigation.append(InlineKeyboardButton("امروز",callback_data="news:calendar"))
        if day < today+timedelta(days=6):
            navigation.append(InlineKeyboardButton("روز بعد",callback_data="news:calendar:"+(day+timedelta(days=1)).isoformat()))
        rows.append(navigation)
        rows.append([InlineKeyboardButton("🔄 به‌روزرسانی همین روز",callback_data="news:calendar-refresh:"+day.isoformat())])
    else:
        rows.append([InlineKeyboardButton("🔄 به‌روزرسانی اخبار",callback_data="news:refresh")])
    rows.append([InlineKeyboardButton("⚙️ تنظیمات اخبار",callback_data="news:settings")])
    return InlineKeyboardMarkup(rows)


def _settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("تغییر نمایش زیر سیگنال",callback_data="news:toggle_enabled")],
        [InlineKeyboardButton("تغییر ارسال خودکار",callback_data="news:toggle_auto")],
        [InlineKeyboardButton("📰 اخبار بازار",callback_data="news:refresh"),
         InlineKeyboardButton("📅 تقویم اقتصادی",callback_data="news:calendar")],
    ])


async def _settings_text(user_id):
    enabled = await db.get_user_news_enabled(user_id)
    auto = await db.get_user_news_auto(user_id)
    status = "فعال ✅" if enabled else "غیرفعال ❌"
    auto_status = "فعال ✅" if auto else "غیرفعال ❌"
    return (f"⚙️ *تنظیمات اخبار*\n\n"
            f"📰 نمایش اخبار و رویدادها زیر سیگنال‌ها: *{status}*\n"
            f"🔔 ارسال خودکار موارد مهم: *{auto_status}*\n\n"
            "• ارسال خودکار بر اساس امتیاز خبری تخمینی ۸۰ به بالا است.\n"
            "• امتیاز خبر احتمال رشد/افت یا میزان تغییر قیمت نیست.")


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/news is one entry with separate market/calendar views."""
    await update.message.reply_text("⏳ در حال دریافت اخبار بازار...")
    try:
        items = await news_module.fetch_crypto_news(limit=8, force=True)
        await update.message.reply_text(news_module.format_news_message(items,max_items=8),
            parse_mode=ParseMode.MARKDOWN,reply_markup=_news_keyboard(),disable_web_page_preview=True)
    except Exception:
        await update.message.reply_text("❌ دریافت یا نمایش اخبار ناموفق بود؛ کمی بعد دوباره تلاش کن.",reply_markup=_news_keyboard())


async def news_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await _settings_text(update.effective_user.id),
        parse_mode=ParseMode.MARKDOWN,reply_markup=_settings_keyboard())


async def handle_news_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if data == "news:refresh":
        await query.edit_message_text("⏳ در حال دریافت اخبار بازار...")
        try:
            items=await news_module.fetch_crypto_news(limit=8,force=True)
            await query.edit_message_text(news_module.format_news_message(items,max_items=8),
                parse_mode=ParseMode.MARKDOWN,reply_markup=_news_keyboard(),disable_web_page_preview=True)
        except Exception:
            await query.edit_message_text("❌ دریافت یا نمایش اخبار ناموفق بود؛ دوباره تلاش کن.",reply_markup=_news_keyboard())
    elif data == "news:calendar" or data.startswith(("news:calendar:", "news:calendar-refresh:")):
        today=_today();selected=today
        if data != "news:calendar":
            try:
                raw=data.split(":",maxsplit=2)[2]
                selected=date.fromisoformat(raw)
                if raw!=selected.isoformat() or not today<=selected<=today+timedelta(days=6):
                    raise ValueError("invalid calendar day")
            except (ValueError,TypeError):
                await query.edit_message_text("تاریخ این دکمه معتبر یا به‌روز نیست؛ تقویم را دوباره باز کن.",reply_markup=_news_keyboard())
                return
        await query.edit_message_text("⏳ در حال دریافت تقویم اقتصادی...")
        try:
            items=await news_module.fetch_forex_calendar(
                limit=None,force=data.startswith("news:calendar-refresh:"),day=selected)
            await query.edit_message_text(news_module.format_calendar_message(items,max_items=12,day=selected),
                parse_mode=ParseMode.MARKDOWN,reply_markup=_news_keyboard(selected),disable_web_page_preview=True)
        except Exception:
            await query.edit_message_text("❌ دریافت یا نمایش تقویم ناموفق بود؛ دوباره تلاش کن.",reply_markup=_news_keyboard(selected))
    elif data == "news:settings":
        await query.edit_message_text(await _settings_text(user_id),parse_mode=ParseMode.MARKDOWN,reply_markup=_settings_keyboard())
    elif data in ("news:toggle_enabled","news:toggle_auto"):
        if data == "news:toggle_enabled":
            current=await db.get_user_news_enabled(user_id)
            await db.set_user_news_enabled(user_id,not current)
        else:
            current=await db.get_user_news_auto(user_id)
            await db.set_user_news_auto(user_id,not current)
        await query.edit_message_text(await _settings_text(user_id),parse_mode=ParseMode.MARKDOWN,reply_markup=_settings_keyboard())
