import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from exchange import ExchangeClient, normalize_symbol
from backtest import (
    run_backtest, run_walk_forward_backtest, run_weight_calibration, SimulationSettings,
    DEFAULT_BACKTEST_CANDLES, MAX_BACKTEST_CANDLES, MIN_BACKTEST_CANDLES,
    CALIBRATION_SEGMENTS, CALIBRATION_MIN_TRADES, CALIBRATION_STABILITY_PENALTY_WEIGHT,
)
from config import ADMIN_IDS, DEFAULT_SYMBOLS
from single_analysis import TIMEFRAME_LABELS_FA
from charts import generate_backtest_chart, generate_backtest_chart_async
import database as db
from engine_config import get_engine_config

SCORING_MODE_FA = {"classic": "مدل فعلی", "trend_block": "مدل بلوک روند (آزمایشی)"}


async def _simulation_settings(scoring_mode: str | None = None) -> SimulationSettings:
    """تنظیمات شبیه‌سازی از engine_config (پنل وب) - هم‌سان با رفتار زنده‌ی ربات"""
    from config import BREAKEVEN_AFTER_TP1
    cfg = await get_engine_config()
    return SimulationSettings(
        live_model=cfg.backtest_live_model,
        fee_rate=cfg.fee_rate,
        slippage=cfg.slippage,
        scoring_mode=scoring_mode or cfg.scoring_mode,
        breakeven_after_tp1=BREAKEVEN_AFTER_TP1,
        max_cost_r=cfg.fee_filter_max_r,
    )


def _methodology_text(sim: SimulationSettings) -> str:
    if sim.live_model:
        entry = ("ورود با سفارش limit روی نقطه‌ی ورود پیشنهادی (مثل پایش زنده)؛ اگه قیمت قبل از ورود "
                 "به SL برسه یا اصلاً به نقطه‌ی ورود نرسه، معامله رد می‌شه. بعد از TP1 حد ضرر به نقطه‌ی "
                 "ورود منتقل می‌شه؛ خروج فقط با TP3، SL، سربه‌سر یا پایان دوره")
    else:
        entry = "ورود فرضی با قیمت باز شدن کندل بعد از سیگنال (مدل قدیمی)"
    if sim.fee_rate > 0 or sim.slippage > 0:
        cost = (f"کارمزد {sim.fee_rate*100:.2f}% هر طرف و لغزش {sim.slippage*100:.2f}% برای خروج‌های "
                f"بازار از R کم شده")
    else:
        cost = "کارمزد/لغزش لحاظ نشده"
    return (f"⚠️ *روش‌شناسی*: {entry}. اگه توی یه کندل هم SL و هم TP لمس بشن، محافظه‌کارانه فرض "
            f"می‌کنیم SL اول خورده. {cost}. سربه‌سر جزو برد یا باخت حساب نمی‌شه (مثل /mystats). "
            f"این شبیه‌سازی تضمین عملکرد آینده نیست.")

EXIT_REASON_FA = {
    "SL": "خورد به حد ضرر",
    "TP3": "رسید به TP3",
    "BREAKEVEN": "بعد از TP1 به نقطه‌ی ورود (سربه‌سر) برگشت",
    "TP2_TIMEOUT": "تا پایان دوره فقط تا TP2 رسید",
    "TP1_TIMEOUT": "تا پایان دوره فقط تا TP1 رسید",
    "TIMEOUT": "بدون رسیدن به هیچ سطحی، دوره تموم شد",
    "STILL_OPEN": "هنوز باز (داده تموم شد) — توی آمار حساب نشده",
}
ENTRY_SKIP_REASON_FA = {
    "INVALID_ENTRY_PRICE": "قیمت ورود نامعتبر",
    "ENTRY_AT_OR_BEYOND_SL": "ورود روی یا آن‌سوی SL",
    "ENTRY_AT_OR_BEYOND_TP1": "ورود روی یا آن‌سوی TP1",
    "INVALID_ACTUAL_GEOMETRY": "هندسه/نسبت R ورود نامعتبر",
    "INVALID_SIGNAL_LEVELS": "خروجی سیگنال نامعتبر",
    "INVALIDATED_BEFORE_ENTRY": "قبل از ورود از SL رد شد (باطل)",
    "NOT_FILLED": "قیمت به نقطه‌ی ورود نرسید",
}


def _format_skipped_reasons(reason_counts: dict) -> str:
    return "؛ ".join(f"{ENTRY_SKIP_REASON_FA.get(reason, 'علت دیگر')}: {number}"
                     for reason, number in reason_counts.items())


def _format_skipped_entries(skipped_count: int, reason_counts: dict) -> list:
    if not skipped_count:
        return []
    return [
        f"⏭ ورودی‌های ردشده: *{skipped_count}* — در برد، باخت و R حساب نشده‌اند.",
        _format_skipped_reasons(reason_counts),
    ]


def _fmt_price(value) -> str:
    if value is None:
        return "-"
    value = float(value)
    if value == 0:
        return "0"
    if value < 0.01:
        return f"{value:,.6f}"
    if value < 1:
        return f"{value:,.4f}"
    return f"{value:,.2f}"


def _format_backtest_result(result, title_prefix: str = "📊") -> str:
    tf_label = TIMEFRAME_LABELS_FA.get(result.timeframe, result.timeframe)
    date_from = str(result.date_from)[:10] if result.date_from is not None else "-"
    date_to = str(result.date_to)[:10] if result.date_to is not None else "-"

    lines = [
        f"{title_prefix} *بک‌تست {result.symbol} — {tf_label}*",
        f"بازه: {date_from} تا {date_to} ({result.candles_tested} کندل)",
        "",
    ]
    lines += _format_skipped_entries(result.skipped_count, result.skipped_reason_counts)

    if result.total_trades == 0:
        lines.append("در این بازه هیچ معاملهٔ نهایی‌شده‌ای برای آمار وجود ندارد.")
        if result.still_open_count:
            lines.append(f"\n({result.still_open_count} معامله هنوز باز مونده بود چون داده تموم شد - توی آمار حساب نشده)")
        return "\n".join(lines)

    win_rate = result.win_rate
    avg_r = result.avg_r
    pf = result.profit_factor

    lines += [
        f"🔢 تعداد معاملات: *{result.total_trades}*" + (f" (+{result.still_open_count} هنوز باز)" if result.still_open_count else ""),
        f"✅ برد: {result.wins} | ❌ باخت: {result.losses}" + (f" | ⚪️ سربه‌سر: {result.breakevens}" if result.breakevens else ""),
        f"📈 نرخ برد: *{win_rate:.1f}%*" if win_rate is not None else "📈 نرخ برد: -",
        f"⚖️ میانگین R هر معامله: *{avg_r:+.2f}*" if avg_r is not None else "",
        f"💰 مجموع R: *{result.total_r:+.2f}*" + (
            f" (قبل از کارمزد/لغزش: {result.gross_total_r:+.2f})"
            if abs(result.gross_total_r - result.total_r) > 1e-9 else ""),
        f"🎯 Profit Factor: *{pf:.2f}*" if pf is not None else "🎯 Profit Factor: بی‌نهایت (بدون ضرر)",
        f"📉 بیشترین باخت‌های پیاپی: {result.max_consecutive_losses}",
    ]
    return "\n".join(l for l in lines if l is not None)


def _format_full_backtest_message(result, sim: SimulationSettings = None, comparison=None) -> str:
    sim = sim or SimulationSettings()
    text = _format_backtest_result(result)

    lines = [text]
    if result.trades:
        lines += ["", "📋 چند معامله‌ی آخر:"]
    for t in result.trades[-5:]:
        reason_fa = EXIT_REASON_FA.get(t.exit_reason, t.exit_reason)
        dir_emoji = "🟢" if t.direction == "BUY" else "🔴"
        lines.append(f"{dir_emoji} {t.direction} @ ${_fmt_price(t.entry)} → {reason_fa} (R={t.r_multiple:+.2f})")

    if comparison is not None:
        other_mode, other = comparison
        lines += ["", f"🔬 *مقایسه با {SCORING_MODE_FA.get(other_mode, other_mode)}* (همین داده و تنظیمات):"]
        if other.total_trades == 0:
            lines.append("بدون معاملهٔ نهایی‌شده.")
        else:
            wr = f"{other.win_rate:.1f}%" if other.win_rate is not None else "-"
            avg = f"{other.avg_r:+.2f}" if other.avg_r is not None else "-"
            pf = f"{other.profit_factor:.2f}" if other.profit_factor is not None else "∞"
            lines.append(f"{other.total_trades} معامله | برد {wr} | میانگین R {avg} | "
                         f"مجموع R {other.total_r:+.2f} | PF {pf}")
        lines.append(f"(مدل فعال: {SCORING_MODE_FA.get(sim.scoring_mode, sim.scoring_mode)} — "
                     f"تغییرش از پنل وب با `engine_trend_block`)")

    lines += ["", _methodology_text(sim)]
    return "\n".join(lines)


def _format_walk_forward_message(results: list, symbol: str, timeframe: str,
                                 sim: SimulationSettings = None) -> str:
    tf_label = TIMEFRAME_LABELS_FA.get(timeframe, timeframe)
    lines = [
        f"📊 *بک‌تست پیشرو (Walk-Forward) {symbol} — {tf_label}*",
        f"داده به {len(results)} بازه‌ی مساوی و بدون هم‌پوشانی تقسیم شد:",
        "",
    ]

    all_trades = []
    skipped_count = 0
    skipped_reasons = {}
    for i, r in enumerate(results, 1):
        date_from = str(r.date_from)[:10] if r.date_from is not None else "-"
        date_to = str(r.date_to)[:10] if r.date_to is not None else "-"
        if r.total_trades == 0:
            lines.append(f"*بازه {i}* ({date_from} تا {date_to}): بدون معاملهٔ نهایی")
        else:
            wr = f"{r.win_rate:.0f}%" if r.win_rate is not None else "-"
            lines.append(
                f"*بازه {i}* ({date_from} تا {date_to}): {r.total_trades} معامله | "
                f"برد {wr} | مجموع R: {r.total_r:+.2f}"
            )
        all_trades.extend(r.trades)
        if r.skipped_count:
            lines.append(f"⏭ ورودی ردشده در این بازه: {r.skipped_count}")
        if r.still_open_count:
            lines.append(f"معاملهٔ هنوز باز در این بازه: {r.still_open_count}")
        skipped_count += r.skipped_count
        for reason, number in r.skipped_reason_counts.items():
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + number

    resolved = [t for t in all_trades if t.exit_reason != "STILL_OPEN"]
    total = len(resolved)
    lines += ["", "📐 *جمع کل (همه‌ی بازه‌ها):*"]
    lines += _format_skipped_entries(skipped_count, skipped_reasons)
    if total == 0:
        lines.append("هیچ معاملهٔ نهایی‌شده‌ای در کل بازه‌ها وجود ندارد.")
    else:
        wins = sum(1 for t in resolved if t.outcome == "win")
        decided = sum(1 for t in resolved if t.outcome != "breakeven")
        total_r = sum(t.r_multiple for t in resolved)
        wr_txt = f"{wins/decided*100:.1f}%" if decided else "-"
        lines.append(f"تعداد کل: {total} | نرخ برد کلی: {wr_txt} | مجموع R: {total_r:+.2f}")

        win_rates = [r.win_rate for r in results if r.win_rate is not None]
        if len(win_rates) >= 2:
            spread = max(win_rates) - min(win_rates)
            if spread > 40:
                lines.append(f"\n⚠️ اختلاف نرخ برد بین بازه‌ها زیاده ({spread:.0f} واحد درصد) — یعنی عملکرد این استراتژی روی این نماد/تایم‌فریم بین دوره‌های مختلف خیلی ناپایداره، احتمال overfitting یا وابستگی به شرایط خاص بازار وجود داره.")
            else:
                lines.append(f"\n✅ نرخ برد بین بازه‌ها نسبتاً پایداره (اختلاف {spread:.0f} واحد درصد) — نشونه‌ی بهتری از قایداری استراتژیه.")

    lines += [
        "",
        "⚠️ هر بازه به‌اندازه‌ی دوره‌ی warmup از قبل خودش کندل قرض می‌گیرد تا "
        "اندیکاتورهاش معتبر باشن؛ بازه‌ی اول این امکان رو نداره، پس ممکنه "
        "عملکردش کمی محافظه‌کارانه‌تر به‌نظر برسه. تعداد معاملات هر بازه "
        "کمتر از یه بک‌تست کامله (چون داده بین چند بخش تقسیم شده) - برای "
        "نتیجه‌ی آماری قابل‌اتکاتر، بازه‌ی زمانی بیشتری امتحان کن. ورودی "
        "ردشده در آمار برد/باخت نیست.",
    ]
    if sim is not None:
        lines += ["", _methodology_text(sim)]
    return "\n".join(lines)


async def backtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "استفاده: `/backtest SYMBOL [تایم‌فریم] [تعداد کندل] [wf]`\n"
            f"مثال: `/backtest BTCUSDT 4h 300`\n"
            f"برای بک‌تست پیشرو (Walk-Forward، تقسیم به چند بازه): `/backtest BTCUSDT 4h 400 wf`\n\n"
            f"تایم‌فریم پیش‌فرض: `1h` — تعداد کندل پیش‌فرض: `{DEFAULT_BACKTEST_CANDLES}` "
            f"(بین {MIN_BACKTEST_CANDLES} تا {MAX_BACKTEST_CANDLES})",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    symbol = normalize_symbol(context.args[0])
    timeframe = context.args[1] if len(context.args) > 1 else "1h"

    walk_forward = "wf" in [a.lower() for a in context.args]
    numeric_args = [a for a in context.args[2:] if a.isdigit()]

    candles = DEFAULT_BACKTEST_CANDLES
    if numeric_args:
        candles = int(numeric_args[0])
        candles = max(MIN_BACKTEST_CANDLES, min(MAX_BACKTEST_CANDLES, candles))

    mode_label = "بک‌تست پیشرو (Walk-Forward)" if walk_forward else "بک‌تست"
    msg = await update.message.reply_text(
        f"⏳ در حال {mode_label} {symbol} روی {TIMEFRAME_LABELS_FA.get(timeframe, timeframe)} "
        f"با {candles} کندل تاریخی... (چند ثانیه طول می‌کشه)"
    )

    client = ExchangeClient()
    try:
        if not await client.validate_symbol(symbol):
            await msg.edit_text(f"❌ نماد `{symbol}` روی صرافی پیدا نشد.", parse_mode=ParseMode.MARKDOWN)
            return

        engine_cfg = await get_engine_config()
        df = await client.fetch_ohlcv_df(symbol, timeframe, limit=candles,
                                         closed_only=engine_cfg.closed_candles)
        if len(df) < MIN_BACKTEST_CANDLES:
            await msg.edit_text(
                f"⚠️ فقط {len(df)} کندل تاریخچه در دسترسه (حداقل {MIN_BACKTEST_CANDLES} تا لازمه). "
                f"یه تایم‌فریم کوچک‌تر یا نماد پرسابقه‌تر امتحان کن.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        confidence_threshold = await db.get_float_setting("confidence_threshold_fraction", 0.25)
        atr_sl_mult = await db.get_float_setting("atr_sl_mult", 1.5)
        rr_targets_raw = await db.get_setting("rr_targets")
        rr_targets = None
        if rr_targets_raw:
            try:
                rr_targets = [float(x.strip()) for x in rr_targets_raw.split(",")]
            except ValueError:
                rr_targets = None

        sim = await _simulation_settings()
        if walk_forward:
            from config import WALK_FORWARD_SEGMENTS
            results = await asyncio.to_thread(
                run_walk_forward_backtest,
                df, symbol, timeframe, segments=WALK_FORWARD_SEGMENTS,
                confidence_threshold_fraction=confidence_threshold,
                atr_sl_mult=atr_sl_mult, rr_targets=rr_targets, sim=sim,
            )
            text = _format_walk_forward_message(results, symbol, timeframe, sim=sim)
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)

            all_trades = [t for r in results for t in r.trades]
            if any(r.total_trades > 0 for r in results):
                chart_buf = await generate_backtest_chart_async(all_trades, symbol, f"{timeframe} (Walk-Forward)")
                await update.message.reply_photo(photo=chart_buf)
        else:
            other_mode = "classic" if sim.scoring_mode == "trend_block" else "trend_block"
            from dataclasses import replace as _replace
            other_sim = _replace(sim, scoring_mode=other_mode)

            def _both():
                main = run_backtest(df, symbol, timeframe,
                                    confidence_threshold_fraction=confidence_threshold,
                                    atr_sl_mult=atr_sl_mult, rr_targets=rr_targets, sim=sim)
                other = run_backtest(df, symbol, timeframe,
                                     confidence_threshold_fraction=confidence_threshold,
                                     atr_sl_mult=atr_sl_mult, rr_targets=rr_targets, sim=other_sim)
                return main, other

            result, other_result = await asyncio.to_thread(_both)
            text = _format_full_backtest_message(result, sim=sim, comparison=(other_mode, other_result))
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)

            if result.total_trades > 0:
                chart_buf = await generate_backtest_chart_async(result.trades, symbol, timeframe)
                await update.message.reply_photo(photo=chart_buf)

    except Exception as e:
        await msg.edit_text(f"❌ خطا در بک‌تست: {e}")
    finally:
        await client.close()


# ==================== کالیبراسیون وزن‌ها (/calibrate) ====================

CALIBRATION_DEFAULT_CANDLES = MAX_BACKTEST_CANDLES
_KNOWN_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "3d", "1w"}


def _fmt_calibration_value(value, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:.1f}{suffix}"


def _format_calibration_message(candidates: list, symbols: list, timeframe: str) -> str:
    tf_label = TIMEFRAME_LABELS_FA.get(timeframe, timeframe)
    symbols_label = "، ".join(symbols)

    reliable = [c for c in candidates if c.reliable]
    unreliable = [c for c in candidates if not c.reliable]

    lines = [
        "🧪 *کالیبراسیون وزن‌ها (Walk-Forward)*",
        f"نمادها: {symbols_label}",
        f"تایم‌فریم: {tf_label}",
        "",
    ]

    if not reliable:
        lines.append(
            f"⚠️ هیچ ترکیبی حداقل {CALIBRATION_MIN_TRADES} معامله‌ی جمع‌شده روی همه‌ی "
            f"نمادها/بازه‌ها نداشت، پس نمی‌شه با اطمینان پیشنهادی داد. "
            f"یه بازه‌ی زمانی/تعداد کندل بیشتر یا نمادهای بیشتر امتحان کن."
        )
    else:
        lines.append("🏆 *بهترین ترکیب‌های قابل‌اتکا:*")
        for i, c in enumerate(reliable[:5], 1):
            wr = _fmt_calibration_value(c.win_rate, "%")
            spread = _fmt_calibration_value(c.win_rate_spread, " واحد")
            lines.append(
                f"{i}. آستانه اطمینان={c.confidence_threshold_fraction:.2f} | "
                f"ضریب ATR (SL)={c.atr_sl_mult:.1f} → "
                f"{c.total_trades} معامله | برد {wr} | میانگین R: {c.avg_r:+.2f} | "
                f"مجموع R: {c.total_r:+.2f} | اختلاف برد بین بازه‌ها: {spread} | "
                f"امتیاز ترکیبی: {c.composite_score:+.2f}"
            )

    if unreliable:
        lines.append(
            "\nℹ️ " + str(len(unreliable)) +
            f" ترکیب دیگه به‌خاطر تعداد معامله‌ی کم (کمتر از {CALIBRATION_MIN_TRADES}) "
            "از رتبه‌بندی حذف شدن؛ آماری قابل‌اتکا ندارن."
        )

    skipped_candidates = [candidate for candidate in candidates if candidate.skipped_count]
    if skipped_candidates:
        lines.append("\n⏭ ورودی‌های ردشده به تفکیک ترکیب (این شمارش‌ها رویداد یکتا نیستند):")
        for candidate in skipped_candidates:
            lines.append(
                f"{candidate.confidence_threshold_fraction:.2f}/{candidate.atr_sl_mult:.1f}: "
                f"{candidate.skipped_count} — {_format_skipped_reasons(candidate.skipped_reason_counts)}"
            )

    lines.append("")
    lines.append(
        "📐 *امتیاز ترکیبی* = میانگین R − (اختلاف نرخ برد بین بازه‌ها ÷ ۱۰۰) × " +
        f"{CALIBRATION_STABILITY_PENALTY_WEIGHT:.1f}" +
        " — یعنی هم سودآوری رو در نظر می‌گیره هم پایداری بین بازه‌های مختلف رو "
        "(مشابه هشدار ناپایداری خود بک‌تست پیشرو)."
    )
    lines.append("")
    lines.append(
        "⚠️ این فقط یه *گزارشه* — هیچ تنظیمی خودکار اعمال نمی‌شه. "
        "برای اعمال یکی از ترکیب‌های بالا، روی دکمه‌ش بزن. ردشدگی ورودی‌ها "
        "در امتیاز جریمهٔ جداگانه ندارد؛ آمار فقط معاملات معتبر است. "
        "کارمزد/لغزش و مدل ورود طبق تنظیمات پنل وب لحاظ شده."
    )
    return "\n".join(lines)


def _calibration_keyboard(candidates: list):
    reliable = [c for c in candidates if c.reliable]
    if not reliable:
        return None
    buttons = []
    for c in reliable[:3]:
        label = f"✅ اعمال ({c.confidence_threshold_fraction:.2f} / {c.atr_sl_mult:.1f})"
        buttons.append([InlineKeyboardButton(
            label, callback_data=f"applycal:{c.confidence_threshold_fraction:.2f}:{c.atr_sl_mult:.1f}"
        )])
    return InlineKeyboardMarkup(buttons)


async def calibrate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    دستور ادمین‌محور برای کالیبراسیون آستانه‌ی اطمینان و ضریب ATR (SL)
    با بک‌تست پیشرو، روی یک یا چند نماد. هیچ‌چیزی خودکار اعمال نمی‌شه؛
    فقط گزارش + دکمه‌ی تایید نشون داده می‌شه.

    استفاده: /calibrate [SYMBOL] [تایم‌فریم] [تعداد کندل]
    بدون SYMBOL → روی چند نماد پیش‌فرض (config.DEFAULT_SYMBOLS) اجرا می‌شه
    (پیشنهادی، چون این تنظیمات سراسری‌ان و روی همه‌ی کاربران/نمادها اثر می‌ذارن).
    """
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ این دستور فقط برای ادمینه.")
        return

    args = list(context.args or [])
    timeframe = "1h"
    candles = CALIBRATION_DEFAULT_CANDLES
    symbol_arg = None

    scoring_override = None
    for a in args:
        if a.lower() in ("tb", "trend_block"):
            scoring_override = "trend_block"
        elif a.lower() == "classic":
            scoring_override = "classic"
        elif a.lower() in _KNOWN_TIMEFRAMES:
            timeframe = a.lower()
        elif a.isdigit():
            candles = max(MIN_BACKTEST_CANDLES, min(MAX_BACKTEST_CANDLES, int(a)))
        else:
            symbol_arg = a

    if symbol_arg:
        symbols = [normalize_symbol(symbol_arg)]
    else:
        symbols = list(DEFAULT_SYMBOLS)

    symbols_label = "، ".join(symbols)
    msg = await update.message.reply_text(
        f"⏳ در حال کالیبراسیون روی {symbols_label} — {TIMEFRAME_LABELS_FA.get(timeframe, timeframe)} "
        f"با {candles} کندل تاریخی... (ممکنه یکی-دو دقیقه طول بکشه)"
    )

    engine_cfg = await get_engine_config()
    sim = await _simulation_settings(scoring_override)
    client = ExchangeClient()
    try:
        async def _load(symbol):
            if not await client.validate_symbol(symbol):
                return None
            df = await client.fetch_ohlcv_df(symbol, timeframe, limit=candles,
                                             closed_only=engine_cfg.closed_candles)
            return df if len(df) >= MIN_BACKTEST_CANDLES else None

        # دریافت هم‌زمان داده‌ی همه‌ی نمادها؛ ترتیب نمادها حفظ می‌شود
        loaded = await asyncio.gather(*(_load(symbol) for symbol in symbols), return_exceptions=True)
        symbol_dfs = []
        skipped = []
        for symbol, df in zip(symbols, loaded):
            if df is None or isinstance(df, BaseException):
                skipped.append(symbol)
            else:
                symbol_dfs.append((symbol, timeframe, df))

        if not symbol_dfs:
            await msg.edit_text("❌ هیچ داده‌ی معتبری برای هیچ‌کدوم از نمادها پیدا نشد.")
            return

        # ده‌ها بک‌تست پشت‌سرهم؛ در ترد جدا تا ربات برای بقیه‌ی کاربران قفل نشه
        candidates = await asyncio.to_thread(run_weight_calibration, symbol_dfs,
                                             segments=CALIBRATION_SEGMENTS, sim=sim)
        used_symbols = [s for s, _, _ in symbol_dfs]
        text = _format_calibration_message(candidates, used_symbols, timeframe)
        text += "\n\n" + f"🧮 مدل امتیازدهی: {SCORING_MODE_FA.get(sim.scoring_mode, sim.scoring_mode)}"
        if sim.scoring_mode != engine_cfg.scoring_mode:
            text += ("\n⚠️ این کالیبراسیون برای مدلی غیر از مدل فعاله؛ فقط وقتی اعمالش کن که "
                     "هم‌زمان `engine_trend_block` رو توی پنل وب عوض می‌کنی.")
        if skipped:
            text = text + "\n\n(نمادهای رد شده به‌خاطر نبود داده‌ی کافی: " + "، ".join(skipped) + ")"

        keyboard = _calibration_keyboard(candidates)
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

    except Exception as e:
        await msg.edit_text(f"❌ خطا در کالیبراسیون: {e}")
    finally:
        await client.close()
