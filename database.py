"""
لایه دیتابیس - مدیریت کاربران، واچ‌لیست و تاریخچه سیگنال‌ها
از aiosqlite استفاده می‌کنیم چون ربات async هست
"""
import aiosqlite
import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from config import DB_PATH
from trade_validation import validate_trade_geometry

logger = logging.getLogger(__name__)
MONITOR_QUEUE_STATE_KEY = "_signal_performance_queue"
SIGNAL_SOURCE_SINGLE_TIMEFRAME = "single_timeframe"
SIGNAL_SOURCE_MULTI_TIMEFRAME = "multi_timeframe"
SIGNAL_HISTORY_SOURCES = (
    SIGNAL_SOURCE_SINGLE_TIMEFRAME, SIGNAL_SOURCE_MULTI_TIMEFRAME
)
NEWS_DELIVERY_LEASE_SECONDS = 10 * 60


async def init_db():
    """ساخت جداول و مهاجرت افزایشی پایش/تاریخچه، بدون بازنویسی رکوردها."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                joined_at TEXT,
                auto_scan_enabled INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                symbol TEXT,
                added_at TEXT,
                UNIQUE(user_id, symbol)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS signal_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                signal_type TEXT,
                score INTEGER,
                price REAL,
                created_at TEXT,
                source TEXT
            )
        """)
        # منبع سوابق قدیمی معلوم نیست؛ NULL می‌ماند و به‌حدس برچسب نمی‌خورد.
        cursor = await db.execute("PRAGMA table_info(signal_history)")
        history_columns = {row[1] for row in await cursor.fetchall()}
        if "source" not in history_columns:
            await db.execute("ALTER TABLE signal_history ADD COLUMN source TEXT")
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_signal_history_symbol_source_id
            ON signal_history (symbol, source, id)
        """)
        # کاربران مسدودشده توسط ادمین
        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                blocked_at TEXT
            )
        """)
        # تنظیمات مدیریت ریسک هر کاربر (برای محاسبه‌ی خودکار حجم پوزیشن)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_risk_settings (
                user_id INTEGER PRIMARY KEY,
                account_balance REAL,
                risk_percent REAL,
                updated_at TEXT
            )
        """)
        # کاربران تاییدشده برای دسترسی به ربات - وقتی access_mode روی
        # whitelist باشه، فقط این‌ها (و ادمین‌ها) می‌تونن از ربات استفاده کنن
        await db.execute("""
            CREATE TABLE IF NOT EXISTS whitelist_users (
                user_id INTEGER PRIMARY KEY,
                note TEXT,
                added_at TEXT
            )
        """)
        # درخواست‌های دسترسی در انتظار تایید ادمین - برای جلوگیری از
        # اسپم نوتیفیکیشن (هر کاربر فقط یه‌بار توی این جدول ثبت می‌شه،
        # حتی اگه چندبار /start بزنه، تا وقتی تایید/رد بشه)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS access_requests (
                user_id INTEGER PRIMARY KEY,
                requested_at TEXT
            )
        """)
        # پایش عملکرد سیگنال‌های صادرشده از موتور تک‌تایم‌فریمی (برای /mystats)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS signal_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                symbol TEXT,
                timeframe TEXT,
                direction TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                tp3 REAL,
                status TEXT DEFAULT 'OPEN',
                created_at TEXT,
                closed_at TEXT,
                entry_triggered_at TEXT,
                entry_observed_price REAL
            )
        """)
        # مهاجرت افزایشی؛ وضعیت/قیمت رکوردهای قدیمی را بازنویسی نمی‌کنیم.
        cursor = await db.execute("PRAGMA table_info(signal_performance)")
        columns = {row[1] for row in await cursor.fetchall()}
        for name, sql_type in (
            ("entry_triggered_at", "TEXT"), ("entry_observed_price", "REAL")
        ):
            if name not in columns:
                await db.execute(f"ALTER TABLE signal_performance ADD COLUMN {name} {sql_type}")
        # سیگنال‌هایی که منتظر تاییدیه‌ی کاربرن (بله/خیر پیگیری شه)؛ بعد
        # از پاسخ کاربر، یا به signal_performance منتقل می‌شن یا حذف می‌شن
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                symbol TEXT,
                timeframe TEXT,
                direction TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                tp3 REAL,
                created_at TEXT
            )
        """)
        # تنظیمات سراسری ربات (خوانده و نوشته‌شده هم از پنل وب، هم از کد)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_news_settings (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                auto_enabled INTEGER DEFAULT 0
            )
        """)
        # دفتر ارسال خبر خودکار؛ سوابق قبلی قابل حدس نیستند و backfill ندارند.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS news_delivery (
                user_id INTEGER NOT NULL,
                news_key TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('reserved', 'sent')),
                claim_token TEXT,
                lease_until REAL,
                sent_at TEXT,
                PRIMARY KEY (user_id, news_key),
                CHECK (
                    (status = 'reserved' AND claim_token IS NOT NULL
                     AND lease_until IS NOT NULL AND sent_at IS NULL)
                    OR (status = 'sent' AND sent_at IS NOT NULL)
                )
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_news_delivery_reserved_claim
            ON news_delivery (user_id, claim_token) WHERE status = 'reserved'
        """)
        await db.commit()


def _validated_news_delivery_keys(user_id, news_keys) -> list[str]:
    if type(user_id) is not int or not 0 < user_id <= 9223372036854775807:
        raise ValueError("Invalid news delivery user")
    if not isinstance(news_keys, (list, tuple)) or len(news_keys) > 50:
        raise ValueError("Invalid news delivery keys")
    if any(not isinstance(key, str) or len(key) != 64
           or any(character not in "0123456789abcdef" for character in key)
           for key in news_keys):
        raise ValueError("Invalid news identity")
    return list(dict.fromkeys(news_keys))


def _validated_news_delivery_claim(user_id, token, news_keys) -> list[str]:
    keys = _validated_news_delivery_keys(user_id, news_keys)
    if (not isinstance(token, str) or len(token) != 32
            or any(character not in "0123456789abcdef" for character in token)
            or not keys or len(keys) > 3):
        raise ValueError("Invalid news delivery claim")
    return keys


async def claim_news_delivery(user_id: int, news_keys, *, limit: int = 3) -> dict | None:
    """Atomic per-user reservation; expired unsent claims can be retried."""
    keys = _validated_news_delivery_keys(user_id, news_keys)
    if type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError("Invalid news batch limit")
    if not keys:
        return None
    async with aiosqlite.connect(DB_PATH) as connection:
        try:
            await connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc).timestamp()  # after acquiring the lock
            token = uuid.uuid4().hex
            selected = []
            for key in keys:
                cursor = await connection.execute("""
                    INSERT INTO news_delivery
                        (user_id, news_key, status, claim_token, lease_until, sent_at)
                    VALUES (?, ?, 'reserved', ?, ?, NULL)
                    ON CONFLICT(user_id, news_key) DO UPDATE SET
                        claim_token=excluded.claim_token,
                        lease_until=excluded.lease_until
                    WHERE news_delivery.status='reserved'
                        AND news_delivery.sent_at IS NULL
                        AND news_delivery.lease_until <= ?
                """, (user_id, key, token, now + NEWS_DELIVERY_LEASE_SECONDS, now))
                if cursor.rowcount == 1:
                    selected.append(key)
                if len(selected) == limit:
                    break
            if not selected:
                await connection.rollback()
                return None
            await connection.commit()
            return {"token": token, "news_keys": selected}
        except BaseException:
            await connection.rollback()
            raise


async def is_news_delivery_claim_current(user_id: int, token: str, news_keys) -> bool:
    """Reject expired/replaced/partial claims before network sending."""
    keys = _validated_news_delivery_claim(user_id, token, news_keys)
    async with aiosqlite.connect(DB_PATH) as connection:
        cursor = await connection.execute("""
            SELECT news_key, lease_until FROM news_delivery
            WHERE user_id=? AND claim_token=? AND status='reserved'
        """, (user_id, token))
        rows = await cursor.fetchall()
    now = datetime.now(timezone.utc).timestamp()
    return (set(row[0] for row in rows) == set(keys)
            and all(isinstance(row[1], (int, float)) and math.isfinite(row[1])
                    and row[1] > now for row in rows))


async def _finish_news_delivery(user_id: int, token: str, news_keys, *, sent: bool) -> bool:
    """Commit or release the complete owned batch; never touch another owner."""
    keys = _validated_news_delivery_claim(user_id, token, news_keys)
    async with aiosqlite.connect(DB_PATH) as connection:
        try:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute("""
                SELECT news_key FROM news_delivery
                WHERE user_id=? AND claim_token=? AND status='reserved'
            """, (user_id, token))
            rows = await cursor.fetchall()
            if set(row[0] for row in rows) != set(keys):
                await connection.rollback()
                return False
            if sent:
                # Success can be recorded after expiry if no other owner reclaimed.
                cursor = await connection.execute("""
                    UPDATE news_delivery SET status='sent', sent_at=?,
                        claim_token=NULL, lease_until=NULL
                    WHERE user_id=? AND claim_token=? AND status='reserved'
                """, (datetime.now(timezone.utc).isoformat(), user_id, token))
            else:
                cursor = await connection.execute("""
                    DELETE FROM news_delivery
                    WHERE user_id=? AND claim_token=? AND status='reserved'
                """, (user_id, token))
            if cursor.rowcount != len(keys):
                raise aiosqlite.IntegrityError("Incomplete news delivery transition")
            await connection.commit()
            return True
        except BaseException:
            await connection.rollback()
            raise


async def mark_news_delivery_sent(user_id: int, token: str, news_keys) -> bool:
    """Call only after Telegram returned success; not a read receipt."""
    return await _finish_news_delivery(user_id, token, news_keys, sent=True)


async def release_news_delivery_claim(user_id: int, token: str, news_keys) -> bool:
    """Known failed/abandoned attempt may be retried; stale owners cannot release."""
    return await _finish_news_delivery(user_id, token, news_keys, sent=False)


async def add_user(user_id: int, username: str) -> bool:
    """
    ثبت کاربر - اگه قبلاً وجود داشته باشه، فقط یوزرنیمش بروزرسانی می‌شه
    (چون ممکنه کاربر یوزرنیمش رو عوض کرده باشه)، تاریخ عضویت اولیه‌ش
    دست‌نخورده می‌مونه. خروجی: True اگه این اولین باری بود که این کاربر
    دیده می‌شد (کاربر تازه)، False اگه از قبل وجود داشت.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        is_new = await cursor.fetchone() is None
        await db.execute(
            """INSERT INTO users (user_id, username, joined_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username""",
            (user_id, username, datetime.utcnow().isoformat())
        )
        await db.commit()
        return is_new


async def add_to_watchlist(user_id: int, symbol: str) -> bool:
    """برمی‌گردونه True اگه با موفقیت اضافه شد"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO watchlist (user_id, symbol, added_at) VALUES (?, ?, ?)",
                (user_id, symbol, datetime.utcnow().isoformat())
            )
            await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False  # از قبل توی لیست بوده


async def remove_from_watchlist(user_id: int, symbol: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND symbol = ?",
            (user_id, symbol)
        )
        await db.commit()


async def get_watchlist(user_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT symbol FROM watchlist WHERE user_id = ?", (user_id,)
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def get_watchlist_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM watchlist WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_all_active_watch_pairs() -> list[tuple[int, str]]:
    """همه‌ی جفت (user_id, symbol) کاربرانی که auto_scan فعاله"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT w.user_id, w.symbol
            FROM watchlist w
            JOIN users u ON u.user_id = w.user_id
            WHERE u.auto_scan_enabled = 1
        """)
        return await cursor.fetchall()


async def set_auto_scan(user_id: int, enabled: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET auto_scan_enabled = ? WHERE user_id = ?",
            (1 if enabled else 0, user_id)
        )
        await db.commit()


async def log_signal(symbol: str, signal_type: str, score: int, price: float, *, source: str):
    """ثبت جدید فقط با منبع صریح؛ امتیاز و نوع سیگنال بازتفسیر نمی‌شوند."""
    if not isinstance(source, str) or source not in SIGNAL_HISTORY_SOURCES:
        raise ValueError("Unknown signal history source")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO signal_history (symbol, signal_type, score, price, created_at, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (symbol, signal_type, score, price, datetime.utcnow().isoformat(), source)
        )
        await db.commit()


async def get_last_signal(symbol: str, *, source: str | None = None) -> dict | None:
    """
    آخرین رکورد نماد در یک موتور؛ NULLهای قدیمی در خواندن منبع‌دار نیستند.
    source=None خواندن تجمیعیِ سازگار است، نه مبنای تشخیص تغییر در اسکن.
    """
    if source is not None and (
        not isinstance(source, str) or source not in SIGNAL_HISTORY_SOURCES
    ):
        raise ValueError("Unknown signal history source")
    query = "SELECT signal_type, score, created_at FROM signal_history WHERE symbol = ?"
    params = (symbol,)
    if source is not None:
        query += " AND source = ?"
        params += (source,)
    query += " ORDER BY id DESC LIMIT 1"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
        if row:
            return {"signal_type": row[0], "score": row[1], "created_at": row[2]}
        return None


# ==================== مدیریت کاربران مسدود (پنل ادمین) ====================

async def block_user(user_id: int, reason: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO blocked_users (user_id, reason, blocked_at) VALUES (?, ?, ?)",
            (user_id, reason, datetime.utcnow().isoformat())
        )
        await db.commit()


async def unblock_user(user_id: int) -> bool:
    """برمی‌گردونه True اگه واقعاً مسدود بوده و حذف شد"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,))
        existed = await cursor.fetchone() is not None
        await db.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))
        await db.commit()
        return existed


async def is_user_blocked(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,))
        return await cursor.fetchone() is not None


async def get_background_notification_recipients(user_ids, kind: str,
                                                 symbol: str | None = None) -> set[int]:
    """
    سیاست مشترک اعلان پس‌زمینه؛ هر SELECT سیاست و تنظیمات را با هم می‌خواند.
    مسدودی بر نقش ادمین/لیست‌سفید اولویت دارد. نبود سیاست = open؛ مقدار
    ناشناس/NULL برای غیرادمین مجوز نیست. خطای DB به caller منتقل می‌شود
    تا به‌جای fallback مجاز، ارسال را متوقف کند. نتایج کش نمی‌شوند.
    """
    preferences = {
        "performance": "1",
        "scan": """EXISTS (SELECT 1 FROM users u
                          WHERE u.user_id = r.user_id AND u.auto_scan_enabled = 1)""",
        # enabled فقط نمایش زیر سیگنال است، نه کلید ارسال خبر خودکار.
        "news": """EXISTS (SELECT 1 FROM user_news_settings n
                          WHERE n.user_id = r.user_id AND n.auto_enabled = 1)""",
        "backup": "r.is_admin = 1",
    }
    if kind not in preferences:
        return set()
    ids = list(dict.fromkeys(
        uid for uid in user_ids
        if type(uid) is int and 0 < uid <= 9223372036854775807
    ))
    if not ids:
        return set()
    from config import ADMIN_IDS
    admins = set(ADMIN_IDS)
    condition = preferences[kind]
    check_watch = kind == "scan" and symbol is not None
    if check_watch:
        condition += """ AND EXISTS (SELECT 1 FROM watchlist w
                                    WHERE w.user_id = r.user_id AND w.symbol = ?)"""
    allowed = set()
    async with aiosqlite.connect(DB_PATH) as connection:
        # دو پارامتر برای هر گیرنده؛ زیر سقف قدیمی ۹۹۹ پارامتر SQLite.
        for offset in range(0, len(ids), 400):
            chunk = ids[offset:offset + 400]
            values = ",".join("(?, ?)" for _ in chunk)
            params = [value for uid in chunk for value in (uid, int(uid in admins))]
            if check_watch:
                params.append(symbol)
            cursor = await connection.execute(
                f"""WITH recipients(user_id, is_admin) AS (VALUES {values}),
                    policy(mode) AS (
                        SELECT value FROM bot_settings WHERE key = 'access_mode'
                        UNION ALL
                        SELECT 'open' WHERE NOT EXISTS (
                            SELECT 1 FROM bot_settings WHERE key = 'access_mode'
                        )
                    )
                    SELECT r.user_id FROM recipients r CROSS JOIN policy p
                    WHERE NOT EXISTS (
                        SELECT 1 FROM blocked_users b WHERE b.user_id = r.user_id
                    )
                    AND (
                        r.is_admin = 1 OR p.mode = 'open'
                        OR (p.mode = 'whitelist' AND EXISTS (
                            SELECT 1 FROM whitelist_users w WHERE w.user_id = r.user_id
                        ))
                    )
                    AND ({condition})""",
                params
            )
            allowed.update(row[0] for row in await cursor.fetchall())
    return allowed


async def can_receive_background_notification(user_id: int, kind: str,
                                               symbol: str | None = None) -> bool:
    """بررسی تازه پیش از ارسال؛ تراکنش مشترک با شبکه/تلگرام ایجاد نمی‌کند."""
    allowed = await get_background_notification_recipients([user_id], kind, symbol)
    return type(user_id) is int and user_id in allowed


async def get_blocked_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id, reason, blocked_at FROM blocked_users ORDER BY blocked_at DESC"
        )
        rows = await cursor.fetchall()
        return [{"user_id": r[0], "reason": r[1], "blocked_at": r[2]} for r in rows]


# ==================== آمار کلی ربات (پنل ادمین) ====================

async def get_bot_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        total_users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        active_autoscan = (await (await db.execute(
            "SELECT COUNT(*) FROM users WHERE auto_scan_enabled = 1"
        )).fetchone())[0]
        total_watchlist = (await (await db.execute("SELECT COUNT(*) FROM watchlist")).fetchone())[0]
        total_blocked = (await (await db.execute("SELECT COUNT(*) FROM blocked_users")).fetchone())[0]
        today = datetime.utcnow().strftime("%Y-%m-%d")
        signals_today = (await (await db.execute(
            "SELECT COUNT(*) FROM signal_history WHERE created_at LIKE ?", (f"{today}%",)
        )).fetchone())[0]
        return {
            "total_users": total_users,
            "active_autoscan": active_autoscan,
            "total_watchlist": total_watchlist,
            "total_blocked": total_blocked,
            "signals_today": signals_today,
        }


async def get_all_user_ids() -> list[int]:
    """برای ارسال پیام همگانی (broadcast)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users")
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


# ==================== تنظیمات مدیریت ریسک هر کاربر ====================

async def set_user_risk(user_id: int, account_balance: float, risk_percent: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO user_risk_settings (user_id, account_balance, risk_percent, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   account_balance=excluded.account_balance,
                   risk_percent=excluded.risk_percent,
                   updated_at=excluded.updated_at""",
            (user_id, account_balance, risk_percent, datetime.utcnow().isoformat())
        )
        await db.commit()


async def get_user_risk(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT account_balance, risk_percent FROM user_risk_settings WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        if row:
            return {"account_balance": row[0], "risk_percent": row[1]}
        return None


# ==================== پایش عملکرد سیگنال‌ها ====================

def is_valid_price(price) -> bool:
    """قیمت عددیِ مثبت و متناهی؛ داده‌ی نامعتبر وارد مدل پایش نشود."""
    try:
        return not isinstance(price, bool) and math.isfinite(price) and price > 0
    except (TypeError, ValueError, OverflowError):
        return False


async def _insert_signal_performance(connection, user_id: int, symbol: str, timeframe: str,
                                     direction: str, entry: float, sl: float, tps: list) -> int:
    """درج بدون commit؛ مالک تراکنش، ثبت و rollback را کنترل می‌کند."""
    entry, sl, targets = validate_trade_geometry(direction, entry, sl, tps)
    tp1, tp2, tp3 = targets
    cursor = await connection.execute(
        """INSERT INTO signal_performance
           (user_id, symbol, timeframe, direction, entry, sl, tp1, tp2, tp3, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'WAITING_ENTRY', ?)""",
        (user_id, symbol, timeframe, direction, entry, sl, tp1, tp2, tp3, datetime.utcnow().isoformat())
    )
    return cursor.lastrowid


async def record_signal_performance(user_id: int, symbol: str, timeframe: str, direction: str,
                                     entry: float, sl: float, tps: list) -> int:
    """ثبت مستقل در انتظار ورود؛ تایید pending باید از decide_pending_signal بگذرد."""
    entry, sl, tps = validate_trade_geometry(direction, entry, sl, tps)
    async with aiosqlite.connect(DB_PATH) as connection:
        perf_id = await _insert_signal_performance(
            connection, user_id, symbol, timeframe, direction, entry, sl, tps
        )
        await connection.commit()
        return perf_id


async def activate_signal_entry(perf_id: int, observed_price: float) -> bool:
    """ورود فرضی با مشاهده‌ی قیمت در محدوده؛ تنها یک بار و به‌صورت اتمیک."""
    if not is_valid_price(observed_price):
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """UPDATE signal_performance
               SET status = 'OPEN', entry_triggered_at = :observed_at,
                   entry_observed_price = :price
               WHERE id = :id AND status = 'WAITING_ENTRY'
                 AND ((direction = 'BUY' AND sl < entry AND :price <= entry AND :price > sl)
                   OR (direction = 'SELL' AND sl > entry AND :price >= entry AND :price < sl))""",
            {"id": perf_id, "price": observed_price, "observed_at": datetime.utcnow().isoformat()}
        )
        await db.commit()
        return cursor.rowcount == 1


async def invalidate_waiting_entry(perf_id: int, observed_price: float) -> bool:
    """مشاهده‌ی SL قبل از ورود: پایان پایش، بدون ثبت برد/باخت معامله."""
    if not is_valid_price(observed_price):
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """UPDATE signal_performance
               SET status = 'ENTRY_INVALIDATED', closed_at = :observed_at
               WHERE id = :id AND status = 'WAITING_ENTRY'
                 AND ((direction = 'BUY' AND sl < entry AND :price <= sl)
                   OR (direction = 'SELL' AND sl > entry AND :price >= sl))""",
            {"id": perf_id, "price": observed_price, "observed_at": datetime.utcnow().isoformat()}
        )
        await db.commit()
        return cursor.rowcount == 1


async def get_open_signal_performances(limit: int = 200) -> list[dict]:
    """
    سیگنال‌های در انتظار ورود و فعال؛ ابطال پیش از ورود از پایش خارج است.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT id, user_id, symbol, timeframe, direction, entry, sl, tp1, tp2, tp3, status
               FROM signal_performance
               WHERE status NOT IN ('TP3_HIT', 'SL_HIT', 'BREAKEVEN_HIT', 'ENTRY_INVALIDATED')
               ORDER BY id ASC LIMIT ?""",
            (limit,)
        )
        rows = await cursor.fetchall()
        cols = ["id", "user_id", "symbol", "timeframe", "direction", "entry", "sl", "tp1", "tp2", "tp3", "status"]
        return [dict(zip(cols, r)) for r in rows]


def _parse_monitor_queue_state(raw, max_sequence: int) -> dict | None:
    """نسخه/نوع/بازه‌ی نشانگر داخلی؛ سقف چرخه باید شناسه‌ی قبلاً ساخته‌شده باشد."""
    try:
        state = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    if (
        type(state) is not dict
        or set(state) != {"version", "last_id", "upper_id"}
        or type(state["version"]) is not int or state["version"] != 1
        or type(state["last_id"]) is not int
        or type(state["upper_id"]) is not int
        or not 0 <= state["last_id"] <= state["upper_id"] <= 9223372036854775807
        or state["upper_id"] == 0
        or state["upper_id"] > max_sequence
    ):
        return None
    return state


async def claim_signal_performance_batch(limit: int = 200) -> list[dict]:
    """
    رزرو دسته‌ی بعدیِ چرخه‌ی محدود؛ نشانگر، انتخاب برای تلاش است نه موفقیت پایش.
    سقف شناسه‌ی چرخه ثابت می‌ماند تا ورود مداوم داده چرخه را تمدید نکند.
    نشانگر با انتخاب دسته در یک تراکنش ذخیره می‌شود؛ شبکه خارج تراکنش است.
    read-only قدیمی و وضعیت/SL/سوابق سیگنال‌ها توسط این تابع تغییر نمی‌کنند.
    """
    if type(limit) is not int or not 0 < limit <= 9223372036854775807:
        raise ValueError("Monitoring batch limit must be a positive SQLite integer")
    eligible = "status NOT IN ('TP3_HIT', 'SL_HIT', 'BREAKEVEN_HIT', 'ENTRY_INVALIDATED')"
    select_batch = f"""SELECT id, user_id, symbol, timeframe, direction,
                             entry, sl, tp1, tp2, tp3, status
                      FROM signal_performance
                      WHERE {eligible} AND id > ? AND id <= ?
                      ORDER BY id ASC LIMIT ?"""
    async with aiosqlite.connect(DB_PATH) as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT value FROM bot_settings WHERE key = ?", (MONITOR_QUEUE_STATE_KEY,)
            )
            saved = await cursor.fetchone()
            cursor = await connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'signal_performance'"
            )
            sequence = await cursor.fetchone()
            max_sequence = sequence[0] if sequence is not None else 0
            state = _parse_monitor_queue_state(saved["value"], max_sequence) if saved is not None else None
            if saved is not None and state is None:
                logger.warning("نشانگر صف پایش نامعتبر است؛ چرخه از ابتدا شروع می‌شود.")
            rows = []
            if state is not None:
                cursor = await connection.execute(
                    select_batch, (state["last_id"], state["upper_id"], limit)
                )
                rows = await cursor.fetchall()
            if not rows:
                cursor = await connection.execute(
                    f"SELECT MAX(id) FROM signal_performance WHERE {eligible}"
                )
                upper_id = (await cursor.fetchone())[0]
                if upper_id is None:
                    await connection.rollback()
                    return []
                state = {"version": 1, "last_id": 0, "upper_id": upper_id}
                cursor = await connection.execute(select_batch, (0, upper_id, limit))
                rows = await cursor.fetchall()
            if not rows:
                await connection.rollback()
                return []

            state["last_id"] = rows[-1]["id"]
            cursor = await connection.execute(
                """INSERT INTO bot_settings(key,value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (MONITOR_QUEUE_STATE_KEY, json.dumps(state, separators=(",", ":")))
            )
            if cursor.rowcount != 1:
                raise aiosqlite.IntegrityError("Monitoring batch cursor was not saved")
            await connection.commit()
            return [dict(row) for row in rows]
        except BaseException:
            await connection.rollback()
            raise


async def advance_signal_performance(perf_id: int, observed_price: float,
                                     breakeven_enabled: bool = True) -> dict | None:
    """
    گذار اتمیک سیگنال فعال با وضعیت/SL تازه؛ قیمت‌گیری خارج تراکنش انجام می‌شود.
    status، sl و closed_at با یک UPDATE و یک commit ثبت می‌شوند.
    فقط نتیجه‌ی commit‌شده برمی‌گردد؛ None یعنی هیچ گذار جدیدی ثبت نشده است.
    انتظار ورود، وضعیت نهایی و داده‌ی ناسازگار خودکار بازنویسی نمی‌شوند.
    """
    if not is_valid_price(observed_price):
        return None
    order = {"OPEN": 0, "TP1_HIT": 1, "TP2_HIT": 2}
    async with aiosqlite.connect(DB_PATH) as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM signal_performance WHERE id = ?", (perf_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                return None
            signal = dict(row)
            if (
                signal["status"] not in order
                or signal["closed_at"] is not None
                or signal["direction"] not in ("BUY", "SELL")
                or not is_valid_price(signal["entry"])
                or not is_valid_price(signal["sl"])
            ):
                await connection.rollback()
                return None

            is_buy = signal["direction"] == "BUY"
            is_breakeven = abs(signal["sl"] - signal["entry"]) < 1e-9
            at_stop = observed_price <= signal["sl"] if is_buy else observed_price >= signal["sl"]
            candidate = None
            if at_stop:
                candidate = "BREAKEVEN_HIT" if is_breakeven else "SL_HIT"
            else:
                for level, status in (("tp3", "TP3_HIT"), ("tp2", "TP2_HIT"), ("tp1", "TP1_HIT")):
                    target = signal[level]
                    if is_valid_price(target) and (
                        observed_price >= target if is_buy else observed_price <= target
                    ):
                        candidate = status
                        break

            terminal = candidate in ("TP3_HIT", "SL_HIT", "BREAKEVEN_HIT")
            if candidate is None or (
                not terminal and order[candidate] <= order[signal["status"]]
            ):
                await connection.rollback()
                return None
            move_sl = (
                candidate in ("TP1_HIT", "TP2_HIT")
                and breakeven_enabled
                and not is_breakeven
            )
            new_sl = signal["entry"] if move_sl else signal["sl"]
            closed_at = datetime.utcnow().isoformat() if terminal else None
            cursor = await connection.execute(
                """UPDATE signal_performance
                   SET status = ?, sl = ?, closed_at = ?
                   WHERE id = ? AND status = ? AND closed_at IS NULL""",
                (candidate, new_sl, closed_at, perf_id, signal["status"])
            )
            if cursor.rowcount != 1:
                raise aiosqlite.IntegrityError("Active signal transition was not updated")
            await connection.commit()
            return {
                **signal, "previous_status": signal["status"], "status": candidate,
                "sl": new_sl, "closed_at": closed_at, "breakeven_changed": bool(move_sl),
            }
        except BaseException:
            await connection.rollback()
            raise


async def close_signal_performance(perf_id: int, status: str):
    """برای وضعیت‌های نهایی (TP3_HIT یا SL_HIT یا BREAKEVEN_HIT) - closed_at رو هم ثبت می‌کنه"""
    # تابع خام برای سازگاری/fixture؛ مسیر پایش باید advance_signal_performance باشد.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE signal_performance SET status = ?, closed_at = ? WHERE id = ?",
            (status, datetime.utcnow().isoformat(), perf_id)
        )
        await db.commit()


async def update_signal_status(perf_id: int, status: str):
    """برای وضعیت‌های میانی (TP1_HIT/TP2_HIT) - سیگنال هنوز باز می‌مونه، closed_at خالی می‌مونه"""
    # تابع خام؛ جایگزین تراکنش گذارِ پایش نیست.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE signal_performance SET status = ? WHERE id = ?",
            (status, perf_id)
        )
        await db.commit()


async def move_sl_to_breakeven(perf_id: int, entry: float):
    """
    بعد از TP1 یا عبور مستقیم به TP2، SL مدل پایش را به Entry منتقل می‌کند.
    status تغییر نمی‌کند؛ سفارش واقعی و کارمزد/لغزش مدیریت نمی‌شوند.
    تابع خام برای سازگاری؛ پایش زنده از advance_signal_performance استفاده می‌کند.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE signal_performance SET sl = ? WHERE id = ?",
            (entry, perf_id)
        )
        await db.commit()


async def get_user_performance_stats(user_id: int) -> dict:
    """آمار نتایج نهایی؛ پیشرفت اهداف میانی جداست و همچنان باز محسوب می‌شه."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT status, COUNT(*) FROM signal_performance WHERE user_id = ? GROUP BY status",
            (user_id,)
        )
        rows = await cursor.fetchall()
        counts = {status: count for status, count in rows}
        wins = counts.get("TP3_HIT", 0)
        losses = counts.get("SL_HIT", 0)
        breakeven = counts.get("BREAKEVEN_HIT", 0)
        excluded_from_open = ("TP3_HIT", "SL_HIT", "BREAKEVEN_HIT", "WAITING_ENTRY", "ENTRY_INVALIDATED")
        waiting = counts.get("WAITING_ENTRY", 0)
        invalidated = counts.get("ENTRY_INVALIDATED", 0)
        # انتظار ورود جدا از معاملات فعال است؛ NULL هم در SQL انتخاب نمی‌شود.
        open_count = sum(
            count for status, count in counts.items()
            if status is not None and status not in excluded_from_open
        )
        closed = wins + losses  # سربه‌سر نه برده حساب می‌شه نه باخته، پس توی نرخ برد نمیاد
        resolved = closed + breakeven
        target_progress = {
            status: counts.get(status, 0)
            for status in ("TP1_HIT", "TP2_HIT", "TP3_HIT")
        }
        win_rate = (wins / closed * 100) if closed > 0 else None
        return {
            "wins": wins, "losses": losses, "breakeven": breakeven, "open": open_count,
            "closed": closed, "win_rate": win_rate, "breakdown": counts,
            # closed برای سازگاری، همان تعداد برد+باخت است؛ resolved همه‌ی نهایی‌هاست.
            "decided": closed, "resolved": resolved, "target_progress": target_progress,
            "waiting": waiting, "invalidated": invalidated, "monitored": open_count + waiting,
        }


async def get_user_open_signals(user_id: int) -> list[dict]:
    """سیگنال‌های در پایش: انتظار ورود یا فعال؛ برای /mysignals."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT id, symbol, timeframe, direction, entry, sl, tp1, tp2, tp3, status, created_at,
                      entry_triggered_at, entry_observed_price
               FROM signal_performance
               WHERE user_id = ? AND status NOT IN ('TP3_HIT', 'SL_HIT', 'BREAKEVEN_HIT', 'ENTRY_INVALIDATED')
               ORDER BY id DESC""",
            (user_id,)
        )
        rows = await cursor.fetchall()
        cols = ["id", "symbol", "timeframe", "direction", "entry", "sl", "tp1", "tp2", "tp3", "status", "created_at",
                "entry_triggered_at", "entry_observed_price"]
        return [dict(zip(cols, r)) for r in rows]


async def delete_signal_performance(perf_id: int, user_id: int) -> bool:
    """حذف دستی یه سیگنال از پایش - فقط اگه واقعاً مال همون کاربر باشه. برمی‌گردونه True اگه حذف شد."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM signal_performance WHERE id = ? AND user_id = ?", (perf_id, user_id)
        )
        exists = await cursor.fetchone() is not None
        if not exists:
            return False
        await db.execute("DELETE FROM signal_performance WHERE id = ? AND user_id = ?", (perf_id, user_id))
        await db.commit()
        return True


# ==================== سیگنال‌های در انتظار تاییدیه‌ی کاربر ====================

async def create_pending_signal(user_id: int, symbol: str, timeframe: str, direction: str,
                                 entry: float, sl: float, tps: list) -> int:
    """
    قبل از ثبت قطعی یه سیگنال برای پایش، اول اینجا به‌صورت موقت ذخیره
    می‌شه تا از کاربر تاییدیه گرفته بشه. آی‌دی ردیف رو برمی‌گردونه.
    """
    entry, sl, targets = validate_trade_geometry(direction, entry, sl, tps)
    tp1, tp2, tp3 = targets
    async with aiosqlite.connect(DB_PATH) as db:
        # پاکسازی سبک، نه مرجع اعتبار تایید. datetime هر دو طرف را به UTC
        # نرمال می‌کند؛ مقایسه‌ی سخت‌گیرانه ممکن است حذف را کمتر از یک ثانیه
        # عقب بیندازد، اما انقضای دقیق هنگام تصمیم بررسی می‌شود.
        cutoff = (datetime.utcnow() - timedelta(days=1)).isoformat()
        await db.execute(
            "DELETE FROM pending_signals WHERE user_id = ? AND datetime(created_at) < datetime(?)",
            (user_id, cutoff)
        )
        cursor = await db.execute(
            """INSERT INTO pending_signals
               (user_id, symbol, timeframe, direction, entry, sl, tp1, tp2, tp3, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, symbol, timeframe, direction, entry, sl, tp1, tp2, tp3, datetime.utcnow().isoformat())
        )
        await db.commit()
        return cursor.lastrowid


async def get_pending_signal(pending_id: int) -> dict | None:
    """خواندن خام برای مشاهده؛ وجود ردیف به معنی اعتبار یا قابل‌تایید بودن نیست."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT id, user_id, symbol, timeframe, direction, entry, sl, tp1, tp2, tp3, created_at
               FROM pending_signals WHERE id = ?""",
            (pending_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        cols = ["id", "user_id", "symbol", "timeframe", "direction", "entry", "sl", "tp1", "tp2", "tp3", "created_at"]
        return dict(zip(cols, row))


def _pending_time_outcome(created_at, now: datetime) -> str | None:
    """زمان‌های قدیمی بدون offset، UTC هستند؛ زمان نامعلوم/آینده پذیرفته نمی‌شود."""
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created = created.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return "invalid_timestamp"
    if created > now:
        return "invalid_timestamp"
    return "expired" if now - created >= timedelta(hours=24) else None


async def decide_pending_signal(pending_id: int, user_id: int, confirm: bool) -> dict:
    """
    تصمیم اتمیک درباره‌ی یک pending: یک تصمیم برنده، حداکثر یک ثبت پایش.
    BEGIN IMMEDIATE قبل از خواندن، تایید/رد هم‌زمان را سری می‌کند.
    وضعیت‌های missing/forbidden/invalid داده را تغییر نمی‌دهند؛ مالک می‌تواند
    درخواست expired/invalid_timestamp را مصرف کند، اما معامله ثبت نمی‌شود.
    """
    async with aiosqlite.connect(DB_PATH) as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM pending_signals WHERE id = ?", (pending_id,)
            )
            pending = await cursor.fetchone()
            if pending is None:
                await connection.rollback()
                return {"outcome": "missing"}
            if pending["user_id"] != user_id:
                await connection.rollback()
                return {"outcome": "forbidden"}

            # ساعت را بعد از انتظار برای قفل می‌خوانیم، نه قبل از تراکنش.
            outcome = _pending_time_outcome(pending["created_at"], datetime.now(timezone.utc))
            perf_id = None
            if outcome is None and confirm:
                try:
                    perf_id = await _insert_signal_performance(
                        connection, pending["user_id"], pending["symbol"],
                        pending["timeframe"], pending["direction"],
                        pending["entry"], pending["sl"],
                        [pending["tp1"], pending["tp2"], pending["tp3"]]
                    )
                except ValueError:
                    await connection.rollback()
                    return {"outcome": "invalid"}
                outcome = "accepted"
            elif outcome is None:
                outcome = "declined"

            cursor = await connection.execute(
                "DELETE FROM pending_signals WHERE id = ? AND user_id = ?",
                (pending_id, user_id)
            )
            if cursor.rowcount != 1:
                raise aiosqlite.IntegrityError("Pending decision was not consumed")
            await connection.commit()
            return {"outcome": outcome, "performance_id": perf_id}
        except BaseException:
            await connection.rollback()
            raise


async def delete_pending_signal(pending_id: int):
    """حذف خام؛ برای تصمیم کاربر از decide_pending_signal استفاده شود."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pending_signals WHERE id = ?", (pending_id,))
        await db.commit()


# ==================== تنظیمات سراسری (خوانده‌شده هم توسط ربات، هم پنل وب) ====================

async def get_setting(key: str, default: str = None) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO bot_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(value))
        )
        await db.commit()


async def get_all_settings() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT key, value FROM bot_settings")
        rows = await cursor.fetchall()
        return {k: v for k, v in rows}


async def get_int_setting(key: str, default: int) -> int:
    val = await get_setting(key)
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default


async def get_float_setting(key: str, default: float) -> float:
    val = await get_setting(key)
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


# ==================== لیست کاربران (برای پنل ادمین) ====================

async def get_users_page(limit: int = 20, offset: int = 0) -> list[dict]:
    """
    لیست کاربران به ترتیب جدیدترین اول، به‌همراه وضعیت مسدود/لیست‌سفید
    (با LEFT JOIN، بدون نیاز به چند کوئری جدا)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT u.user_id, u.username, u.joined_at, u.auto_scan_enabled,
                      CASE WHEN b.user_id IS NOT NULL THEN 1 ELSE 0 END AS is_blocked,
                      CASE WHEN w.user_id IS NOT NULL THEN 1 ELSE 0 END AS is_whitelisted
               FROM users u
               LEFT JOIN blocked_users b ON b.user_id = u.user_id
               LEFT JOIN whitelist_users w ON w.user_id = u.user_id
               ORDER BY u.joined_at DESC
               LIMIT ? OFFSET ?""",
            (limit, offset)
        )
        rows = await cursor.fetchall()
        cols = ["user_id", "username", "joined_at", "auto_scan_enabled", "is_blocked", "is_whitelisted"]
        return [dict(zip(cols, r)) for r in rows]


async def get_user_total_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return row[0] if row else 0


async def find_user(user_id: int) -> dict | None:
    """پروفایل کامل یه کاربر خاص - برای جستجوی تکی توسط ادمین"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT u.user_id, u.username, u.joined_at, u.auto_scan_enabled,
                      CASE WHEN b.user_id IS NOT NULL THEN 1 ELSE 0 END AS is_blocked,
                      b.reason,
                      CASE WHEN w.user_id IS NOT NULL THEN 1 ELSE 0 END AS is_whitelisted
               FROM users u
               LEFT JOIN blocked_users b ON b.user_id = u.user_id
               LEFT JOIN whitelist_users w ON w.user_id = u.user_id
               WHERE u.user_id = ?""",
            (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        cols = ["user_id", "username", "joined_at", "auto_scan_enabled", "is_blocked", "block_reason", "is_whitelisted"]
        profile = dict(zip(cols, row))
        profile["watchlist_count"] = await get_watchlist_count(user_id)
        return profile


# ==================== لیست سفید (whitelist) ====================

async def add_to_whitelist(user_id: int, note: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO whitelist_users (user_id, note, added_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET note=excluded.note""",
            (user_id, note, datetime.utcnow().isoformat())
        )
        await db.commit()


async def remove_from_whitelist(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM whitelist_users WHERE user_id = ?", (user_id,))
        existed = await cursor.fetchone() is not None
        await db.execute("DELETE FROM whitelist_users WHERE user_id = ?", (user_id,))
        await db.commit()
        return existed


async def is_whitelisted(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM whitelist_users WHERE user_id = ?", (user_id,))
        return await cursor.fetchone() is not None


async def get_whitelist() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT w.user_id, u.username, w.note, w.added_at
               FROM whitelist_users w
               LEFT JOIN users u ON u.user_id = w.user_id
               ORDER BY w.added_at DESC"""
        )
        rows = await cursor.fetchall()
        cols = ["user_id", "username", "note", "added_at"]
        return [dict(zip(cols, r)) for r in rows]


# ==================== درخواست‌های دسترسی در انتظار (برای نوتیفیکیشن ادمین) ====================

async def create_access_request(user_id: int) -> bool:
    """
    ثبت درخواست دسترسی. خروجی True یعنی این اولین باریه که این کاربر
    درخواست داده (پس باید به ادمین اطلاع بدیم)؛ False یعنی قبلاً درخواست
    داده بود (چندبار /start زده) - نباید دوباره نوتیفای کنیم.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM access_requests WHERE user_id = ?", (user_id,))
        already_exists = await cursor.fetchone() is not None
        if already_exists:
            return False
        await db.execute(
            "INSERT INTO access_requests (user_id, requested_at) VALUES (?, ?)",
            (user_id, datetime.utcnow().isoformat())
        )
        await db.commit()
        return True


async def delete_access_request(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM access_requests WHERE user_id = ?", (user_id,))
        await db.commit()


async def get_access_requests() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT a.user_id, u.username, a.requested_at
               FROM access_requests a
               LEFT JOIN users u ON u.user_id = a.user_id
               ORDER BY a.requested_at ASC"""
        )
        rows = await cursor.fetchall()
        cols = ["user_id", "username", "requested_at"]
        return [dict(zip(cols, r)) for r in rows]


# ==================== تحلیل فراداده (Meta-Analytics) ====================

async def get_resolved_signals(user_id: int = None) -> list[dict]:
    """
    همه‌ی سیگنال‌هایی که به یه نتیجه‌ی نهایی (TP3/SL/سربه‌سر) رسیدن - پایه‌ی
    تحلیل فراداده (کدوم تایم‌فریم/نماد/روز بهتر عمل کرده). اگه user_id
    داده بشه فقط سیگنال‌های همون کاربر، وگرنه کل ربات (برای ادمین).
    """
    query = """SELECT symbol, timeframe, direction, status, created_at, closed_at
               FROM signal_performance
               WHERE status IN ('TP3_HIT', 'SL_HIT', 'BREAKEVEN_HIT')"""
    params = ()
    if user_id is not None:
        query += " AND user_id = ?"
        params = (user_id,)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        cols = ["symbol", "timeframe", "direction", "status", "created_at", "closed_at"]
        return [dict(zip(cols, r)) for r in rows]


async def get_resolved_signals_for(symbol: str, timeframe: str, direction: str) -> list[dict]:
    """
    سیگنال‌های به‌نتیجه‌رسیده‌ی قبلی با همون نماد + تایم‌فریم + جهت دقیقاً - برای
    فاز ۲ (یادگیری از تاریخچه‌ی عملکرد) - برمی‌گردونه لیستی از دیکشنری فقط شامل status.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT status FROM signal_performance
               WHERE symbol = ? AND timeframe = ? AND direction = ?
               AND status IN ('TP3_HIT', 'SL_HIT', 'BREAKEVEN_HIT')""",
            (symbol, timeframe, direction)
        )
        rows = await cursor.fetchall()
        return [{"status": r[0]} for r in rows]


# ==================== تنظیمات اخبار هر کاربر ====================

async def get_user_news_enabled(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT enabled FROM user_news_settings WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row is None:
            return True  # پیش‌فرض روشن
        return bool(row[0])

async def set_user_news_enabled(user_id: int, enabled: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO user_news_settings (user_id, enabled) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled""",
            (user_id, 1 if enabled else 0)
        )
        await db.commit()

async def get_user_news_auto(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT auto_enabled FROM user_news_settings WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row is None:
            return False
        return bool(row[0])

async def set_user_news_auto(user_id: int, enabled: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO user_news_settings (user_id, auto_enabled) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET auto_enabled=excluded.auto_enabled""",
            (user_id, 1 if enabled else 0)
        )
        await db.commit()

async def get_users_with_news_auto() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM user_news_settings WHERE auto_enabled = 1")
        rows = await cursor.fetchall()
        return [r[0] for r in rows]
