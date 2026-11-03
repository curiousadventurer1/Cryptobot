"""
منوی دکمه‌ای (شیشه‌ای) ربات.

/start یا /menu یا دکمه‌ی «📋 منو» پایین صفحه، منوی اصلی رو نشون می‌ده.
با زدن هر دکمه، توضیح اون قابلیت بالای همون منو میاد و کارش هم همون لحظه
انجام می‌شه (نتیجه به‌صورت پیام جدید زیر منو ارسال می‌شه). قابلیت‌هایی که
ورودی لازم دارن (نماد، عدد، متن) بعد از توضیح، دکمه‌های انتخاب یا درخواست
تایپ نشون می‌دن. دستورهای متنی قبلی (/signal BTCUSDT و ...) دست‌نخورده‌ان.

خود کار هر دکمه با همون هندلر دستور متنی انجام می‌شه (با یه Update
جایگزین که پاسخ‌ها رو به همین چت می‌فرسته)، پس رفتار، اعتبارسنجی و
محدودیت دسترسی دقیقاً مثل دستور تایپ‌شده‌ست.
callback_data همه‌ی دکمه‌ها با «m:» شروع می‌شه.
"""
import logging
import time

from telegram import (
    Update, Message, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import database as db
from config import ADMIN_IDS

from handlers.analysis import signal_command, chart_command, price_command, top_command, gainers_command
from handlers.watchlist import watch_command, unwatch_command, mywatchlist_command, autoscan_command
from handlers.risk import setrisk_command, myrisk_command, mystats_command, mysignals_command, myanalytics_command
from handlers.backtest import backtest_command, calibrate_command
from handlers.news import news_command, news_settings_command
from handlers.admin import (
    admin_help_command, block_command, unblock_command, blocklist_command,
    stats_command, broadcast_command, users_command, finduser_command,
    whitelist_add_command, whitelist_remove_command, whitelist_list_command,
    accessmode_command, setwelcome_command, removewelcome_command, getwelcome_command,
    botanalytics_command, backup_command, backuplist_command, USERS_PAGE_SIZE,
)

logger = logging.getLogger(__name__)

MENU_BUTTON_TEXT = "📋 منو"
QUICK_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]
INPUT_TIMEOUT_SECONDS = 10 * 60
_STATE_KEY = "menu_input"
_BROADCAST_KEY = "menu_broadcast"

MENU_HEADER = (
    "📋 *منوی اصلی*\n\n"
    "روی هر دکمه بزنی، توضیحش همین‌جا میاد و کارش هم انجام می‌شه 👇"
)
ADMIN_HEADER = (
    "🛠 *پنل ادمین*\n\n"
    "روی هر دکمه بزنی، توضیحش همین‌جا میاد و کارش هم انجام می‌شه 👇"
)

# ==================== برچسب‌ها و توضیح‌ها ====================

LABELS = {
    # کاربر
    "signal": "📊 سیگنال", "backtest": "🧪 بک‌تست",
    "chart": "📈 نمودار", "price": "💰 قیمت",
    "gainers": "🚀 بیشترین رشد/افت", "top": "🔝 ارزهای پرطرفدار",
    "watchlist": "⭐ واچ‌لیست من", "autoscan": "🔔 اسکن خودکار",
    "mysignals": "📌 سیگنال‌های من", "mystats": "🏆 آمار من",
    "myanalytics": "🔬 تحلیل من", "risk": "⚖️ تنظیم ریسک",
    "news": "📰 اخبار", "newssettings": "⚙️ تنظیمات اخبار",
    "help": "❓ راهنما", "admin": "🛠 پنل ادمین",
    "watch": "➕ افزودن به واچ‌لیست", "unwatch": "➖ حذف از واچ‌لیست",
    "setrisk": "✏️ تنظیم ریسک جدید",
    # ادمین
    "a_stats": "📊 آمار ربات", "a_botanalytics": "🔬 تحلیل کل ربات",
    "a_users": "👥 کاربران", "a_finduser": "🔎 جستجوی کاربر",
    "a_block": "🚫 مسدود کردن", "a_unblock": "✅ رفع مسدودی",
    "a_blocklist": "📋 لیست مسدودها", "a_accessmode": "🔐 حالت دسترسی",
    "a_wladd": "➕ افزودن به لیست سفید", "a_wlremove": "➖ حذف از لیست سفید",
    "a_whitelist": "📜 لیست سفید", "a_broadcast": "📢 پیام همگانی",
    "a_backup": "💾 بکاپ فوری", "a_backuplist": "🗂 لیست بکاپ‌ها",
    "a_welcome": "👋 پیام خوش‌آمد", "a_calibrate": "🎛 کالیبراسیون",
    "a_help": "📖 راهنمای ادمین", "a_setwelcome": "✏️ تنظیم پیام خوش‌آمد",
}

DESCRIPTIONS = {
    "signal": (
        "تحلیل کامل یک ارز روی تایم‌فریمی که انتخاب می‌کنی: سیگنال خرید/فروش، درصد اطمینان، "
        "نقطه‌ی ورود، حد ضرر (SL) و سه تارگت سود (TP) به‌همراه نمودار چند اندیکاتوره "
        "(EMA، SMA، RSI، MACD، فیبوناچی). سیگنال با روند تایم‌فریم بالاتر مقایسه می‌شه، "
        "واگرایی RSI و همبستگی با بیت‌کوین و فشار اردربوک چک می‌شه، رژیم نوسان بازار روی "
        "حجم پوزیشن پیشنهادی اثر می‌ذاره و خلاصه‌ی فاندامنتال هم پایین پیام میاد.\n"
        "👇 ارز رو انتخاب کن (بعدش تایم‌فریم رو می‌پرسه)."
    ),
    "backtest": (
        "همین موتور سیگنال رو روی داده‌ی تاریخی شبیه‌سازی می‌کنه و آمار عملکرد (نرخ برد، "
        "میانگین R، Profit Factor، بیشترین باخت پیاپی) + نمودار منحنی تجمعی R می‌ده؛ "
        "بعد از کسر کارمزد و لغزش و با مدل پایش زنده.\n"
        "با دکمه، تایم‌فریم ۱ساعته و ۳۰۰ کندل بررسی می‌شه. برای تنظیمات بیشتر تایپ کن: "
        "`/backtest BTCUSDT 4h 400 wf` (`wf` = بک‌تست پیشرو).\n"
        "👇 ارز رو انتخاب کن."
    ),
    "chart": (
        "فقط نمودار قیمت (بدون متن تحلیل) با EMA/SMA، حجم، RSI و MACD روی تایم‌فریم ۱ساعته.\n"
        "برای تایم‌فریم دیگه تایپ کن: `/chart BTCUSDT 4h`\n"
        "👇 ارز رو انتخاب کن."
    ),
    "price": "قیمت لحظه‌ای ارز.\n👇 ارز رو انتخاب کن.",
    "gainers": "بیشترین رشد و افت ۲۴ساعته‌ی بازار (فیلترشده بر اساس نقدینگی).",
    "top": "مرور سریع چند ارز پرطرفدار به‌صورت چندتایم‌فریمی (۱۵دقیقه/۱ساعته/۴ساعته) برای دید کلی بازار.",
    "watchlist": (
        "لیست ارزهایی که زیر نظر داری. اگه اسکن خودکار روشن باشه، هر وقت سیگنال یکی از "
        "این‌ها تغییر مهمی کنه، خودکار بهت خبر می‌دم.\n"
        "با دکمه‌های زیر می‌تونی ارز اضافه یا حذف کنی."
    ),
    "watch": "یه ارز به واچ‌لیستت اضافه می‌شه تا سیگنالش پس‌زمینه پایش بشه.\n👇 ارز رو انتخاب کن.",
    "unwatch": "یه ارز از واچ‌لیستت حذف می‌شه.\n👇 ارزی که می‌خوای حذف بشه رو انتخاب کن.",
    "autoscan": (
        "روشن/خاموش کردن اعلان خودکار: وقتی سیگنال یکی از ارزهای واچ‌لیستت تغییر مهمی کنه "
        "(اسکن چندتایم‌فریمی پس‌زمینه)، بهت پیام می‌دم."
    ),
    "mysignals": (
        "سیگنال‌هایی که تایید کردی پیگیری بشن (در انتظار ورود فرضی یا فعال در مدل پایش)؛ "
        "هرکدوم رو هر وقت خواستی می‌تونی از پایش حذف کنی.\n"
        "تایید پیگیری، اجرای سفارش نیست؛ ثبت‌های جدید تا رسیدن قیمت به Entry منتظر می‌مونن."
    ),
    "mystats": (
        "نرخ برد نهایی سیگنال‌های پیگیری‌شده (TP3 برد، SL باخت، سربه‌سر جدا)، انتظار ورود و "
        "وضعیت TP1/TP2. پایش با سقف/کف کندل‌های ۵ دقیقه‌ای انجام می‌شه."
    ),
    "myanalytics": (
        "تحلیل فراداده: از روی سیگنال‌های قبلی‌ت نشون می‌ده کدوم تایم‌فریم، نماد و جهت "
        "(خرید یا فروش) برات بهتر جواب داده — بدون نیاز به داده‌ی جدید."
    ),
    "risk": (
        "تنظیمات ریسک فعلی‌ت: موجودی و درصد ریسک هر معامله. با این‌ها زیر هر سیگنال، حجم "
        "پوزیشن پیشنهادی خودکار حساب و نمایش داده می‌شه (با در نظر گرفتن نوسان بازار)."
    ),
    "setrisk": (
        "موجودی و درصد ریسک هر معامله رو با یه فاصله بفرست.\n"
        "مثال: `1000 2` یعنی موجودی ۱۰۰۰ دلار و حداکثر ۲٪ ریسک هر معامله."
    ),
    "news": "اخبار مهم کریپتو و فارکس به فارسی؛ هر خبر با امتیاز اهمیت و خلاصه‌ی فارسی، به‌همراه تقویم اقتصادی.",
    "newssettings": "تنظیمات اخبار: نمایش اخبار زیر سیگنال‌ها و ارسال خودکار خبرهای خیلی مهم.",
    "help": "راهنمای کامل همه‌ی دستورهای متنی ربات.",
    # ادمین
    "a_stats": "آمار کلی ربات: تعداد کاربران، واچ‌لیست‌ها، سیگنال‌ها و ...",
    "a_botanalytics": (
        "تحلیل فراداده‌ی کل ربات: کدوم تایم‌فریم/نماد/جهت بین همه‌ی کاربران بهتر جواب داده "
        "(نمونه‌ی آماری بزرگ‌تر از «تحلیل من» شخصی)."
    ),
    "a_users": "لیست کاربرانی که ربات رو استارت کردن (جدیدترین اول). 🚫 = مسدود، ✅ = لیست سفید.",
    "a_finduser": "جزئیات کامل یه کاربر خاص.\n✍️ شناسه‌ی عددی کاربر رو بفرست.",
    "a_block": "مسدود کردن کاربر.\n✍️ شناسه‌ی عددی کاربر رو بفرست؛ می‌تونی بعدش دلیل هم بنویسی. مثال: `123456789 اسپم`",
    "a_unblock": "رفع مسدودی کاربر.\n✍️ شناسه‌ی عددی کاربر رو بفرست.",
    "a_blocklist": "لیست کاربران مسدودشده.",
    "a_accessmode": (
        "حالت دسترسی ربات:\n🌐 *باز*: همه می‌تونن استفاده کنن (مگر مسدودشده‌ها).\n"
        "🔒 *لیست سفید*: فقط کاربران تاییدشده و ادمین‌ها؛ بقیه پیام «منتظر تایید» می‌گیرن."
    ),
    "a_wladd": "افزودن کاربر به لیست سفید.\n✍️ شناسه‌ی عددی رو بفرست؛ می‌تونی بعدش یادداشت هم بنویسی.",
    "a_wlremove": "حذف کاربر از لیست سفید.\n✍️ شناسه‌ی عددی کاربر رو بفرست.",
    "a_whitelist": "نمایش لیست سفید.",
    "a_broadcast": "ارسال پیام به همه‌ی کاربران ربات.\n✍️ متن پیام رو بفرست؛ قبل از ارسال ازت تایید گرفته می‌شه.",
    "a_backup": "ساخت یه بکاپ فوری از دیتابیس و ارسال فایلش همین‌جا (علاوه بر بکاپ خودکار دوره‌ای).",
    "a_backuplist": "لیست بکاپ‌های موجود روی سرور.",
    "a_welcome": (
        "پیام خوش‌آمدگویی سفارشی که موقع /start قبل از منو نشون داده می‌شه "
        "(و برای کاربران منتظر تایید، به‌جای متن پیش‌فرض)."
    ),
    "a_setwelcome": "✍️ متن پیام خوش‌آمدگویی جدید رو بفرست.",
    "a_calibrate": (
        "کالیبراسیون آستانه‌ی اطمینان و ضریب ATR (SL) با بک‌تست پیشرو روی تایم‌فریم ۱ساعته. "
        "فقط گزارش می‌ده؛ برای اعمال باید روی دکمه‌ی «✅ اعمال» خود گزارش بزنی. "
        "«🧪 بلوک روند» = همین کار روی امتیازدهی آزمایشی.\n👇 نماد رو انتخاب کن (ممکنه یکی-دو دقیقه طول بکشه)."
    ),
    "a_help": "راهنمای کامل دستورهای متنی ادمین.",
}

USER_LAYOUT = [
    ["signal", "backtest"],
    ["chart", "price"],
    ["gainers", "top"],
    ["watchlist", "autoscan"],
    ["mysignals", "mystats"],
    ["myanalytics", "risk"],
    ["news", "newssettings"],
]
ADMIN_LAYOUT = [
    ["a_stats", "a_botanalytics"],
    ["a_users", "a_finduser"],
    ["a_block", "a_unblock"],
    ["a_blocklist", "a_accessmode"],
    ["a_wladd", "a_wlremove"],
    ["a_whitelist", "a_broadcast"],
    ["a_backup", "a_backuplist"],
    ["a_welcome", "a_calibrate"],
]

# دکمه‌هایی که مستقیم یه دستور بدون ورودی رو اجرا می‌کنن
RUN_COMMANDS = {
    "gainers": gainers_command, "top": top_command,
    "mysignals": mysignals_command, "mystats": mystats_command,
    "myanalytics": myanalytics_command, "news": news_command,
    "newssettings": news_settings_command,
    "a_stats": stats_command, "a_botanalytics": botanalytics_command,
    "a_blocklist": blocklist_command, "a_whitelist": whitelist_list_command,
    "a_backup": backup_command, "a_backuplist": backuplist_command,
    "a_help": admin_help_command,
}
# دکمه‌هایی که نماد می‌خوان
SYMBOL_COMMANDS = {
    "signal": signal_command, "backtest": backtest_command, "chart": chart_command,
    "price": price_command, "watch": watch_command, "unwatch": unwatch_command,
    "a_calibrate": calibrate_command,
}
# دکمه‌هایی که ورودی تایپی می‌خوان: (هندلر، نوع ورودی)
INPUT_COMMANDS = {
    "setrisk": (setrisk_command, "numbers"),
    "a_finduser": (finduser_command, "id"),
    "a_block": (block_command, "id"),
    "a_unblock": (unblock_command, "id"),
    "a_wladd": (whitelist_add_command, "id"),
    "a_wlremove": (whitelist_remove_command, "id"),
    "a_broadcast": (broadcast_command, "broadcast"),
    "a_setwelcome": (setwelcome_command, "welcome"),
}
INPUT_PROMPTS = {
    "signal": "✍️ نماد ارز رو بفرست (مثلاً `ADAUSDT` یا `ADA`).",
    "backtest": "✍️ نماد رو بفرست؛ می‌تونی تایم‌فریم و تعداد کندل هم بنویسی (مثلاً `ADA 4h 400`).",
    "chart": "✍️ نماد رو بفرست؛ می‌تونی تایم‌فریم هم بنویسی (مثلاً `ADA 4h`).",
    "price": "✍️ نماد ارز رو بفرست (مثلاً `ADA`).",
    "watch": "✍️ نماد ارز رو بفرست (مثلاً `ADA`).",
    "unwatch": "✍️ نماد ارز رو بفرست.",
    "a_calibrate": "✍️ نماد رو بفرست؛ می‌تونی تایم‌فریم، تعداد کندل و `tb` هم بنویسی (مثلاً `ADA 4h tb`).",
}
ADMIN_KEYS = {k for row in ADMIN_LAYOUT for k in row} | {"a_setwelcome"}

_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫،", "01234567890123456789.,")


# ==================== ابزارها ====================

class _MenuUpdate:
    """
    Update جایگزین برای اجرای هندلرهای دستوری از روی دکمه: پاسخ‌ها
    (update.message.reply_*) به همون چت فرستاده می‌شن و effective_user
    همون کسیه که دکمه رو زده. بقیه‌ی ویژگی‌ها از Update اصلی خونده می‌شن.
    """
    callback_query = None

    def __init__(self, update: Update, message):
        self._update = update
        self.message = message
        self.effective_message = message

    def __getattr__(self, name):
        return getattr(self._update, name)


class _TextMessage:
    """پیام با متن جایگزین (برای setwelcome که متن خام بعد از دستور رو می‌خونه)"""

    def __init__(self, message: Message, text: str):
        self._message = message
        self.text = text

    def __getattr__(self, name):
        return getattr(self._message, name)


def is_admin(user_id) -> bool:
    return user_id in ADMIN_IDS


def _btn(key: str, data: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(LABELS[key], callback_data=data or f"m:a:{key}")


def main_menu_keyboard(user_id) -> InlineKeyboardMarkup:
    rows = [[_btn(k) for k in row] for row in USER_LAYOUT]
    last = [_btn("help")]
    if is_admin(user_id):
        last.append(InlineKeyboardButton(LABELS["admin"], callback_data="m:adm"))
    rows.append(last)
    return InlineKeyboardMarkup(rows)


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [[_btn(k) for k in row] for row in ADMIN_LAYOUT]
    rows.append([_btn("a_help"), InlineKeyboardButton("🔙 منوی اصلی", callback_data="m:home")])
    return InlineKeyboardMarkup(rows)


def reply_menu_keyboard() -> ReplyKeyboardMarkup:
    """دکمه‌ی ثابت «📋 منو» پایین صفحه؛ چون نتیجه‌ها زیر منو میان و منو بالا می‌ره"""
    return ReplyKeyboardMarkup([[KeyboardButton(MENU_BUTTON_TEXT)]], resize_keyboard=True, is_persistent=True)


def _back_row(admin: bool) -> list:
    if admin:
        return [InlineKeyboardButton("🔙 پنل ادمین", callback_data="m:adm")]
    return [InlineKeyboardButton("🔙 منوی اصلی", callback_data="m:home")]


def _symbol_keyboard(key: str, symbols: list[str] | None = None) -> InlineKeyboardMarkup:
    symbols = QUICK_SYMBOLS if symbols is None else symbols
    buttons = [InlineKeyboardButton(s, callback_data=f"m:s:{key}:{s}") for s in symbols]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    if key == "a_calibrate":
        rows.insert(0, [InlineKeyboardButton("🌐 همه‌ی نمادها", callback_data="m:s:a_calibrate:ALL"),
                        InlineKeyboardButton("🧪 همه با بلوک روند", callback_data="m:s:a_calibrate:ALLTB")])
    rows.append([InlineKeyboardButton("✍️ ارز دیگه", callback_data=f"m:t:{key}")])
    if key in ("watch", "unwatch"):
        rows.append([InlineKeyboardButton(LABELS["watchlist"], callback_data="m:a:watchlist"),
                     InlineKeyboardButton("🔙 منوی اصلی", callback_data="m:home")])
    else:
        rows.append(_back_row(key in ADMIN_KEYS))
    return InlineKeyboardMarkup(rows)


def _cancel_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data=f"m:x:{'adm' if key in ADMIN_KEYS else 'home'}")]])


def _titled(key: str, extra: str = "") -> str:
    text = f"*{LABELS[key]}*\n\n{DESCRIPTIONS[key]}"
    return f"{text}\n\n{extra}" if extra else text


async def _edit_menu(query, text: str, keyboard: InlineKeyboardMarkup):
    """متن و دکمه‌های همون پیام منو رو عوض می‌کنه (اگه Markdown خراب بود، بی‌فرمت)"""
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
                                      disable_web_page_preview=True)
    except BadRequest as e:
        msg = str(e).lower()
        if "not modified" in msg:
            return
        if "parse" in msg:
            await query.edit_message_text(text, reply_markup=keyboard, disable_web_page_preview=True)
            return
        raise


async def _run(handler, update, context, message, args, label: str):
    """اجرای هندلر دستوری با args داده‌شده؛ خطا فقط به همین کاربر گزارش می‌شه"""
    context.args = list(args)
    try:
        await handler(_MenuUpdate(update, message), context)
    except Exception:
        logger.exception("اجرای «%s» از منو ناموفق بود", label)
        try:
            await message.reply_text("❌ اجرای این گزینه ناموفق بود؛ کمی بعد دوباره امتحان کن.")
        except Exception:
            pass


def _clear_input(context):
    context.user_data.pop(_STATE_KEY, None)


def _set_input(context, key: str):
    context.user_data[_STATE_KEY] = {"key": key, "ts": time.monotonic()}


async def send_main_menu(message: Message, user_id, with_reply_keyboard: bool = False):
    if with_reply_keyboard:
        # کیبورد ثابت پایین صفحه فقط با یه پیام معمولی فرستاده می‌شه (نه همراه دکمه‌های شیشه‌ای)
        await message.reply_text(f"👇 هر وقت منو رو خواستی، دکمه‌ی «{MENU_BUTTON_TEXT}» پایین صفحه رو بزن.",
                                 reply_markup=reply_menu_keyboard())
    await message.reply_text(MENU_HEADER, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard(user_id))


# ==================== دستور /menu و دکمه‌ی «📋 منو» ====================

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_input(context)
    await send_main_menu(update.message, update.effective_user.id)


async def menu_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پیام متنی معمولی: یا دکمه‌ی «📋 منو»، یا جواب گزینه‌ای که ورودی خواسته بود"""
    message = update.message
    if message is None or not message.text:
        return
    text = message.text.strip()
    if text == MENU_BUTTON_TEXT:
        await menu_command(update, context)
        return

    state = context.user_data.get(_STATE_KEY)
    if not state:
        return  # مثل قبل: متن آزاد بدون دستور نادیده گرفته می‌شه
    _clear_input(context)
    if time.monotonic() - state["ts"] > INPUT_TIMEOUT_SECONDS:
        await message.reply_text("⌛️ زمان این درخواست تموم شده بود؛ دوباره از منو انتخاب کن.")
        return

    key = state["key"]
    user_id = update.effective_user.id
    if key in ADMIN_KEYS and not is_admin(user_id):
        return

    if key in SYMBOL_COMMANDS:
        parts = text.translate(_FA_DIGITS).split()
        if key == "a_calibrate":
            await _run(calibrate_command, update, context, message, parts, key)
        else:
            await _run(SYMBOL_COMMANDS[key], update, context, message, parts[:4], key)
        return

    handler, kind = INPUT_COMMANDS[key]
    if kind == "numbers":
        parts = text.translate(_FA_DIGITS).replace(",", " ").split()
        await _run(handler, update, context, message, parts, key)
    elif kind == "id":
        normalized = text.translate(_FA_DIGITS)
        first, _, rest = normalized.partition(" ")
        if not first.lstrip("-").isdigit():
            _set_input(context, key)  # فرصت دوباره
            await message.reply_text("❌ شناسه باید عدد باشه؛ دوباره بفرست یا «❌ لغو» رو بزن.",
                                     reply_markup=_cancel_keyboard(key))
            return
        args = [first] + (text.partition(" ")[2].split() if rest else [])
        await _run(handler, update, context, message, args, key)
    elif kind == "broadcast":
        user_count = len(await db.get_all_user_ids())
        context.user_data[_BROADCAST_KEY] = message.text
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ ارسال به {user_count} کاربر", callback_data="m:bc:yes"),
            InlineKeyboardButton("❌ لغو", callback_data="m:bc:no"),
        ]])
        await message.reply_text(f"📢 پیش‌نمایش پیام همگانی:\n\n📢 {message.text}\n\nارسال بشه؟",
                                 reply_markup=keyboard)
    elif kind == "welcome":
        proxy_message = _TextMessage(message, f"/setwelcome {message.text}")
        await _run(handler, update, context, proxy_message, message.text.split(), key)


# ==================== دکمه‌ها ====================

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    message = query.message
    if not isinstance(message, Message):
        await query.answer("این منو قدیمی شده؛ /start رو بزن.", show_alert=True)
        return
    user_id = query.from_user.id
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    # هر کلیک روی منو، منتظرِ ورودی بودنِ قبلی رو لغو می‌کنه (جز خود «✍️»)
    if action != "t":
        _clear_input(context)

    if action == "home":
        await query.answer()
        await _edit_menu(query, MENU_HEADER, main_menu_keyboard(user_id))
        return

    admin_action = action in ("adm", "acc", "bc", "rmw", "u") or (
        len(parts) > 2 and parts[2] in ADMIN_KEYS)
    if admin_action and not is_admin(user_id):
        await query.answer("⛔️ این بخش فقط برای ادمینه.", show_alert=True)
        return

    if action == "adm":
        await query.answer()
        await _edit_menu(query, ADMIN_HEADER, admin_menu_keyboard())
        return

    if action == "x":
        await query.answer("لغو شد")
        back = parts[2] if len(parts) > 2 else "home"
        if back == "adm" and is_admin(user_id):
            await _edit_menu(query, ADMIN_HEADER, admin_menu_keyboard())
        else:
            await _edit_menu(query, MENU_HEADER, main_menu_keyboard(user_id))
        return

    if action == "a" and len(parts) == 3:
        await _open_item(update, context, query, parts[2], user_id)
        return

    if action == "s" and len(parts) == 4 and parts[2] in SYMBOL_COMMANDS:
        key, symbol = parts[2], parts[3]
        await query.answer(f"⏳ {LABELS[key]} — {symbol}")
        # normalize_symbol خود هندلرها «BTC» یا «BTC/USDT» رو به BTC/USDT تبدیل می‌کنه
        if key == "a_calibrate" and symbol in ("ALL", "ALLTB"):
            args = ["tb"] if symbol == "ALLTB" else []
        else:
            args = [symbol]
        await _run(SYMBOL_COMMANDS[key], update, context, message, args, key)
        return

    if action == "t" and len(parts) == 3 and (parts[2] in SYMBOL_COMMANDS or parts[2] in INPUT_COMMANDS):
        key = parts[2]
        await query.answer()
        _set_input(context, key)
        prompt = INPUT_PROMPTS.get(key, "")
        await _edit_menu(query, _titled(key, prompt), _cancel_keyboard(key))
        return

    if action == "scan" and len(parts) == 3 and parts[2] in ("on", "off"):
        await query.answer()
        await _run(autoscan_command, update, context, message, [parts[2]], "autoscan")
        await _edit_menu(query, _titled("autoscan", await _autoscan_status(user_id)), _autoscan_keyboard())
        return

    if action == "acc" and len(parts) == 3 and parts[2] in ("open", "whitelist"):
        await query.answer()
        await _run(accessmode_command, update, context, message, [parts[2]], "accessmode")
        await _edit_menu(query, _titled("a_accessmode", await _access_status()), _access_keyboard())
        return

    if action == "u" and len(parts) == 3 and parts[2].isdigit():
        await query.answer()
        await _show_users_page(update, context, query, int(parts[2]))
        return

    if action == "bc" and len(parts) == 3:
        text = context.user_data.pop(_BROADCAST_KEY, None)
        if parts[2] != "yes" or not text:
            await query.answer("لغو شد")
            await query.edit_message_text("❌ پیام همگانی ارسال نشد.")
            return
        await query.answer("⏳ در حال ارسال...")
        await query.edit_message_reply_markup(reply_markup=None)
        await _run(broadcast_command, update, context, message, [text], "broadcast")
        return

    if action == "rmw" and len(parts) == 3:
        await query.answer()
        if parts[2] == "ask":
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ بله، حذف کن", callback_data="m:rmw:yes"),
                InlineKeyboardButton("❌ نه", callback_data="m:a:a_welcome"),
            ]])
            await _edit_menu(query, "🗑 پیام خوش‌آمدگویی سفارشی حذف بشه؟", keyboard)
        elif parts[2] == "yes":
            await _run(removewelcome_command, update, context, message, [], "removewelcome")
            await _edit_menu(query, _titled("a_welcome"), _welcome_keyboard())
        return

    await query.answer("این دکمه دیگه معتبر نیست؛ /start رو بزن.", show_alert=True)


async def _open_item(update, context, query, key: str, user_id):
    message = query.message
    if key not in LABELS or key == "admin":
        await query.answer("این دکمه دیگه معتبر نیست؛ /start رو بزن.", show_alert=True)
        return

    if key == "help":
        from handlers.basic import help_command
        await query.answer()
        await _edit_menu(query, _titled("help"), main_menu_keyboard(user_id))
        await _run(help_command, update, context, message, [], key)
        return

    if key in RUN_COMMANDS:
        await query.answer(f"⏳ {LABELS[key]}")
        keyboard = admin_menu_keyboard() if key in ADMIN_KEYS else main_menu_keyboard(user_id)
        await _edit_menu(query, _titled(key), keyboard)
        await _run(RUN_COMMANDS[key], update, context, message, [], key)
        return

    await query.answer()
    if key == "unwatch":
        symbols = await db.get_watchlist(user_id)
        if not symbols:
            await _edit_menu(query, _titled("unwatch", "ℹ️ واچ‌لیستت خالیه."), _watchlist_keyboard())
            return
        # callback_data حداکثر ۶۴ بایته؛ نمادهای خیلی بلند فقط با تایپ
        fitting = [s for s in symbols if len(f"m:s:unwatch:{s}".encode()) <= 64][:30]
        await _edit_menu(query, _titled("unwatch"), _symbol_keyboard("unwatch", fitting))
        return

    if key in SYMBOL_COMMANDS:
        await _edit_menu(query, _titled(key), _symbol_keyboard(key))
        return

    if key in INPUT_COMMANDS:
        _set_input(context, key)
        await _edit_menu(query, _titled(key), _cancel_keyboard(key))
        return

    if key == "watchlist":
        await _edit_menu(query, _titled("watchlist"), _watchlist_keyboard())
        await _run(mywatchlist_command, update, context, message, [], key)
    elif key == "autoscan":
        await _edit_menu(query, _titled("autoscan", await _autoscan_status(user_id)), _autoscan_keyboard())
    elif key == "risk":
        await _edit_menu(query, _titled("risk"), InlineKeyboardMarkup([
            [_btn("setrisk")], _back_row(False)]))
        await _run(myrisk_command, update, context, message, [], key)
    elif key == "a_accessmode":
        await _edit_menu(query, _titled("a_accessmode", await _access_status()), _access_keyboard())
    elif key == "a_users":
        await _show_users_page(update, context, query, 1)
    elif key == "a_welcome":
        await _edit_menu(query, _titled("a_welcome"), _welcome_keyboard())
        await _run(getwelcome_command, update, context, message, [], key)


def _watchlist_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("watch"), _btn("unwatch")], _back_row(False)])


def _autoscan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 روشن", callback_data="m:scan:on"),
         InlineKeyboardButton("🔴 خاموش", callback_data="m:scan:off")],
        _back_row(False),
    ])


async def _autoscan_status(user_id) -> str:
    enabled = await db.get_auto_scan(user_id)
    return f"وضعیت فعلی: *{'روشن 🟢' if enabled else 'خاموش 🔴'}*"


def _access_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 باز", callback_data="m:acc:open"),
         InlineKeyboardButton("🔒 لیست سفید", callback_data="m:acc:whitelist")],
        _back_row(True),
    ])


async def _access_status() -> str:
    mode = await db.get_setting("access_mode", "open")
    return f"حالت فعلی: *{'🔒 لیست سفید' if mode == 'whitelist' else '🌐 باز'}*"


def _welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("a_setwelcome"), InlineKeyboardButton("🗑 حذف پیام", callback_data="m:rmw:ask")],
        _back_row(True),
    ])


async def _show_users_page(update, context, query, page: int):
    total = await db.get_user_total_count()
    total_pages = max(1, (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️ صفحه‌ی قبل", callback_data=f"m:u:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("صفحه‌ی بعد ▶️", callback_data=f"m:u:{page + 1}"))
    rows = ([nav] if nav else []) + [_back_row(True)]
    await _edit_menu(query, _titled("a_users", f"صفحه {page} از {total_pages}"), InlineKeyboardMarkup(rows))
    await _run(users_command, update, context, query.message, [str(page)], "a_users")
