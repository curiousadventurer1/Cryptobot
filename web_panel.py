"""
پنل وب مدیریت ربات - بدون نیاز به دسترسی سرور یا ری‌دیپلوی
hamrah ba khod robot, tooye hamoon proses, roo ye port joda ejra mishe.
Railway be-soorat khodkar in port ro tashkhis mide o ye domain-e omoomi
barash misaze (chon az env variable makhsoos PORT estefade mikonim).

احراز هویت: HTTP Basic Auth ساده (username/password از .env)
⚠️ حتماً WEB_PANEL_PASSWORD رو یه مقدار قوی و غیرپیش‌فرض بذار، وگرنه
هرکسی که آدرس ربات رو پیدا کنه می‌تونه تنظیماتت رو عوض کنه یا کاربر
مسدود/رفع‌مسدود کنه.
"""
import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from html import escape as _html_escape
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Depends, HTTPException, status, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import database as db
import backup
import engine_config
from config import WEB_PANEL_USERNAME, WEB_PANEL_PASSWORD, BOT_TOKEN

logger = logging.getLogger(__name__)

# مستندات خودکار FastAPI (/docs، /redoc، /openapi.json) بدون احراز هویت در
# دسترس بودن و لیست کامل endpointها رو لو می‌دادن - خاموششون می‌کنیم.
app = FastAPI(title="پنل مدیریت ربات سیگنال", docs_url=None, redoc_url=None, openapi_url=None)
security = HTTPBasic()

if WEB_PANEL_PASSWORD and len(WEB_PANEL_PASSWORD) < 12:
    logger.warning("WEB_PANEL_PASSWORD کوتاهه (کمتر از ۱۲ کاراکتر) - یه رمز طولانی و تصادفی بذار.")


def h(value) -> str:
    """Escape برای هر مقدار متغیری که داخل HTML گذاشته می‌شه (جلوگیری از XSS)"""
    return _html_escape("" if value is None else str(value), quote=True)


# ---------- محدودیت تلاش ناموفق ورود (جلوگیری از brute-force) ----------

LOGIN_MAX_FAILURES = 10          # حداکثر تلاش ناموفق
LOGIN_WINDOW_SECONDS = 15 * 60   # در این بازه‌ی زمانی، برای هر IP
_failed_logins: dict[str, deque] = defaultdict(deque)


def _client_key(request: Request) -> str:
    # پشت پروکسی Railway، IP واقعی آخرین مقدار X-Forwarded-For هست
    # (مقدارهای قبلی رو خود کلاینت می‌تونه جعل کنه)
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _recent_failures(key: str) -> deque:
    q = _failed_logins[key]
    cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
    while q and q[0] < cutoff:
        q.popleft()
    if not q:
        _failed_logins.pop(key, None)
        q = _failed_logins[key]
    return q


async def verify_auth(request: Request, credentials: HTTPBasicCredentials = Depends(security)) -> str:
    if not WEB_PANEL_PASSWORD:
        # اگه رمز تنظیم نشده، پنل رو کلاً غیرفعال می‌کنیم (امن‌تر از پسورد خالی)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="پنل وب فعال نیست چون WEB_PANEL_PASSWORD تنظیم نشده."
        )

    key = _client_key(request)
    failures = _recent_failures(key)
    if len(failures) >= LOGIN_MAX_FAILURES:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="تلاش ناموفق زیاد بود. چند دقیقه بعد دوباره امتحان کن.",
        )

    # مقایسه روی bytes: compare_digest با رشته‌ی غیر ASCII خطا می‌ده (500)
    correct_user = secrets.compare_digest(credentials.username.encode("utf-8"), WEB_PANEL_USERNAME.encode("utf-8"))
    correct_pass = secrets.compare_digest(credentials.password.encode("utf-8"), WEB_PANEL_PASSWORD.encode("utf-8"))
    if not (correct_user and correct_pass):
        failures.append(time.monotonic())
        if len(_failed_logins) > 10_000:  # سقف حافظه
            _failed_logins.clear()
        logger.warning("ورود ناموفق به پنل وب از %s", key)
        await asyncio.sleep(0.5)  # کند کردن حدس‌زدن رمز
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="نام کاربری یا رمز اشتباهه",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ---------- محافظت CSRF ----------
# Basic Auth رو مرورگر خودکار روی هر درخواستی می‌فرسته، حتی اگه از یه سایت
# دیگه فرستاده شده باشه. بدون توکن CSRF، هر سایتی که ادمین بازش کنه
# می‌تونست بی‌صدا پیام همگانی بفرسته یا تنظیمات/مسدودی‌ها رو عوض کنه.
# توکن با یه کلید تصادفیِ هر بار اجرا ساخته می‌شه (بعد از ری‌استارت،
# صفحه‌ی باز رو یه بار رفرش کن).

_CSRF_SECRET = secrets.token_bytes(32)


def _csrf_token(username: str) -> str:
    return hmac.new(_CSRF_SECRET, username.encode("utf-8"), hashlib.sha256).hexdigest()


async def verify_post(request: Request, csrf_token: str = Form(""), user: str = Depends(verify_auth)) -> str:
    """برای همه‌ی درخواست‌های POST: احراز هویت + بررسی منشأ + توکن CSRF"""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="درخواست از سایت دیگه پذیرفته نمی‌شه.")
    if not hmac.compare_digest(csrf_token.encode("utf-8"), _csrf_token(user).encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="توکن امنیتی فرم نامعتبره. صفحه رو رفرش کن و دوباره امتحان کن.",
        )
    return user


_POST_FORM_RE = re.compile(r"(<form\b[^>]*\bmethod=['\"]post['\"][^>]*>)", re.IGNORECASE)


def _add_csrf_fields(body: str, user: str) -> str:
    field = f"<input type='hidden' name='csrf_token' value='{_csrf_token(user)}'>"
    return _POST_FORM_RE.sub(lambda m: m.group(1) + field, body)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    )
    response.headers.setdefault("Cache-Control", "no-store")
    return response


PAGE_STYLE = """
<style>
  * { box-sizing: border-box; }
  body { font-family: Tahoma, Segoe UI, sans-serif; direction: rtl; background:#0f1117; color:#e6e6e6; margin:0; padding:24px; }
  .container { max-width: 820px; margin: 0 auto; }
  h1 { color: #22c55e; font-size: 22px; }
  h3 { margin-top: 0; }
  .card { background:#1a1d27; border-radius:10px; padding:18px; margin-bottom:18px; border:1px solid #2a2d3a; }
  table { width:100%; border-collapse: collapse; }
  td, th { padding:10px; border-bottom:1px solid #2a2d3a; text-align:right; font-size:14px; }
  input, textarea { background:#0f1117; border:1px solid #3a3d4a; color:#e6e6e6; padding:10px; border-radius:6px; width:100%; margin-bottom:10px; font-family: inherit; }
  button { background:#22c55e; color:#0f1117; border:none; padding:10px 18px; border-radius:6px; cursor:pointer; font-weight:bold; }
  button.danger { background:#ef4444; color:white; }
  a.nav { color:#3b82f6; margin-left:18px; text-decoration:none; font-size:14px; }
  a.nav:hover { text-decoration:underline; }
  .stat { display:inline-block; background:#0f1117; padding:12px 18px; border-radius:8px; margin:4px; border:1px solid #2a2d3a; font-size:14px; }
  .hint { color:#888; font-size:13px; }
  code { background:#0f1117; padding:2px 6px; border-radius:4px; }
</style>
"""


def layout(title: str, body: str, user: str) -> str:
    body = _add_csrf_fields(body, user)
    return f"""<!DOCTYPE html>
<html lang="fa"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)} — پنل ربات</title>{PAGE_STYLE}</head>
<body><div class="container">
<h1>🤖 پنل مدیریت ربات سیگنال</h1>
<div>
  <a class="nav" href="/">📊 داشبورد</a>
  <a class="nav" href="/users">👥 کاربران</a>
  <a class="nav" href="/blocked">🚫 مسدودها</a>
  <a class="nav" href="/settings">⚙️ تنظیمات</a>
  <a class="nav" href="/backups">💾 بکاپ‌ها</a>
  <a class="nav" href="/broadcast">📢 پیام همگانی</a>
</div>
<hr style="border-color:#2a2d3a; margin:16px 0;">
{body}
</div></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard(user: str = Depends(verify_auth)):
    stats = await db.get_bot_stats()
    access_mode = await db.get_setting("access_mode", "open")
    mode_badge = "🔒 لیست سفید" if access_mode == "whitelist" else "🌐 باز برای همه"
    body = f"""
    <div class="card">
      <h3>آمار کلی</h3>
      <div class="stat">👥 کل کاربران: <b>{stats['total_users']}</b></div>
      <div class="stat">🔄 اسکن خودکار فعال: <b>{stats['active_autoscan']}</b></div>
      <div class="stat">⭐ آیتم واچ‌لیست: <b>{stats['total_watchlist']}</b></div>
      <div class="stat">🚫 کاربر مسدود: <b>{stats['total_blocked']}</b></div>
      <div class="stat">📈 سیگنال امروز: <b>{stats['signals_today']}</b></div>
      <div class="stat">حالت دسترسی: <b>{mode_badge}</b></div>
    </div>
    <div class="card hint">
      این پنل مستقیماً به همون دیتابیس ربات وصله. هر تغییری این‌جا بدی
      بلافاصله (بدون نیاز به ری‌استارت یا ری‌دیپلوی) روی ربات اعمال می‌شه.
    </div>
    """
    return layout("داشبورد", body, user)


PAGE_SIZE = 20


@app.get("/users", response_class=HTMLResponse)
async def users_page(user: str = Depends(verify_auth), page: int = 1):
    page = max(1, page)
    total = await db.get_user_total_count()
    offset = (page - 1) * PAGE_SIZE
    users = await db.get_users_page(limit=PAGE_SIZE, offset=offset)

    rows = ""
    for u in users:
        badges = ""
        if u["is_blocked"]:
            badges += "<span style='color:#ef4444'>🚫 مسدود</span> "
        if u["is_whitelisted"]:
            badges += "<span style='color:#22c55e'>✅ لیست‌سفید</span>"
        uname = h(u["username"] or "بدون‌نام")
        uid = h(u["user_id"])

        if u["is_whitelisted"]:
            wl_form = f"""<form method='post' action='/users/unwhitelist' style='margin:0'>
                <input type='hidden' name='user_id' value='{uid}'>
                <button class='danger' type='submit'>حذف از لیست سفید</button></form>"""
        else:
            wl_form = f"""<form method='post' action='/users/whitelist' style='margin:0'>
                <input type='hidden' name='user_id' value='{uid}'>
                <button type='submit'>افزودن به لیست سفید</button></form>"""

        rows += (
            f"<tr><td>{uid}</td><td>{uname}</td><td>{h((u['joined_at'] or '')[:10])}</td>"
            f"<td>{badges or '-'}</td><td>{wl_form}</td></tr>"
        )

    if not rows:
        rows = "<tr><td colspan='5' class='hint'>کاربری پیدا نشد.</td></tr>"

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    nav = ""
    if page > 1:
        nav += f"<a class='nav' href='/users?page={page-1}'>« قبلی</a>"
    if page < total_pages:
        nav += f"<a class='nav' href='/users?page={page+1}'>بعدی »</a>"

    body = f"""
    <div class="card">
      <h3>کاربرانی که ربات رو استارت کردن ({total})</h3>
      <p class="hint">صفحه {page} از {total_pages}</p>
      <table><tr><th>شناسه</th><th>یوزرنیم/نام</th><th>تاریخ عضویت</th><th>وضعیت</th><th></th></tr>{rows}</table>
      <div style="margin-top:12px">{nav}</div>
    </div>
    """
    return layout("کاربران", body, user)


@app.post("/users/whitelist")
async def whitelist_user_web(user_id: str = Form(...), user: str = Depends(verify_post)):
    cleaned = user_id.strip()
    if cleaned.lstrip("-").isdigit():
        await db.add_to_whitelist(int(cleaned), "از پنل وب")
    return RedirectResponse("/users", status_code=303)


@app.post("/users/unwhitelist")
async def unwhitelist_user_web(user_id: str = Form(...), user: str = Depends(verify_post)):
    cleaned = user_id.strip()
    if cleaned.lstrip("-").isdigit():
        await db.remove_from_whitelist(int(cleaned))
    return RedirectResponse("/users", status_code=303)


@app.get("/blocked", response_class=HTMLResponse)
async def blocked_page(user: str = Depends(verify_auth)):
    blocked = await db.get_blocked_users()
    rows = "".join(
        f"<tr><td>{h(b['user_id'])}</td><td>{h(b['reason'])}</td><td>{h((b['blocked_at'] or '')[:10])}</td>"
        f"<td><form method='post' action='/blocked/unblock' style='margin:0'>"
        f"<input type='hidden' name='user_id' value='{h(b['user_id'])}'>"
        f"<button class='danger' type='submit'>رفع مسدودی</button></form></td></tr>"
        for b in blocked
    )
    if not rows:
        rows = "<tr><td colspan='4' class='hint'>هیچ کاربری مسدود نیست.</td></tr>"

    body = f"""
    <div class="card">
      <h3>مسدودکردن کاربر جدید</h3>
      <form method="post" action="/blocked/block">
        <input name="user_id" placeholder="شناسه‌ی عددی کاربر (نه یوزرنیم)" required>
        <input name="reason" placeholder="دلیل (اختیاری)">
        <button type="submit">مسدود کن</button>
      </form>
      <p class="hint">شناسه‌ی عددی رو با فوروارد پیام کاربر به @userinfobot پیدا کن.</p>
    </div>
    <div class="card">
      <h3>کاربران مسدودشده ({len(blocked)})</h3>
      <table><tr><th>شناسه</th><th>دلیل</th><th>تاریخ</th><th></th></tr>{rows}</table>
    </div>
    """
    return layout("کاربران مسدود", body, user)


@app.post("/blocked/block")
async def block_user_web(user_id: str = Form(...), reason: str = Form(""), user: str = Depends(verify_post)):
    cleaned = user_id.strip()
    if cleaned.lstrip("-").isdigit():
        await db.block_user(int(cleaned), reason.strip() or "بدون دلیل ثبت‌شده (از پنل وب)")
    return RedirectResponse("/blocked", status_code=303)


@app.post("/blocked/unblock")
async def unblock_user_web(user_id: str = Form(...), user: str = Depends(verify_post)):
    cleaned = user_id.strip()
    if cleaned.lstrip("-").isdigit():
        await db.unblock_user(int(cleaned))
    return RedirectResponse("/blocked", status_code=303)


# هر آیتم: (کلید در bot_settings, برچسب فارسی, مقدار پیش‌فرض, توضیح کوتاه)
SETTINGS_SCHEMA = [
    ("max_watchlist_per_user", "حداکثر واچ‌لیست هر کاربر", "15", "عدد صحیح"),
    ("confidence_threshold_fraction", "آستانه‌ی صدور سیگنال قطعی", "0.25",
     "بین ۰ تا ۱ - هرچی بزرگ‌تر، سیگنال‌های کمتر ولی مطمئن‌تر"),
    ("atr_sl_mult", "ضریب ATR برای حد ضرر", "1.5", "عدد اعشاری"),
    ("rr_targets", "نسبت‌های ریسک‌به‌ریوارد TP1,TP2,TP3", "1.0,2.0,3.0", "سه عدد با کاما جدا"),
    # ── فاز ۳: دقت سیگنال (۱ = روشن، ۰ = خاموش) ──
    ("engine_closed_candles", "تحلیل فقط روی کندل‌های بسته‌شده", "1",
     "۱ یا ۰ - جلوی تغییر سیگنال وسط کندل (repaint) رو می‌گیره؛ سیگنال تا یک کندل دیرتر میاد"),
    ("engine_ohlc_tracking", "پایش سیگنال‌ها با سقف/کف کندل‌ها", "1",
     "۱ یا ۰ - لمس TP/SL بین دو بررسی دیگه جا نمی‌افته (کندل‌های ۵ دقیقه‌ای)"),
    ("engine_history_v2", "عملکرد تاریخی بر پایه‌ی میانگین R", "1",
     "۱ یا ۰ - با حداقل ۲۰ نمونه‌ی یکتا و تعدیل آماری؛ ۰ = روش قبلی (نرخ برد خام)"),
    ("engine_trend_block", "امتیازدهی «بلوک روند»", "0",
     "۱ یا ۰ - اندیکاتورهای هم‌خانواده‌ی روند یک رأی حساب می‌شن. تا وقتی ۰ هست فقط در لاگ (shadow-scoring) محاسبه می‌شه"),
    ("backtest_live_model", "بک‌تست با مدل پایش زنده", "1",
     "۱ یا ۰ - ورود لیمیت، انتظار برای پرشدن، بریک‌ایون بعد از TP1 و کارمزد؛ ۰ = مدل قدیمی"),
    ("fee_rate_pct", "کارمزد هر طرف معامله (درصد)", "0.1", "بین ۰ تا ۱ - مثلاً 0.1 یعنی ۰٫۱٪"),
    ("slippage_pct", "لغزش قیمت خروج بازار (درصد)", "0.05", "بین ۰ تا ۱ - برای خروج با SL/تایم‌اوت"),
    ("fee_filter_max_r", "فیلتر هزینه (حداکثر هزینه بر حسب R)", "0",
     "بین ۰ تا ۲ - سیگنالی که هزینه‌ی رفت‌وبرگشتش بیشتر از این باشه صادر نمی‌شه؛ ۰ = خاموش"),
    ("history_v2_since", "شروع تاریخچه‌ی معتبر عملکرد", "",
     "تاریخ YYYY-MM-DD یا ISO - فقط سیگنال‌های بعد از این تاریخ در «عملکرد تاریخی» حساب می‌شن"),
]

_FLAG_SETTING_KEYS = {
    "engine_closed_candles", "engine_ohlc_tracking", "engine_history_v2",
    "engine_trend_block", "backtest_live_model",
}
_PERCENT_SETTING_RANGES = {"fee_rate_pct": 1.0, "slippage_pct": 1.0, "fee_filter_max_r": 2.0}


@app.get("/backups", response_class=HTMLResponse)
async def backups_page(user: str = Depends(verify_auth)):
    backups = backup.list_backups()
    rows = ""
    for b in backups:
        size_str = f"{b['size_kb']:.1f} KB" if b["size_kb"] < 1024 else f"{b['size_kb']/1024:.1f} MB"
        date_str = b["created_at"].strftime("%Y-%m-%d %H:%M")
        rows += (
            f"<tr><td>{h(b['filename'])}</td><td>{size_str}</td><td>{date_str}</td>"
            f"<td><a class='nav' href='/backups/download/{h(quote(b['filename']))}'>⬇️ دانلود</a></td></tr>"
        )
    if not rows:
        rows = "<tr><td colspan='4' class='hint'>هنوز هیچ بکاپی ساخته نشده.</td></tr>"

    body = f"""
    <div class="card">
      <h3>بکاپ‌گیری دستی</h3>
      <p class="hint">علاوه بر بکاپ خودکار دوره‌ای (هر چند ساعت یه‌بار)، می‌تونی همین الان یه بکاپ فوری بسازی.</p>
      <form method="post" action="/backups/create">
        <button type="submit">📸 ساخت بکاپ الان</button>
      </form>
    </div>
    <div class="card">
      <h3>بکاپ‌های موجود ({len(backups)})</h3>
      <table><tr><th>فایل</th><th>حجم</th><th>تاریخ</th><th></th></tr>{rows}</table>
    </div>
    """
    return layout("بکاپ‌ها", body, user)


@app.post("/backups/create")
async def create_backup_web(user: str = Depends(verify_post)):
    try:
        await backup.create_backup()
    except Exception:
        pass  # صفحه‌ی بعدی هرحال لیست به‌روز رو نشون می‌ده؛ اگه واقعاً fail شده باشه، لیست تغییری نمی‌کنه
    return RedirectResponse("/backups", status_code=303)


@app.get("/backups/download/{filename}")
async def download_backup_web(filename: str, user: str = Depends(verify_auth)):
    path = backup.get_backup_path(filename)
    if not path:
        raise HTTPException(status_code=404, detail="فایل بکاپ پیدا نشد")
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(user: str = Depends(verify_auth), error: str = ""):
    current = await db.get_all_settings()
    forms = ""
    labels = {k: label for k, label, _, _ in SETTINGS_SCHEMA}
    if error in labels:
        forms += (f"<p style='color:#ef4444'>❌ مقدار «{h(labels[error])}» نامعتبر بود و ذخیره نشد. "
                  f"راهنمای زیر هر فیلد رو ببین.</p>")
    for key, label, default, hint in SETTINGS_SCHEMA:
        value = current.get(key, default)
        forms += f"""
        <div style="margin-bottom:16px; padding-bottom:16px; border-bottom:1px solid #2a2d3a;">
          <label><b>{label}</b> <code>{key}</code></label>
          <p class="hint">{hint}</p>
          <form method="post" action="/settings/update" style="display:flex; gap:8px; align-items:center;">
            <input name="key" value="{key}" type="hidden">
            <input name="value" value="{h(value)}" style="margin-bottom:0;">
            <button type="submit">ذخیره</button>
          </form>
        </div>
        """

    access_mode = current.get("access_mode", "open")
    open_checked = "checked" if access_mode == "open" else ""
    wl_checked = "checked" if access_mode == "whitelist" else ""

    welcome_msg = current.get("custom_welcome_message", "")

    body = f"""
    <div class="card">
      <h3>🔒 کنترل دسترسی</h3>
      <p class="hint">
        در حالت «باز»، هر کسی می‌تونه از ربات استفاده کنه (مگر مسدودشده‌ها).
        در حالت «لیست سفید»، فقط کاربرانی که تاییدشون کردی دسترسی دارن؛
        بقیه فقط پیام «منتظر تایید» می‌گیرن. مدیریت لیست سفید از صفحه‌ی
        <a class="nav" href="/users">کاربران</a> انجام می‌شه.
      </p>
      <form method="post" action="/settings/access-mode">
        <label><input type="radio" name="mode" value="open" {open_checked}> باز برای همه</label><br>
        <label><input type="radio" name="mode" value="whitelist" {wl_checked}> فقط لیست سفید</label><br><br>
        <button type="submit">ذخیره حالت دسترسی</button>
      </form>
    </div>

    <div class="card">
      <h3>👋 پیام خوش‌آمدگویی سفارشی</h3>
      <p class="hint">این پیام، قبل از راهنمای استاندارد، به هر کسی که /start بزنه نشون داده می‌شه.</p>
      <form method="post" action="/settings/welcome">
        <textarea name="message" rows="3" placeholder="مثلاً: به کانال ما هم سر بزن...">{h(welcome_msg)}</textarea>
        <button type="submit">ذخیره پیام</button>
      </form>
      <form method="post" action="/settings/welcome/delete" style="margin-top:8px">
        <button class="danger" type="submit">حذف پیام سفارشی (برگشت به پیش‌فرض)</button>
      </form>
    </div>

    <div class="card">
      <h3>تنظیمات موتور سیگنال</h3>
      <p class="hint">این‌ها بدون نیاز به ری‌دیپلوی، بلافاصله روی تحلیل‌های بعدی اثر می‌ذارن.</p>
      {forms}
    </div>
    """
    return layout("تنظیمات", body, user)


def _validate_setting(key: str, raw: str) -> str | None:
    """
    مقدار تنظیمات موتور رو اعتبارسنجی و نرمال می‌کنه؛ اگه نامعتبر باشه None.
    (قبلاً هر متنی ذخیره می‌شد - مثلاً nan یا عدد منفی - و موتور سیگنال رو خراب می‌کرد)
    """
    import math
    raw = raw.strip()
    try:
        if key == "max_watchlist_per_user":
            v = int(raw)
            return str(v) if 1 <= v <= 500 else None
        if key == "confidence_threshold_fraction":
            v = float(raw)
            return str(v) if math.isfinite(v) and 0 < v < 1 else None
        if key == "atr_sl_mult":
            v = float(raw)
            return str(v) if math.isfinite(v) and 0.1 <= v <= 10 else None
        if key == "rr_targets":
            parts = [float(x) for x in raw.split(",")]
            if (len(parts) == 3 and all(math.isfinite(x) and 0 < x <= 20 for x in parts)
                    and parts[0] < parts[1] < parts[2]):
                return ",".join(f"{x:g}" for x in parts)
            return None
        if key in _FLAG_SETTING_KEYS:
            v = raw.lower()
            if v in ("1", "true", "on", "yes"):
                return "1"
            if v in ("0", "false", "off", "no"):
                return "0"
            return None
        if key in _PERCENT_SETTING_RANGES:
            v = float(raw)
            return f"{v:g}" if math.isfinite(v) and 0 <= v <= _PERCENT_SETTING_RANGES[key] else None
        if key == "history_v2_since":
            from datetime import datetime
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is not None:
                return None  # بقیه‌ی تاریخ‌های دیتابیس UTC و بدون منطقه‌ی زمانی‌ان
            return parsed.isoformat()
    except ValueError:
        return None
    return None


@app.post("/settings/update")
async def update_setting_web(key: str = Form(...), value: str = Form(...), user: str = Depends(verify_post)):
    valid_keys = {k for k, _, _, _ in SETTINGS_SCHEMA}
    if key in valid_keys:
        cleaned = _validate_setting(key, value)
        if cleaned is None:
            return RedirectResponse(f"/settings?error={quote(key)}", status_code=303)
        await db.set_setting(key, cleaned)
        engine_config.invalidate_cache()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/access-mode")
async def update_access_mode_web(mode: str = Form(...), user: str = Depends(verify_post)):
    if mode in ("open", "whitelist"):
        await db.set_setting("access_mode", mode)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/welcome")
async def update_welcome_web(message: str = Form(...), user: str = Depends(verify_post)):
    await db.set_setting("custom_welcome_message", message.strip())
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/welcome/delete")
async def delete_welcome_web(user: str = Depends(verify_post)):
    await db.set_setting("custom_welcome_message", "")
    return RedirectResponse("/settings", status_code=303)


@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(user: str = Depends(verify_auth)):
    user_count = len(await db.get_all_user_ids())
    body = f"""
    <div class="card">
      <h3>ارسال پیام همگانی</h3>
      <p class="hint">این پیام برای هر {user_count} کاربر ثبت‌شده‌ی ربات ارسال می‌شه.</p>
      <form method="post" action="/broadcast/send">
        <textarea name="message" rows="4" placeholder="متن پیام..." required></textarea>
        <button type="submit">ارسال به همه</button>
      </form>
    </div>
    """
    return layout("پیام همگانی", body, user)


@app.post("/broadcast/send", response_class=HTMLResponse)
async def send_broadcast_web(message: str = Form(...), user: str = Depends(verify_post)):
    user_ids = await db.get_all_user_ids()
    sent, failed = 0, 0
    async with httpx.AsyncClient(timeout=10) as client:
        for uid in user_ids:
            try:
                resp = await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": uid, "text": f"📢 {message}"}
                )
                if resp.status_code == 200:
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)  # رعایت محدودیت نرخ ارسال تلگرام

    body = f"""
    <div class="card">
      ✅ ارسال شد به <b>{sent}</b> کاربر (ناموفق: {failed})
      <br><br><a class="nav" href="/broadcast">بازگشت</a>
    </div>
    """
    return layout("پیام همگانی", body, user)
