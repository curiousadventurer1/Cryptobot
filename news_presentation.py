"""Persian news/calendar display. Original source fields remain identity inputs.

Calendar XML clocks are UTC for the verified Faireconomy weekly export. Its JSON
export carries an explicit offset and was cross-checked; never reuse this rule
for another source. Article clocks must have an explicit timezone.
"""
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from telegram.helpers import escape_markdown

try:
    TEHRAN_TZ = ZoneInfo('Asia/Tehran')
except Exception:
    TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')
WEEKDAYS = ('دوشنبه', 'سه‌شنبه', 'چهارشنبه', 'پنجشنبه', 'جمعه', 'شنبه', 'یکشنبه')
MONTHS = ('ژانویه','فوریه','مارس','آوریل','مه','ژوئن','ژوئیه','اوت','سپتامبر','اکتبر','نوامبر','دسامبر')
COUNTRIES = {'USD':'آمریکا', 'EUR':'منطقهٔ یورو', 'GBP':'بریتانیا', 'JPY':'ژاپن', 'CNY':'چین'}
CALENDAR_TITLES = {
    'non-farm employment change':'اشتغال غیرکشاورزی (NFP)',
    'nonfarm payrolls':'اشتغال غیرکشاورزی (NFP)',
    'nfp':'اشتغال غیرکشاورزی (NFP)',
    'adp non-farm employment change':'اشتغال غیرکشاورزی بخش خصوصی (ADP)',
    'unemployment rate':'نرخ بیکاری',
    'average hourly earnings':'میانگین درآمد ساعتی',
    'unemployment claims':'درخواست‌های بیمهٔ بیکاری',
    'initial jobless claims':'درخواست‌های اولیهٔ بیمهٔ بیکاری',
    'jolts job openings':'فرصت‌های شغلی (JOLTS)',
    'cpi':'شاخص قیمت مصرف‌کننده (CPI)',
    'core cpi':'شاخص قیمت مصرف‌کنندهٔ هسته (Core CPI)',
    'cpi flash estimate':'برآورد اولیهٔ شاخص قیمت مصرف‌کننده (CPI)',
    'core cpi flash estimate':'برآورد اولیهٔ تورم هسته (Core CPI)',
    'ppi':'شاخص قیمت تولیدکننده (PPI)',
    'core ppi':'شاخص قیمت تولیدکنندهٔ هسته (Core PPI)',
    'core pce price index':'شاخص قیمت مخارج مصرف شخصی هسته (Core PCE)',
    'pce price index':'شاخص قیمت مخارج مصرف شخصی (PCE)',
    'gdp':'تولید ناخالص داخلی (GDP)',
    'advance gdp':'برآورد اولیهٔ تولید ناخالص داخلی (GDP)',
    'prelim gdp':'برآورد مقدماتی تولید ناخالص داخلی (GDP)',
    'final gdp':'برآورد نهایی تولید ناخالص داخلی (GDP)',
    'retail sales':'خرده‌فروشی', 'core retail sales':'خرده‌فروشی هسته',
    'federal funds rate':'نرخ بهرهٔ فدرال رزرو',
    'fomc statement':'بیانیهٔ کمیتهٔ بازار آزاد فدرال (FOMC)',
    'fomc meeting minutes':'صورت‌جلسهٔ کمیتهٔ بازار آزاد فدرال (FOMC)',
    'fomc press conference':'نشست خبری فدرال رزرو (FOMC)',
    'interest rate decision':'تصمیم‌گیری دربارهٔ نرخ بهره',
    'main refinancing rate':'نرخ اصلی تأمین مالی بانک مرکزی اروپا',
    'ecb press conference':'نشست خبری بانک مرکزی اروپا',
    'monetary policy statement':'بیانیهٔ سیاست پولی',
    'monetary policy report':'گزارش سیاست پولی',
    'official bank rate':'نرخ بهرهٔ رسمی بانک انگلستان',
    'boe inflation report':'گزارش تورم بانک انگلستان',
    'boj policy rate':'نرخ بهرهٔ بانک ژاپن',
    'manufacturing pmi':'شاخص مدیران خرید تولید (PMI)',
    'services pmi':'شاخص مدیران خرید خدمات (PMI)',
    'flash manufacturing pmi':'برآورد اولیهٔ شاخص مدیران خرید تولید (PMI)',
    'flash services pmi':'برآورد اولیهٔ شاخص مدیران خرید خدمات (PMI)',
    'ism manufacturing pmi':'شاخص مدیران خرید تولید (ISM)',
    'ism services pmi':'شاخص مدیران خرید خدمات (ISM)',
    'ism manufacturing prices':'شاخص قیمت‌های تولید (ISM)',
    'cb consumer confidence':'اعتماد مصرف‌کننده (CB)',
    'prelim uom consumer sentiment':'برآورد اولیهٔ احساسات مصرف‌کنندهٔ میشیگان',
    'revised uom consumer sentiment':'احساسات مصرف‌کنندهٔ میشیگان — بازنگری‌شده',
    'durable goods orders':'سفارش کالاهای بادوام',
    'core durable goods orders':'سفارش کالاهای بادوام هسته',
    'industrial production':'تولید صنعتی', 'trade balance':'تراز تجاری',
    'current account':'حساب جاری', 'building permits':'مجوزهای ساخت‌وساز',
    'housing starts':'آغاز ساخت مسکن', 'new home sales':'فروش خانه‌های نوساز',
    'existing home sales':'فروش خانه‌های موجود', 'pending home sales':'فروش در انتظارِ مسکن',
    'bank holiday':'تعطیلی بانک‌ها', 'employment change':'تغییر اشتغال',
}


def fa_digits(value):
    return str(value).translate(DIGITS)


def _text(value, limit=420):
    return escape_markdown(' '.join(str(value or '').split())[:limit], version=1)


def date_label(value):
    """Gregorian date with Persian weekday/month, matching the requested sample."""
    return f'{WEEKDAYS[value.weekday()]}، {fa_digits(value.day)} {MONTHS[value.month-1]} {fa_digits(value.year)}'


def article_datetime(value):
    import email.utils
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(TEHRAN_TZ)
    except (ValueError, OverflowError):
        return None


def publication_label(item):
    stamp = article_datetime(item.get('pubDate'))
    if stamp is None:
        return 'زمان انتشار مشخص نیست'
    return f'{date_label(stamp)} — {fa_digits(stamp.strftime("%H:%M"))} به وقت ایران'


def calendar_metadata(date_text, clock_text):
    """Strict source date/clock; tentative/all-day are not invented midnight."""
    day = None
    if isinstance(date_text, str):
        for fmt in ('%m-%d-%Y','%Y-%m-%d','%m/%d/%Y'):
            try:
                day = datetime.strptime(date_text.strip(),fmt).date(); break
            except ValueError:
                pass
    clock = clock_text.strip().casefold() if isinstance(clock_text,str) else ''
    status = 'all_day' if clock=='all day' else 'tentative' if clock=='tentative' else 'unknown'
    instant = None
    if day is not None and clock not in ('all day','tentative','','-'):
        for fmt in ('%I:%M%p','%I:%M:%S%p','%H:%M','%I%p'):
            try:
                parsed = datetime.strptime(clock.replace(' ',''),fmt)
                instant = datetime.combine(day,parsed.time(),tzinfo=timezone.utc)
                status = 'scheduled'; break
            except ValueError:
                pass
    return {'event_date':day.isoformat() if day else None,
            'event_time_utc':instant.isoformat() if instant else None,'time_status':status}


def _calendar_metadata(item):
    if 'time_status' in item and 'event_date' in item:
        return item
    raw = item.get('pubDate')
    parts = raw.strip().split(maxsplit=1) if isinstance(raw,str) else []
    return dict(item,**calendar_metadata(parts[0] if parts else '',parts[1] if len(parts)>1 else ''))


def calendar_local_date(item):
    value = _calendar_metadata(item)
    stamp = article_datetime(value.get('event_time_utc'))
    if stamp is not None:
        return stamp.date()
    try:
        return datetime.strptime(value.get('event_date') or '', '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def calendar_clock_label(item):
    value = _calendar_metadata(item)
    if value.get('time_status')=='scheduled':
        stamp = article_datetime(value.get('event_time_utc'))
        if stamp is not None:
            return fa_digits(stamp.strftime('%H:%M'))+' به وقت ایران'
    return {'all_day':'رویداد تمام‌روز؛ ساعت مشخص ندارد',
            'tentative':'زمان هنوز قطعی نیست'}.get(value.get('time_status'),'ساعت اعلام نشده')


def calendar_title(title):
    original = ' '.join(str(title or '').split())
    base = original.casefold()
    period = ''
    for suffix,label in [(' m/m','ماهانه'),(' y/y','سالانه'),(' q/q','فصلی')]:
        if base.endswith(suffix):
            base=base[:-len(suffix)];period=' — '+label;break
    translated = CALENDAR_TITLES.get(base)
    return (translated+period,True) if translated else (original,False)


def related_topics(title, description=''):
    text = f'{title or ""} {description or ""}'.casefold()
    rules = [
        (r'\b(bitcoin|btc)\b','بیت‌کوین'), (r'\b(ethereum|eth)\b','اتریوم'),
        (r'\b(solana|sol)\b','سولانا'), (r'\b(xrp|ripple)\b','ریپل (XRP)'),
        (r'\b(etf|etfs)\b','ETF'),
        (r'\b(fed|fomc|federal reserve|powell)\b','سیاست پولی آمریکا'),
        (r'\b(inflation|cpi|pce)\b','تورم'),
        (r'\b(nfp|nonfarm|unemployment|jobs|payrolls)\b','اشتغال'),
        (r'\b(sec|regulation|regulatory)\b','قانون‌گذاری'),
        (r'\b(binance|coinbase|exchange|exchanges)\b','صرافی‌ها'),
        (r'\b(hack|hacked|exploit|breach)\b','امنیت و هک'),
    ]
    labels=[label for pattern,label in rules if re.search(pattern,text)]
    return labels or ['بازار عمومی کریپتو']


def _safe_link(value):
    if not isinstance(value,str): return None
    try:
        parsed=urlsplit(value)
        if parsed.scheme.casefold() not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
            return None
        parsed.port
        if any(char.isspace() for char in value): return None
        return quote(value,safe=':/?&=,%#@+~._-')[:1000]
    except ValueError:
        return None


def _fit(header, blocks, footer, *, budget=3500, omitted=0):
    shown=[]; more='\n\nموارد بیشتر را از منبع یا نمای همان روز ببینید.'
    for block in blocks:
        candidate='\n\n'.join([header,*shown,block])+more+'\n\n'+footer
        if len(candidate)>budget:
            omitted+=len(blocks)-len(shown);break
        shown.append(block)
    return '\n\n'.join([header,*shown]+([more.strip()] if omitted else [])+[footer])


def _impact(item):
    score=item.get('impact')
    if type(score) not in (int,float) or not 0<=score<=100:
        return 'تأثیر تخمینی: نامشخص'
    return f'{_text(item.get("impact_label") or "اهمیت خبری")} — تأثیر تخمینی: {fa_digits(int(score))}٪'


def render_market(items, *, max_items=8, budget=3500):
    if not items:
        return '📰 اخبار بازار\nدر داده‌های دریافت‌شده خبری برای نمایش نیست؛ ممکن است دریافت منبع ناموفق بوده باشد.'
    header='📰 *اخبار بازار*\nاخبار امروز و موارد با زمان انتشار نامشخص'
    blocks=[]
    for index,item in enumerate(items[:max_items],1):
        title=item.get('title_fa') or item.get('title') or 'عنوان نامشخص'
        note='\nعنوان اصلی؛ ترجمهٔ فارسی فعال یا در دسترس نیست.' if item.get('translation_status')=='original' else ''
        topics=item.get('related_to') or related_topics(item.get('title'), item.get('desc',''))
        if not isinstance(topics,(tuple,list)):topics=['نامشخص']
        block=f'*{fa_digits(index)}. {_text(title)}*{note}\n{_impact(item)}\n🏷 مرتبط با: {_text("، ".join(map(str,topics)),180)}\n🕒 زمان انتشار: {publication_label(item)}\n📰 منبع: {_text(item.get("source"),90)}'
        link=_safe_link(item.get('link'))
        if link:block+='\n🔗 [منبع]('+link+')'
        blocks.append(block)
    footer='⚠️ درصد تأثیر یک برآورد خبری است؛ احتمال رشد یا افت و میزان تغییر قیمت نیست. توصیهٔ مالی نیست.'
    return _fit(header,blocks,footer,budget=budget,omitted=max(0,len(items)-max_items))


def render_calendar(items, *, max_items=12, budget=3500, day=None):
    header='📅 *تقویم اقتصادی*'
    if day is not None:header+='\n'+date_label(day)
    header+='\nساعت‌های مشخص به وقت ایران؛ برنامه ممکن است تغییر کند.'
    if not items:
        return header+'\n\nدر داده‌های دریافت‌شده رویداد مهمی برای این روز یافت نشد؛ پوشش خوراک هفتگی ممکن است کامل نباشد.'
    ordered=sorted(items,key=lambda item:(str(calendar_local_date(item) or '9999-12-31'),str(_calendar_metadata(item).get('event_time_utc') or 'zz')))
    blocks=[];last_day=None
    for index,item in enumerate(ordered[:max_items],1):
        original=item.get('event_title') or str(item.get('title') or '').removeprefix(str(item.get('country') or '')+' - ')
        title,translated=calendar_title(original)
        local_day=calendar_local_date(item)
        block=''
        if local_day!=last_day or index==1:
            block+='📆 '+(date_label(local_day) if local_day else 'تاریخ مشخص نیست')+'\n';last_day=local_day
        block+=f'*{fa_digits(index)}. {_text(title)}*\n🌍 {_text(COUNTRIES.get(item.get("country"),item.get("country") or "کشور نامشخص"))} | {_text(item.get("country"),10)}'
        if not translated:block+='\nعنوان اصلی؛ ترجمهٔ استاندارد این رویداد هنوز موجود نیست.'
        block+='\n🕒 زمان اعلام: '+calendar_clock_label(item)
        if _calendar_metadata(item).get('time_status')!='scheduled':
            block+='\nتاریخ، تاریخ اعلام‌شدهٔ منبع است؛ ساعت محلی حدس زده نشده.'
        importance={'high':'🔴 اهمیت بالا','medium':'🟠 اهمیت متوسط','low':'🟢 اهمیت کم'}.get(str(item.get('raw_impact') or '').casefold(),'اهمیت: نامشخص')
        block+='\n'+importance
        for field,label in [('previous','قبلی'),('forecast','پیش‌بینی'),('actual','اعلام‌شده')]:
            value=item.get(field)
            if value is not None and str(value).strip():block+=f'\n{label}: {_text(fa_digits(value),80)}'
        blocks.append(block)
    footer='🔗 [تقویم اصلی](https://www.forexfactory.com/calendar)\n⚠️ احتمال افزایش نوسان وجود دارد؛ جهت حرکت بازار قطعی نیست.'
    return _fit(header,blocks,footer,budget=budget,omitted=max(0,len(ordered)-max_items))


def render_combined(items, *, max_items=8):
    selected=items[:max_items]
    market=[item for item in selected if item.get('category')!='فارکس']
    calendar=[item for item in selected if item.get('category')=='فارکس']
    if market and calendar:
        return render_market(market,max_items=max_items,budget=1749)+'\n\n'+render_calendar(calendar,max_items=max_items,budget=1749)
    if calendar:return render_calendar(calendar,max_items=max_items)
    return render_market(market,max_items=max_items)


def render_brief(items, *, max_items=2):
    selected=[item for item in items if type(item.get('impact')) in (int,float) and 55<=item['impact']<=100][:max_items]
    if not selected:return ''
    lines=['\n📰 *اخبار و رویدادهای مهم بازار:*']
    for item in selected:
        if item.get('category')=='فارکس':
            original=item.get('event_title') or str(item.get('title') or '').removeprefix(str(item.get('country') or '')+' - ')
            title,_=calendar_title(original); icon='📅';clock=calendar_clock_label(item)
        else:
            title=item.get('title_fa') or item.get('title');icon='📰';clock=publication_label(item)
        lines.append(f'{icon} {_text(title,85)} — {fa_digits(int(item["impact"]))}٪ تخمینی\n🕒 {clock}')
    lines.append('_برای جزئیات: /news؛ امتیاز خبر احتمال حرکت قیمت نیست._')
    return '\n'.join(lines)
