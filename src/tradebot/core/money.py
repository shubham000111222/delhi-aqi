"""Decimal money helpers. All money math in the system is Decimal, never float."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")

Number = Decimal | int | str | float


def D(value: Number) -> Decimal:
    """Parse anything into a Decimal without float contamination.

    Handles numpy scalars too: in numpy 2, ``repr(np.float64(1.5))`` is
    ``'np.float64(1.5)'``, which ``Decimal`` refuses - so we go through ``float()``.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("refusing to convert bool to Decimal")
    if isinstance(value, (int, str)):
        return Decimal(value)
    if isinstance(value, float):  # note: numpy.float64 subclasses float
        return Decimal(repr(float(value)))
    try:
        return Decimal(repr(float(value)))  # numpy scalars, Fraction, etc.
    except (TypeError, ValueError) as exc:
        raise TypeError(f"cannot convert {type(value)!r} to Decimal") from exc


def quantize_step(value: Number, step: Number, rounding: str = ROUND_FLOOR) -> Decimal:
    """Snap a value down to a multiple of `step` (tick size / lot step)."""
    v, s = D(value), D(step)
    if s <= 0:
        return v
    return (v / s).to_integral_value(rounding=rounding) * s


def round_price(value: Number, tick: Number, side: str | None = None) -> Decimal:
    """Round a price to the tick grid.

    For limit orders we round in the passive direction so we never accidentally
    cross the spread more than intended: buys round down, sells round up.
    """
    v, t = D(value), D(tick)
    if t <= 0:
        return v
    if side == "buy":
        return quantize_step(v, t, ROUND_FLOOR)
    if side == "sell":
        return quantize_step(v, t, ROUND_CEILING)
    return quantize_step(v, t, ROUND_HALF_UP)


def round_qty(value: Number, step: Number) -> Decimal:
    """Quantities always round *down*: never send more than intended."""
    return quantize_step(value, step, ROUND_FLOOR)


def pct(part: Number, whole: Number) -> Decimal:
    """part / whole as a Decimal fraction, 0 when whole is 0."""
    w = D(whole)
    if w == 0:
        return ZERO
    return D(part) / w


def safe_div(a: Number, b: Number, default: Decimal = ZERO) -> Decimal:
    try:
        d = D(b)
        if d == 0:
            return default
        return D(a) / d
    except (InvalidOperation, ZeroDivisionError):
        return default


def to_str(value: Decimal, places: int | None = None) -> str:
    """Stable string form for JSON / DB storage."""
    if places is not None:
        q = Decimal(1).scaleb(-places)
        return format(D(value).quantize(q, rounding=ROUND_HALF_UP), "f")
    return format(D(value).normalize(), "f")


def money_json(value: Decimal | None) -> str | None:
    return None if value is None else to_str(value, 8)
