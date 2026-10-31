"""
ماژول اخبار تاثیرگذار بر کریپتو و فارکس — فارسی، با درصد تاثیر
منابع داده: RSS های CoinDesk و CoinTelegraph و تقویم هفتگی ForexFactory.
دریافت این داده‌ها کلید نمی‌خواهد؛ ترجمهٔ فقط عنوان اخبار با Gemini اختیاری
است و به کلید API و دسترسی سرویس نیاز دارد. متن مقاله ترجمه/ارسال نمی‌شود.
نمایش اخبار و تقویم جداست؛ ساعت‌های مشخص به وقت ایران هستند.
امتیاز تأثیر همان برآورد قبلی است، نه احتمال یا میزان تغییر قیمت.
"""
import re
import time
import logging
import email.utils
import hashlib
import json
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import httpx
from news_translation import translate_news_titles
from news_presentation import (calendar_metadata, calendar_title, calendar_local_date,
    article_datetime, related_topics, render_combined, render_calendar, render_brief)

# فقط اخبار امروز (از ۰۰:۰۰ امروز به وقت تهران) — نه ۲۴ ساعت گذشته
from zoneinfo import ZoneInfo
try:
    TEHRAN_TZ = ZoneInfo("Asia/Tehran")
except Exception:
    TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 8.0
CACHE_TTL_NEWS = 15 * 60  # 15 دقیقه
CACHE_TTL_CALENDAR = 60 * 60  # 1 ساعت

_cache: dict = {}

def _cache_get(key, ttl):
    e = _cache.get(key)
    if not e:
        return None
    ts, val = e
    if time.time() - ts > ttl:
        return None
    return val

def _cache_set(key, val):
    _cache[key] = (time.time(), val)

def _parse_pubdate(pubdate_str: str) -> datetime | None:
    """پارس pubDate آراس‌اس (RFC2822 مثل 'Mon, 09 Sep 2024 12:00:00 GMT')"""
    if not pubdate_str:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(pubdate_str)
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def _is_today(pubdate_str: str) -> bool:
    """آیا خبر مال امروزه (از ۰۰:۰۰ امروز به وقت تهران)؟ اگه تاریخ قابل پارس نباشه، تازه فرض می‌شه"""
    dt = article_datetime(pubdate_str)
    if dt is None:
        return True
    # تبدیل به وقت تهران و مقایسه تاریخ شمسی/میلادی همون روز
    dt_tehran = dt.astimezone(TEHRAN_TZ)
    now_tehran = datetime.now(TEHRAN_TZ)
    return dt_tehran.date() == now_tehran.date()

# alias برای سازگاری
def _is_fresh(pubdate_str: str, max_age_hours: int = 24) -> bool:
    return _is_today(pubdate_str)

def _parse_forex_date(date_str: str, time_str: str) -> datetime | None:
    """پارس تاریخ ForexFactory: date مثل '09-12-2024' یا '2024-09-12' و time مثل '10:00am'"""
    if not date_str:
        return None
    date_str = date_str.strip()
    time_str = (time_str or "").strip().lower()
    # تاریخ
    dt_date = None
    for fmt in ("%m-%d-%Y", "%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%m-%d-%y"):
        try:
            dt_date = datetime.strptime(date_str, fmt)
            break
        except ValueError:
            continue
    if dt_date is None:
        return None
    # ساعت - اگه All Day / Tentative بود، همون 00:00
    if not time_str or time_str in ("all day", "tentative", "", "-"):
        return dt_date.replace(tzinfo=timezone.utc)
    # مثل 10:00am / 2:30pm / 10:00 am
    time_str = time_str.replace(" ", "")
    for fmt in ("%I:%M%p", "%I:%M:%S%p", "%H:%M", "%I%p"):
        try:
            t = datetime.strptime(time_str, fmt)
            return dt_date.replace(hour=t.hour, minute=t.minute, second=0, tzinfo=timezone.utc)
        except ValueError:
            continue
    return dt_date.replace(tzinfo=timezone.utc)

def _news_identity_text(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _news_identity_url(value: str) -> str | None:
    """Conservative URL normalization; meaningful/repeated query order is kept."""
    value = value.strip()
    if any(character.isspace() for character in value):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None):
            return None
        port = parts.port  # also validates malformed ports
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if port is not None and (parts.scheme.lower(), port) not in (("http", 80), ("https", 443)):
            host += f":{port}"
        tracking = {"fbclid", "gclid", "mc_cid", "mc_eid", "mkt_tok"}
        query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
                 if not key.casefold().startswith("utm_") and key.casefold() not in tracking]
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(query), ""))
    except ValueError:
        return None


def news_item_key(item) -> str | None:
    """
    Versioned SHA256 identity, independent of Python hash seed/impact/translation.
    ForexFactory's shared calendar URL is NOT a unique event ID.
    """
    if not isinstance(item, dict):
        return None
    source = _news_identity_text(item.get("source"))
    title = _news_identity_text(item.get("title"))
    pubdate = item.get("pubDate")
    date_text = _news_identity_text(pubdate)
    if source == "forexfactory" or item.get("category") == "فارکس":
        country = _news_identity_text(item.get("country"))
        if not (source and title and country and date_text):
            return None
        parts = pubdate.strip().split(maxsplit=1)
        parsed = _parse_forex_date(parts[0], parts[1] if len(parts) > 1 else "")
        event_time = parsed.astimezone(timezone.utc).isoformat() if parsed else date_text
        identity = ["calendar", source, country, title, event_time]
    else:
        link = item.get("link")
        if link is not None and not isinstance(link, str):
            return None
        if isinstance(link, str) and link.strip():
            canonical = _news_identity_url(link)
            if canonical is None:
                return None
            identity = ["article-url", canonical]
        else:
            if not (source and title and date_text):
                return None
            parsed = _parse_pubdate(pubdate)
            event_time = parsed.astimezone(timezone.utc).isoformat() if parsed else date_text
            identity = ["article-fallback", source, title, event_time]
    payload = json.dumps(["news-v1", *identity], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

# --- دیکشنری ترجمه کلمات کلیدی به فارسی ---
KEYWORD_FA = {
    "bitcoin": "بیت‌کوین", "btc": "بیت‌کوین", "ethereum": "اتریوم", "eth": "اتریوم",
    "fed": "فدرال رزرو آمریکا", "federal reserve": "فدرال رزرو", "sec": "کمیسیون بورس آمریکا (SEC)",
    "etf": "صندوق ETF", "halving": "هاوینگ", "inflation": "تورم", "cpi": "شاخص قیمت مصرف‌کننده (CPI)",
    "nfp": "گزارش اشتغال آمریکا (NFP)", "nonfarm": "اشتغال غیرکشاورزی", "gdp": "تولید ناخالص داخلی (GDP)",
    "interest rate": "نرخ بهره", "rate hike": "افزایش نرخ بهره", "rate cut": "کاهش نرخ بهره",
    "fomc": "کمیته بازار آزاد فدرال (FOMC)", "powell": "پاول (رئیس فدرال رزرو)",
    "regulation": "قانون‌گذاری", "ban": "ممنوعیت", "approval": "تایید", "launch": "راه‌اندازی",
    "hack": "هک", "crash": "سقوط", "rally": "رشد شارپ", "bull": "صعودی", "bear": "نزولی",
    "dollar": "دلار", "euro": "یورو", "gold": "طلا", "oil": "نفت",
    "binance": "بایننس", "coinbase": "کوین‌بیس", "blackrock": "بلک‌راک",
}

# کلمات با تاثیر بالا -> درصد تاثیر بیشتر
HIGH_IMPACT_KEYWORDS = [
    "fed", "fomc", "powell", "cpi", "nfp", "nonfarm", "interest rate", "rate hike", "rate cut",
    "sec", "etf", "approval", "ban", "regulation", "halving", "hack", "crash", "blackrock",
    "inflation", "gdp", "unemployment",
]
MEDIUM_IMPACT_KEYWORDS = [
    "binance", "coinbase", "launch", "partnership", "upgrade", "rally", "bull", "bear", "dollar", "gold", "oil"
]

def _calc_impact(title: str, description: str = "", source_weight: int = 50, calendar_importance: str = "") -> int:
    """محاسبه درصد تاثیر 0-100 بر اساس کلمات کلیدی + اهمیت تقویم"""
    text = f"{title} {description}".lower()
    score = source_weight  # پایه
    # اهمیت تقویم فارکس
    if calendar_importance:
        imp = calendar_importance.lower()
        if "high" in imp or "3" in imp or "مهم" in imp:
            return 85 + (hash(title) % 12)  # 85-96
        if "medium" in imp or "2" in imp:
            return 55 + (hash(title) % 15)  # 55-69
        if "low" in imp or "1" in imp:
            return 25 + (hash(title) % 15)  # 25-39
    # اخبار کریپتو بر اساس کلمات کلیدی
    high_hits = sum(1 for k in HIGH_IMPACT_KEYWORDS if k in text)
    med_hits = sum(1 for k in MEDIUM_IMPACT_KEYWORDS if k in text)
    score += high_hits * 18 + med_hits * 8
    # سقف
    score = min(96, score)
    # کف 15
    score = max(15, score)
    # کمی تنوع با هش تا همیشه عدد ثابت نباشه
    score = min(96, max(15, score + (hash(title) % 7) - 3))
    return int(score)

def _impact_label(pct: int) -> str:
    if pct >= 80:
        return "🔴 تاثیر خیلی بالا"
    if pct >= 60:
        return "🟠 تاثیر بالا"
    if pct >= 40:
        return "🟡 تاثیر متوسط"
    return "🟢 تاثیر کم"

def _to_persian_summary(title: str, desc: str = "") -> str:
    """خلاصه فارسی ساده: کلمات کلیدی رو ترجمه می‌کنه + جمله فارسی می‌سازه"""
    text = f"{title} {desc}".strip()
    # ترجمه کلمات کلیدی داخل متن
    fa_text = text
    for en, fa in sorted(KEYWORD_FA.items(), key=lambda x: -len(x[0])):
        pattern = re.compile(re.escape(en), re.IGNORECASE)
        fa_text = pattern.sub(fa, fa_text)
    # اگه هنوز انگلیسی زیاد مونده، همون عنوان اصلی رو نگه دار و یه خلاصه فارسی اضافه کن
    # خلاصه کوتاه فارسی بر اساس تاثیر
    lower = text.lower()
    if any(k in lower for k in ["etf", "approval", "sec"]):
        summary = "خبر مربوط به تایید/قانون‌گذاری ETF — معمولاً تاثیر مستقیم روی قیمت بیت‌کوین و آلت‌کوین‌ها داره."
    elif any(k in lower for k in ["fed", "fomc", "rate", "powell", "cpi", "inflation"]):
        summary = "خبر مرتبط با سیاست پولی آمریکا و تورم — روی دلار، طلا و کل بازار کریپتو اثر می‌ذاره."
    elif any(k in lower for k in ["nfp", "unemployment", "gdp"]):
        summary = "داده اقتصادی مهم آمریکا — نوسان دلار و به تبعش کریپتو رو بالا می‌بره."
    elif any(k in lower for k in ["hack", "crash", "ban"]):
        summary = "خبر منفی/ریسکی — معمولاً باعث فشار فروش کوتاه‌مدت می‌شه."
    elif any(k in lower for k in ["halving", "upgrade", "launch", "partnership"]):
        summary = "خبر مثبت فاندامنتالی — می‌تونه محرک صعودی میان‌مدت باشه."
    elif any(k in lower for k in ["binance", "coinbase", "blackrock"]):
        summary = "خبر مرتبط با بازیگر بزرگ بازار — روی نقدینگی و اعتماد بازار اثر داره."
    else:
        summary = "خبر عمومی بازار — روند کلی رو دنبال کن و حد ضرر رو رعایت کن."
    return summary

def _translate_title(title: str) -> str:
    """عنوان رو تا حد ممکن فارسی می‌کنه (کلمات کلیدی)"""
    fa = title
    for en, fa_word in sorted(KEYWORD_FA.items(), key=lambda x: -len(x[0])):
        fa = re.compile(re.escape(en), re.IGNORECASE).sub(fa_word, fa)
    return fa

# --- دریافت اخبار کریپتو از RSS ---
CRYPTO_RSS = [
    ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk", 55),
    ("https://cointelegraph.com/rss", "CoinTelegraph", 50),
]

async def _fetch_rss(url: str) -> list:
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                return []
            root = ET.fromstring(resp.text)
            items = []
            for item in root.findall(".//item")[:8]:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                desc = (item.findtext("description") or "").strip()
                # پاکسازی HTML ساده
                desc = re.sub(r"<[^>]+>", "", desc)[:220]
                pub = (item.findtext("pubDate") or "").strip()
                if title:
                    items.append({"title": title, "link": link, "desc": desc, "pubDate": pub})
            return items
    except Exception as e:
        logger.warning(f"RSS fetch failed {url}: {e}")
        return []

async def fetch_crypto_news(limit: int = 8, force: bool = False) -> list:
    if not force:
        cached = _cache_get("crypto_news", CACHE_TTL_NEWS)
        if cached is not None:
            return await translate_news_titles([item for item in cached if _is_today(item.get("pubDate", ""))][:limit])
    all_items = []
    for url, source, weight in CRYPTO_RSS:
        items = await _fetch_rss(url)
        for it in items:
            # فقط اخبار امروز (از ۰۰:۰۰ امروز به وقت تهران)
            if not _is_today(it["pubDate"]):
                continue
            pct = _calc_impact(it["title"], it["desc"], source_weight=weight)
            all_items.append({
                "title": it["title"],
                "title_fa": it["title"],
                "translation_status": "native" if re.search(r"[\u0600-\u06ff]", it["title"]) else "original",
                "related_to": related_topics(it["title"], it["desc"]),
                "summary_fa": "",
                "impact": pct,
                "impact_label": _impact_label(pct),
                "category": "کریپتو",
                "source": source,
                "link": it["link"],
                "pubDate": it["pubDate"],
            })
    # مرتب بر اساس تاثیر نزولی
    all_items.sort(key=lambda x: x["impact"], reverse=True)
    _cache_set("crypto_news", all_items)
    return await translate_news_titles(all_items[:limit])

# --- تقویم فارکس (ForexFactory) ---
async def fetch_forex_calendar(limit: int = 8, force: bool = False, day=None) -> list:
    """Weekly UTC XML cache, filtered per requested Tehran day on every read.

    Display times do not use the legacy parser kept for persistent identity.
    Unknown clocks retain their status, never an invented midnight.
    """
    selected_day = day or datetime.now(TEHRAN_TZ).date()
    cached = None if force else _cache_get("forex_calendar_v2", CACHE_TTL_CALENDAR)
    if cached is None:
        urls = ["https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
                "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml"]
        cached = []
        for url in urls:
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
                    resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                    if resp.status_code != 200:
                        continue
                    root = ET.fromstring(resp.text)
                    for ev in root.findall(".//event"):
                        title = (ev.findtext("title") or "").strip()
                        country = (ev.findtext("country") or "").strip()
                        date = (ev.findtext("date") or "").strip()
                        time_e = (ev.findtext("time") or "").strip()
                        impact = (ev.findtext("impact") or "").strip()
                        if not title or country not in ("USD", "EUR", "GBP", "JPY", "CNY"):
                            continue
                        metadata = calendar_metadata(date, time_e)
                        if metadata["event_date"] is None:
                            continue
                        pct = _calc_impact(title, calendar_importance=impact, source_weight=45)
                        if pct < 35:
                            continue
                        translated, _ = calendar_title(title)
                        cached.append({
                            "title": f"{country} - {title}", "title_fa": translated,
                            "event_title": title, "summary_fa": "",
                            "impact": pct, "impact_label": _impact_label(pct),
                            "category": "فارکس", "source": "ForexFactory",
                            "link": "https://www.forexfactory.com/calendar",
                            "pubDate": f"{date} {time_e}", "country": country,
                            "raw_impact": impact,
                            "forecast": (ev.findtext("forecast") or "").strip(),
                            "previous": (ev.findtext("previous") or "").strip(),
                            "actual": (ev.findtext("actual") or "").strip(),
                            **metadata,
                        })
                    if cached:
                        break
            except Exception as error:
                logger.warning("Calendar source failed: %s", type(error).__name__)
        _cache_set("forex_calendar_v2", cached)
    items = [item for item in cached if calendar_local_date(item) == selected_day]
    if limit is None:
        items.sort(key=lambda item: (item.get("event_time_utc") or "zz", -item["impact"]))
    else:
        # Preserve importance-based selection for existing auto/signal callers.
        items.sort(key=lambda item: -item["impact"])
    return items if limit is None else items[:limit]


async def get_combined_news(crypto_limit: int = 6, forex_limit: int = 4, force: bool = False) -> list:
    """ترکیب اخبار کریپتو + تقویم فارکس، مرتب بر اساس تاثیر — اگه force=True کش نادیده گرفته می‌شه"""
    crypto = await fetch_crypto_news(limit=crypto_limit, force=force)
    forex = await fetch_forex_calendar(limit=forex_limit, force=force)
    combined = crypto + forex
    combined.sort(key=lambda x: x["impact"], reverse=True)
    return combined

def format_news_message(news_list: list, max_items: int = 8) -> str:
    """Manual market list or separate sections for legacy combined auto batches."""
    return render_combined(news_list, max_items=max_items)


def format_calendar_message(news_list: list, max_items: int = 12, day=None) -> str:
    return render_calendar(news_list, max_items=max_items, day=day)


def format_brief_for_signal(news_list: list, max_items: int = 2) -> str:
    return render_brief(news_list, max_items=max_items)
