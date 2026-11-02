"""
بک‌تست خودکار موتور سیگنال روی داده‌ی تاریخی

روش کار:
اندیکاتورها یه‌بار روی کل دیتافریم محاسبه می‌شن (چون همه‌شون علی/causal
هستن - یعنی مقدارشون در هر لحظه فقط به داده‌ی تا همون لحظه بستگی داره،
نه آینده؛ برای اطمینان بیشتر، برای صدور سیگنال در هر نقطه فقط از
df.iloc[:i+1] استفاده می‌شه، یعنی موتور اصلاً نمی‌تونه به کندل‌های بعدی
دسترسی داشه باشه). سپس برای هر کندل i (بعد از دوره‌ی warmup):
- موتور signal رو دقیقاً با همون منطق زنده‌ی /signal صدا می‌زنیم
- اگه سیگنال قطعی (BUY/SELL) بود، فرض می‌کنیم ورود در کندل بعدی (i+1)
  با قیمت باز شدنش (Open) انجام می‌شه - این روش استاندارد و محافظه‌کارانه‌ست
  (نه با قیمت دقیق پیشنهادی که ممکنه اصلاً پر نشه)
- قیمت ورود باید بین SL و TP1 باقی بماند؛ گپ نامعتبر با علت در
  skipped_entries ثبت می‌شود، نه به‌عنوان برد/باخت یا معاملهٔ باز
- بعد کندل‌به‌کندل جلو می‌ریم تا ببینیم اول به SL می‌خوریم یا به TP ها
- تا وقتی این معامله باز نشده، سیگنال جدید حساب نمی‌کنیم (بدون هم‌پوشانی)

فرض محافظه‌کارانه: اگه توی یه کندل، هم SL و هم یه TP لمس بشن (چون از
روی کندل‌های OHLC نمی‌شه ترتیب دقیق داخل کندل رو فهمید)، SL رو مقدم
فرض می‌کنیم - یعنی بک‌تست به‌جای خوش‌بینانه، بدبینانه/محافظه‌کارانه‌ست.

** فاز ۳ - مدل هم‌سان با پایش زنده (SimulationSettings.live_model=True) **
- ورود با سفارش limit روی Entry پیشنهادی (پولبک)، نه open کندل بعد؛ تا
  max_hold کندل منتظر پر شدن می‌مونه. گپ به آن‌سوی SL قبل از ورود = باطل.
  اگه open از Entry بهتر بود (گپ)، با open پر می‌شه.
- توی کندل ورود فقط SL چک می‌شه (ترتیب داخل کندل معلوم نیست).
- بعد از TP1، SL به نقطه‌ی ورود منتقل می‌شه (مثل BREAKEVEN_AFTER_TP1 پایش).
- خروج فقط TP3، SL، سربه‌سر یا پایان دوره (با close)؛ دیگه «تا TP2 رسید»
  به‌عنوان ۲R کامل ثبت نمی‌شه، چون پوزیشن واقعی هنوز باز بوده.
- کارمزد هر طرف + لغزش خروج‌های بازار (SL/سربه‌سر/timeout) از R کم می‌شه.
- برد/باخت مثل /mystats: سربه‌سر جزو برد یا باخت حساب نمی‌شه.
"""
from dataclasses import dataclass, field
from collections import Counter
from numbers import Real
import math
import pandas as pd
from dataclasses import dataclass as _dataclass

from single_analysis import add_extended_indicators, build_single_result
from trade_validation import TradeGeometryError, validate_trade_geometry

DEFAULT_WARMUP_CANDLES = 60      # قبل از این تعداد کندل، اندیکاتورها (مخصوصاً SMA50) هنوز معتبر نیستن
DEFAULT_MAX_HOLD_CANDLES = 60    # حداکثر تعداد کندلی که یه معامله باز می‌مونه قبل از timeout
DEFAULT_BACKTEST_CANDLES = 300


@_dataclass(frozen=True)
class SimulationSettings:
    """پیش‌فرض‌ها = رفتار قبلی بک‌تست (برای سازگاری)؛ هندلرها از engine_config می‌سازن"""
    live_model: bool = False
    fee_rate: float = 0.0          # کسر برای هر طرف (0.001 = 0.1%)
    slippage: float = 0.0          # کسر برای خروج‌های بازار
    scoring_mode: str = "classic"
    breakeven_after_tp1: bool = True
    max_cost_r: float = 0.0


LEGACY_SIMULATION = SimulationSettings()
MAX_BACKTEST_CANDLES = 500
MIN_BACKTEST_CANDLES = 120


@dataclass
class BacktestTrade:
    entry_index: int
    entry_time: object
    exit_time: object
    direction: str
    entry: float
    sl: float
    tps: list
    exit_price: float
    exit_reason: str  # "SL" | "TP3" | "BREAKEVEN" | "TP2_TIMEOUT" | "TP1_TIMEOUT" | "TIMEOUT" | "STILL_OPEN"
    r_multiple: float     # خالص (بعد از کارمزد/لغزش)
    confidence: int
    gross_r: float = None  # قبل از کسر هزینه‌ها
    cost_r: float = 0.0

    @property
    def outcome(self) -> str:
        """win | loss | breakeven - سربه‌سر (BREAKEVEN) نه برد حساب می‌شه نه باخت (مثل /mystats)"""
        if self.exit_reason == "BREAKEVEN":
            return "breakeven"
        return "win" if self.r_multiple > 0 else "loss"


@dataclass
class BacktestSkippedEntry:
    """An attempted entry, not a win/loss/open trade; indices are local to its run."""
    signal_index: int
    entry_index: int
    signal_time: object
    entry_time: object
    direction: object
    planned_entry: object
    actual_entry: object
    sl: object
    tps: object
    reason: str


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    candles_tested: int
    date_from: object
    date_to: object
    trades: list = field(default_factory=list)
    skipped_entries: list = field(default_factory=list)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_entries)

    @property
    def skipped_reason_counts(self) -> dict:
        return dict(Counter(entry.reason for entry in self.skipped_entries))

    @property
    def resolved_trades(self) -> list:
        """معاملاتی که واقعاً به نتیجه رسیدن (نه STILL_OPEN که هنوز دیتاش تموم نشده)"""
        return [t for t in self.trades if t.exit_reason != "STILL_OPEN"]

    @property
    def total_trades(self) -> int:
        return len(self.resolved_trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.resolved_trades if t.outcome == "win")

    @property
    def breakevens(self) -> int:
        return sum(1 for t in self.resolved_trades if t.outcome == "breakeven")

    @property
    def gross_total_r(self) -> float:
        return sum((t.gross_r if t.gross_r is not None else t.r_multiple) for t in self.resolved_trades)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.resolved_trades if t.outcome == "loss")

    @property
    def win_rate(self):
        decided = self.wins + self.losses
        return (self.wins / decided * 100) if decided else None

    @property
    def total_r(self) -> float:
        return sum(t.r_multiple for t in self.resolved_trades)

    @property
    def avg_r(self):
        return (self.total_r / self.total_trades) if self.total_trades else None

    @property
    def profit_factor(self):
        gains = sum(t.r_multiple for t in self.resolved_trades if t.r_multiple > 0)
        losses = abs(sum(t.r_multiple for t in self.resolved_trades if t.r_multiple < 0))
        if losses == 0:
            return None  # تقسیم بر صفر - یعنی هیچ ضرری نبوده (خیلی به‌ندرت پیش میاد)
        return gains / losses

    @property
    def max_consecutive_losses(self) -> int:
        streak, worst = 0, 0
        for t in self.resolved_trades:
            outcome = t.outcome
            if outcome == "loss":
                streak += 1
                worst = max(worst, streak)
            elif outcome == "win":
                streak = 0
            # سربه‌سر: رشته‌ی باخت رو نه ادامه می‌ده نه قطع می‌کنه
        return worst

    @property
    def still_open_count(self) -> int:
        return sum(1 for t in self.trades if t.exit_reason == "STILL_OPEN")


def _simulate_trade(df: pd.DataFrame, entry_idx: int, direction: str, entry: float,
                     sl: float, tps: list, max_hold: int) -> tuple:
    """
    شبیه‌سازی یه معامله از کندل entry_idx به بعد.
    خروجی: (r_multiple, exit_reason, exit_price, exit_time, exit_index)
    R از سود/ضرر جهت‌دار نسبت به فاصله‌ی ورود واقعی تا SL اولیه محاسبه می‌شه.
    """
    n = len(df)
    end_idx = min(entry_idx + max_hold, n - 1)
    risk = abs(entry - sl)
    if risk == 0:
        return 0.0, "STILL_OPEN", entry, df.iloc[entry_idx]["timestamp"], entry_idx

    def r_at(exit_price: float) -> float:
        pnl = exit_price - entry if direction == "BUY" else entry - exit_price
        return pnl / risk

    best_level = 0  # 0=هیچی، 1=TP1، 2=TP2

    for j in range(entry_idx, end_idx + 1):
        row = df.iloc[j]
        low, high = float(row["low"]), float(row["high"])

        if direction == "BUY":
            hit_sl = low <= sl
            hit_tp3 = high >= tps[2]
            hit_tp2 = high >= tps[1]
            hit_tp1 = high >= tps[0]
        else:  # SELL
            hit_sl = high >= sl
            hit_tp3 = low <= tps[2]
            hit_tp2 = low <= tps[1]
            hit_tp1 = low <= tps[0]

        # فرض محافظه‌کارانه: اگه SL و TP توی یه کندل با هم لمس بشن، SL مقدمه
        if hit_sl:
            return r_at(sl), "SL", sl, row["timestamp"], j
        if hit_tp3:
            return r_at(tps[2]), "TP3", tps[2], row["timestamp"], j
        if hit_tp2:
            best_level = max(best_level, 2)
        elif hit_tp1:
            best_level = max(best_level, 1)

    # به پایان دوره‌ی نگهداری رسیدیم بدون برخورد به SL یا TP3
    last_row = df.iloc[end_idx]
    is_data_end = end_idx == n - 1

    if best_level == 2:
        r = r_at(tps[1])
        return r, ("STILL_OPEN" if is_data_end else "TP2_TIMEOUT"), tps[1], last_row["timestamp"], end_idx
    if best_level == 1:
        r = r_at(tps[0])
        return r, ("STILL_OPEN" if is_data_end else "TP1_TIMEOUT"), tps[0], last_row["timestamp"], end_idx

    # نه SL نه هیچ TP - مارک‌تومارکت با آخرین قیمت بسته‌شدن
    last_close = float(last_row["close"])
    signed_r = r_at(last_close)
    reason = "STILL_OPEN" if is_data_end else "TIMEOUT"
    return signed_r, reason, last_close, last_row["timestamp"], end_idx


def _trade_cost_r(entry: float, exit_price: float, risk: float, sim: SimulationSettings,
                  market_entry: bool, market_exit: bool) -> float:
    """کارمزد ورود + خروج و لغزش سفارش‌های بازار، بر حسب R"""
    if not risk or (sim.fee_rate <= 0 and sim.slippage <= 0):
        return 0.0
    cost = sim.fee_rate * (abs(entry) + abs(exit_price))
    if market_entry:
        cost += sim.slippage * abs(entry)
    if market_exit:
        cost += sim.slippage * abs(exit_price)
    return cost / risk


def _simulate_live_trade(df: pd.DataFrame, start_idx: int, direction: str, entry: float,
                         sl: float, tps: list, max_hold: int, breakeven_after_tp1: bool) -> dict:
    """
    مدل هم‌سان با پایش زنده (scheduler/check_signal_performance_job):
    سفارش limit روی entry، در هر کندل اول بدترین قیمت (SL) بعد بهترین (TP)،
    توی کندل ورود فقط SL، و انتقال SL به ورود بعد از TP1.
    خروجی dict با کلید status: "skipped" | "trade".
    """
    n = len(df)
    is_buy = direction == "BUY"
    opens, highs, lows, closes = (df[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close"))
    ts = df["timestamp"]

    # --- انتظار برای پر شدن سفارش ---
    wait_end = min(start_idx + max_hold - 1, n - 1)
    fill_idx = None
    fill_price = None
    for j in range(start_idx, wait_end + 1):
        o, h, l = opens[j], highs[j], lows[j]
        if (is_buy and o <= sl) or (not is_buy and o >= sl):
            return {"status": "skipped", "reason": "INVALIDATED_BEFORE_ENTRY", "index": j, "actual": o}
        if (is_buy and l <= entry) or (not is_buy and h >= entry):
            fill_idx = j
            fill_price = min(o, entry) if is_buy else max(o, entry)
            break
    if fill_idx is None:
        return {"status": "skipped", "reason": "NOT_FILLED", "index": wait_end, "actual": None}

    risk = abs(fill_price - sl)
    if risk <= 0:
        return {"status": "skipped", "reason": "INVALID_ACTUAL_GEOMETRY", "index": fill_idx, "actual": fill_price}

    def r_at(price):
        return ((price - fill_price) if is_buy else (fill_price - price)) / risk

    cur_sl = sl
    moved = False
    hold_end = min(fill_idx + max_hold, n - 1)
    for j in range(fill_idx, hold_end + 1):
        adverse = lows[j] if is_buy else highs[j]
        favorable = highs[j] if is_buy else lows[j]
        if (is_buy and adverse <= cur_sl) or (not is_buy and adverse >= cur_sl):
            return {"status": "trade", "fill_idx": fill_idx, "fill_price": fill_price, "risk": risk,
                    "exit_idx": j, "exit_price": cur_sl, "reason": "BREAKEVEN" if moved else "SL",
                    "gross_r": r_at(cur_sl), "market_exit": True, "time": ts.iloc[j]}
        if j == fill_idx:
            continue  # کندل ورود: ترتیب داخل کندل معلوم نیست، TP رو از کندل بعد چک می‌کنیم
        reached = lambda level: (favorable >= level) if is_buy else (favorable <= level)
        if reached(tps[2]):
            return {"status": "trade", "fill_idx": fill_idx, "fill_price": fill_price, "risk": risk,
                    "exit_idx": j, "exit_price": tps[2], "reason": "TP3",
                    "gross_r": r_at(tps[2]), "market_exit": False, "time": ts.iloc[j]}
        if breakeven_after_tp1 and not moved and reached(tps[0]):
            cur_sl = fill_price
            moved = True

    is_data_end = hold_end == n - 1
    last_close = closes[hold_end]
    return {"status": "trade", "fill_idx": fill_idx, "fill_price": fill_price, "risk": risk,
            "exit_idx": hold_end, "exit_price": last_close,
            "reason": "STILL_OPEN" if is_data_end else "TIMEOUT",
            "gross_r": r_at(last_close), "market_exit": True, "time": ts.iloc[hold_end]}


def run_backtest(
    df: pd.DataFrame, symbol: str, timeframe: str,
    confidence_threshold_fraction: float = None, atr_sl_mult: float = None, rr_targets: list = None,
    warmup: int = DEFAULT_WARMUP_CANDLES, max_hold: int = DEFAULT_MAX_HOLD_CANDLES,
    sim: SimulationSettings = None,
) -> BacktestResult:
    """
    sim=None یعنی مدل قبلی بدون هزینه (سازگاری)؛ برای مدل هم‌سان با پایش زنده
    و کارمزد، SimulationSettings(live_model=True, ...) بده.

    df باید کندل‌های خام (بدون اندیکاتور) و مرتب‌شده بر اساس زمان باشه.
    اندیکاتورها یه‌بار این‌جا (وکتایرایز، سریع) محاسبه می‌شن.
    فقط هندسهٔ معتبر در open کندل بعد شبیه‌سازی می‌شود؛ ردشدگی جدا گزارش
    می‌شود و به‌جای ورود واقعی سفارش یا اعتبارسنجی کامل OHLC نیست.
    """
    sim = sim or LEGACY_SIMULATION
    df = add_extended_indicators(df.copy()).reset_index(drop=True)
    n = len(df)
    trades = []
    skipped_entries = []

    def reject_entry(reason, signal_index, entry_index, proposal=None, actual_entry=None):
        targets = getattr(proposal, "tps", None)
        skipped_entries.append(BacktestSkippedEntry(
            signal_index=signal_index, entry_index=entry_index,
            signal_time=df.iloc[signal_index]["timestamp"],
            entry_time=df.iloc[entry_index]["timestamp"],
            direction=getattr(proposal, "direction", None),
            planned_entry=getattr(proposal, "entry", None), actual_entry=actual_entry,
            sl=getattr(proposal, "sl", None),
            tps=tuple(targets) if isinstance(targets, (list, tuple)) else None,
            reason=reason,
        ))

    i = warmup
    while i < n - 1:
        sub_df = df.iloc[:i + 1]
        entry_idx = i + 1
        try:
            result = build_single_result(
                sub_df, symbol, timeframe,
                confidence_threshold_fraction=confidence_threshold_fraction,
                atr_sl_mult=atr_sl_mult, rr_targets=rr_targets,
                scoring_mode=sim.scoring_mode,
                fee_rate=sim.fee_rate, slippage=sim.slippage, max_cost_r=sim.max_cost_r,
            )
        except TradeGeometryError:
            reject_entry("INVALID_SIGNAL_LEVELS", i, entry_idx)
            i += 1
            continue

        if result.direction == "NEUTRAL":
            i += 1
            continue

        try:
            _, stop_price, targets = validate_trade_geometry(
                result.direction, result.entry, result.sl, result.tps
            )
        except TradeGeometryError:
            reject_entry("INVALID_SIGNAL_LEVELS", i, entry_idx, result)
            i += 1
            continue

        if sim.live_model:
            outcome = _simulate_live_trade(
                df, entry_idx, result.direction, float(result.entry), stop_price, list(targets),
                max_hold, sim.breakeven_after_tp1,
            )
            if outcome["status"] == "skipped":
                reject_entry(outcome["reason"], i, outcome["index"], result, outcome["actual"])
                # مثل پایش زنده: تا وقتی سیگنال در انتظار ورود بوده، سیگنال جدید نمی‌گیریم
                i = outcome["index"] + 1
                continue
            gross = outcome["gross_r"]
            cost = 0.0
            if outcome["reason"] != "STILL_OPEN":
                cost = _trade_cost_r(outcome["fill_price"], outcome["exit_price"], outcome["risk"], sim,
                                     market_entry=False, market_exit=outcome["market_exit"])
            trades.append(BacktestTrade(
                entry_index=outcome["fill_idx"],
                entry_time=df.iloc[outcome["fill_idx"]]["timestamp"],
                exit_time=outcome["time"],
                direction=result.direction,
                entry=outcome["fill_price"],
                sl=stop_price,
                tps=list(targets),
                exit_price=outcome["exit_price"],
                exit_reason=outcome["reason"],
                r_multiple=round(gross - cost, 3),
                confidence=result.confidence_percent,
                gross_r=round(gross, 3),
                cost_r=round(cost, 3),
            ))
            i = outcome["exit_idx"] + 1
            continue

        # Read the column directly, retaining bool/text types rather than coercing them.
        raw_entry = df["open"].iloc[entry_idx]
        try:
            entry_price = float(raw_entry) if isinstance(raw_entry, Real) and not isinstance(raw_entry, bool) else None
        except (TypeError, ValueError, OverflowError):
            entry_price = None
        rejection = None
        if entry_price is None or not math.isfinite(entry_price) or entry_price <= 0:
            rejection = "INVALID_ENTRY_PRICE"
        elif ((result.direction == "BUY" and entry_price <= stop_price)
              or (result.direction == "SELL" and entry_price >= stop_price)):
            rejection = "ENTRY_AT_OR_BEYOND_SL"
        elif ((result.direction == "BUY" and entry_price >= targets[0])
              or (result.direction == "SELL" and entry_price <= targets[0])):
            rejection = "ENTRY_AT_OR_BEYOND_TP1"
        else:
            try:
                validate_trade_geometry(result.direction, entry_price, stop_price, targets)
            except TradeGeometryError:
                rejection = "INVALID_ACTUAL_GEOMETRY"
        if rejection is not None:
            reject_entry(rejection, i, entry_idx, result, raw_entry)
            i += 1  # No trade opened: do not consume a simulated holding period.
            continue

        r, reason, exit_price, exit_time, exit_idx = _simulate_trade(
            df, entry_idx, result.direction, entry_price, stop_price, list(targets), max_hold
        )
        gross = r
        cost = 0.0
        if reason != "STILL_OPEN":
            cost = _trade_cost_r(entry_price, exit_price, abs(entry_price - stop_price), sim,
                                 market_entry=True, market_exit=reason in ("SL", "TIMEOUT"))
        r = gross - cost

        trades.append(BacktestTrade(
            entry_index=entry_idx,
            entry_time=df.iloc[entry_idx]["timestamp"],
            exit_time=exit_time,
            direction=result.direction,
            entry=entry_price,
            sl=stop_price,
            tps=list(targets),
            exit_price=exit_price,
            exit_reason=reason,
            r_multiple=round(r, 3),
            confidence=result.confidence_percent,
            gross_r=round(gross, 3),
            cost_r=round(cost, 3),
        ))

        # تا وقتی این معامله باز بوده، سیگنال جدید حساب نمی‌کنیم (بدون هم‌پوشانی)
        i = exit_idx + 1

    return BacktestResult(
        symbol=symbol,
        timeframe=timeframe,
        candles_tested=n,
        date_from=df.iloc[0]["timestamp"] if n else None,
        date_to=df.iloc[-1]["timestamp"] if n else None,
        trades=trades,
        skipped_entries=skipped_entries,
    )


def run_walk_forward_backtest(
    df: pd.DataFrame, symbol: str, timeframe: str, segments: int,
    confidence_threshold_fraction: float = None, atr_sl_mult: float = None, rr_targets: list = None,
    warmup: int = DEFAULT_WARMUP_CANDLES, max_hold: int = DEFAULT_MAX_HOLD_CANDLES,
    sim: SimulationSettings = None,
) -> list:
    """
    داده‌ی تاریخی رو به `segments` بازه‌ی مساوی و بدون هم‌پوشانی تقسیم
    می‌کنه و روی هرکدوم جدا بک‌تست اجرا می‌کنه - هدف اینه که مشخص بشه
    استراتژی فقط روی یه بازه‌ی خاص (مثلاً یه رالی صعودی) خوب بوده
    (overfitting/شانسی) یا واقعاً روی چند دوره‌ی متفاوت پایداره.

    برای اینکه اندیکاتورهای هر بازه از همون ابدا معتبر باشن، هر بازه
    (به‌جز اولی) به‌اندازه‌ی warmup کندل از قبل خودش هم قرض می‌گیره -
    این کندل‌های قرضی صرفاً برای محاسبه‌ی اندیکاتورن، جزو بازه‌ی
    معامله‌گیری حساب نمی‌شن. بازه‌ی اول این امکان رو نداره (چیزی قبلش
    نیست)، پس عملکردش ممکنه به‌خاطر warmup داخلی، کمی محافظه‌کارانه‌تر
    از بقیه به‌نظر برسه.

    خروجی: لیستی از BacktestResult، یکی به ازای هر بازه.
    """
    n = len(df)
    segment_size = n // segments
    results = []

    for seg_idx in range(segments):
        seg_start = seg_idx * segment_size
        seg_end = n if seg_idx == segments - 1 else (seg_idx + 1) * segment_size

        fetch_start = max(0, seg_start - warmup)
        segment_df = df.iloc[fetch_start:seg_end].reset_index(drop=True)

        if fetch_start > 0:
            effective_warmup = seg_start - fetch_start  # دقیقاً برابر warmup، مگر نزدیک ابدای داده باشیم
        else:
            effective_warmup = warmup  # بازه‌ی اول - چیزی برای قرض گرفتن نیست

        effective_warmup = min(effective_warmup, max(0, len(segment_df) - 2))

        result = run_backtest(
            segment_df, symbol, timeframe,
            confidence_threshold_fraction=confidence_threshold_fraction,
            atr_sl_mult=atr_sl_mult, rr_targets=rr_targets,
            warmup=effective_warmup, max_hold=max_hold, sim=sim,
        )
        # date_from رو به شروع واقعی این بازه (نه کندل‌های قرضی warmup)
        # اصلاح می‌کنیم، وگرنه توی نمایش به‌اشتباه به‌نظر میاد بازه‌ها
        # با هم هم‌پوشانی دارن - در حالی که فقط اندیکاتورها قرض گرفته شدن
        result.date_from = df.iloc[seg_start]["timestamp"]
        results.append(result)

    return results
# ==================== کالیبراسیون وزن‌ها (Walk-Forward Grid Search) ====================

CALIBRATION_SEGMENTS = 3
CALIBRATION_CONFIDENCE_CANDIDATES = [0.15, 0.25, 0.35]
CALIBRATION_ATR_SL_MULT_CANDIDATES = [1.0, 1.5, 2.0]
CALIBRATION_MIN_TRADES = 10
CALIBRATION_STABILITY_PENALTY_WEIGHT = 2.0


@dataclass
class CalibrationCandidate:
    confidence_threshold_fraction: float
    atr_sl_mult: float
    total_trades: int
    win_rate: object
    avg_r: object
    total_r: float
    win_rate_spread: object
    composite_score: object
    reliable: bool
    skipped_count: int = 0
    skipped_reason_counts: dict = field(default_factory=dict)


def evaluate_calibration_candidate(
    symbol_dfs: list, confidence_threshold_fraction: float, atr_sl_mult: float,
    segments: int = CALIBRATION_SEGMENTS, sim: SimulationSettings = None,
) -> CalibrationCandidate:
    """
    یه ترکیب (confidence_threshold_fraction, atr_sl_mult) رو روی همه‌ی
    نمادهای داده‌شده با بک‌تست پیشرو (Walk-Forward) می‌سنجه و نتایج
    همه‌ی نمادها/بازه‌ها رو با هم جمع می‌کنه - هدف اینه که این تنظیمات
    سراسری (که روی همه‌ی کاربران/نمادها اثر می‌ذارن) بیش‌ازحد روی یه
    نماد خاص overfit نشن.
    """
    all_resolved_trades = []
    segment_win_rates = []
    skipped_count = 0
    skipped_reasons = Counter()

    for symbol, timeframe, df in symbol_dfs:
        try:
            results = run_walk_forward_backtest(
                df, symbol, timeframe, segments=segments,
                confidence_threshold_fraction=confidence_threshold_fraction,
                atr_sl_mult=atr_sl_mult, sim=sim,
            )
        except Exception:
            continue  # این نماد رو رد می‌کنیم، بقیه رو ادامه می‌دیم

        for r in results:
            all_resolved_trades.extend(r.resolved_trades)
            skipped_count += r.skipped_count
            skipped_reasons.update(r.skipped_reason_counts)
            if r.win_rate is not None:
                segment_win_rates.append(r.win_rate)

    total_trades = len(all_resolved_trades)
    if total_trades:
        wins = sum(1 for t in all_resolved_trades if t.outcome == "win")
        decided = sum(1 for t in all_resolved_trades if t.outcome != "breakeven")
        win_rate = (wins / decided * 100) if decided else None
        total_r = sum(t.r_multiple for t in all_resolved_trades)
        avg_r = total_r / total_trades
    else:
        win_rate = None
        total_r = 0.0
        avg_r = None

    if len(segment_win_rates) >= 2:
        win_rate_spread = max(segment_win_rates) - min(segment_win_rates)
    else:
        win_rate_spread = None

    reliable = total_trades >= CALIBRATION_MIN_TRADES

    if reliable and avg_r is not None:
        penalty = (win_rate_spread / 100) * CALIBRATION_STABILITY_PENALTY_WEIGHT if win_rate_spread is not None else 0.0
        composite_score = avg_r - penalty
    else:
        composite_score = None

    return CalibrationCandidate(
        confidence_threshold_fraction=confidence_threshold_fraction,
        atr_sl_mult=atr_sl_mult,
        total_trades=total_trades,
        win_rate=win_rate,
        avg_r=avg_r,
        total_r=total_r,
        win_rate_spread=win_rate_spread,
        composite_score=composite_score,
        reliable=reliable,
        skipped_count=skipped_count,
        skipped_reason_counts=dict(skipped_reasons),
    )


def run_weight_calibration(symbol_dfs: list, segments: int = CALIBRATION_SEGMENTS,
                           sim: SimulationSettings = None) -> list:
    """
    گرید-سرچ روی ترکیب‌های confidence_threshold_fraction × atr_sl_mult؛
    خروجی لیستی از CalibrationCandidate، مرتب‌شده: قابل‌اتکاها اول
    (بر اساس composite_score نزولی)، بعد غیرقابل‌اتکاها (صرفاً برای
    نمایش تعداد معامله، نه پیشنهاد - چون آمارشون کافی نیست).
    """
    candidates = []
    for confidence_threshold_fraction in CALIBRATION_CONFIDENCE_CANDIDATES:
        for atr_sl_mult in CALIBRATION_ATR_SL_MULT_CANDIDATES:
            candidates.append(evaluate_calibration_candidate(
                symbol_dfs, confidence_threshold_fraction, atr_sl_mult, segments=segments, sim=sim,
            ))

    reliable = [c for c in candidates if c.reliable]
    unreliable = [c for c in candidates if not c.reliable]
    reliable.sort(key=lambda c: c.composite_score, reverse=True)
    unreliable.sort(key=lambda c: c.total_trades, reverse=True)
    return reliable + unreliable
