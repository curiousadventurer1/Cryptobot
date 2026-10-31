"""Pure validation of initial trade levels; no prediction, sorting, or DB writes."""
import math
from numbers import Real


class TradeGeometryError(ValueError):
    """A proposed output cannot safely be displayed or registered as a trade."""


def _positive_finite_price(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TradeGeometryError(f"{name} must be a finite positive real number")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise TradeGeometryError(f"{name} cannot be represented as a price") from error
    if not math.isfinite(normalized) or normalized <= 0:
        raise TradeGeometryError(f"{name} must be a finite positive real number")
    return normalized


def validate_trade_geometry(direction: str, entry, sl, tps) -> tuple:
    """
    Strict initial BUY/SELL geometry with exactly three targets.
    Returns SQLite-safe floats without changing the input or inventing targets.
    This is NOT a validator for already-active breakeven stops.
    """
    if not isinstance(direction, str) or direction not in ("BUY", "SELL"):
        raise TradeGeometryError("Initial trade direction must be BUY or SELL")
    if not isinstance(tps, (list, tuple)) or len(tps) != 3:
        raise TradeGeometryError("Exactly three ordered targets are required")
    entry_price = _positive_finite_price(entry, "Entry")
    stop_price = _positive_finite_price(sl, "SL")
    targets = tuple(_positive_finite_price(value, f"TP{index}")
                    for index, value in enumerate(tps, 1))
    tp1, tp2, tp3 = targets
    if direction == "BUY":
        ordered = stop_price < entry_price < tp1 < tp2 < tp3
    else:
        ordered = tp3 < tp2 < tp1 < entry_price < stop_price
    if not ordered:
        raise TradeGeometryError("Entry, SL and TP1/TP2/TP3 have invalid initial ordering")
    risk = abs(entry_price - stop_price)
    if not math.isfinite(risk) or risk <= 0:
        raise TradeGeometryError("Initial risk distance must be finite and positive")
    if any(not math.isfinite(abs(target - entry_price) / risk) for target in targets):
        raise TradeGeometryError("Risk/reward ratios must be finite")
    return entry_price, stop_price, targets


def validate_signal_output(direction: str, price, entry, sl, tps) -> tuple | None:
    """NEUTRAL requires a valid market price, but is not a trackable trade."""
    _positive_finite_price(price, "Market price")
    if isinstance(direction, str) and direction == "NEUTRAL":
        return None
    return validate_trade_geometry(direction, entry, sl, tps)