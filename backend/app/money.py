"""Money helpers. Everything internal is integer minor units; floats never touch money."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def to_minor(value: str | int | float | Decimal) -> int:
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor(minor: int) -> Decimal:
    return Decimal(minor) / 100


def parse_amount(raw: str | None) -> int | None:
    """Parse '1,23,456.78', '₹ 5,000', '1234.5 Cr', '(100.00)' -> signed minor units, None if blank/invalid."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in {"-", "--", "NA", "N/A"}:
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.startswith("-")
    s = re.sub(r"(?i)\b(cr|dr|inr|rs\.?)\b|₹|[,\s()+\-]", "", s)
    try:
        v = to_minor(Decimal(s))
    except InvalidOperation:
        return None
    return -v if neg else v


def pct_of(amount_minor: int, rate_bp: int) -> int:
    """amount * rate_bp / 10000 rounded half-up to the paisa."""
    return int((Decimal(amount_minor) * rate_bp / 10000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
