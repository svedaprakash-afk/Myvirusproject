"""Exchange-rate lookups and conversions. Rates are base-currency per 1 unit, x1e6, all integer arithmetic."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ExchangeRate

MICRO = 1_000_000


class MissingRate(ValueError):
    pass


def to_base(amount_minor: int, rate_micro: int) -> int:
    """Convert minor units of a foreign currency to base minor units (both 2-decimal currencies)."""
    return int((Decimal(amount_minor) * rate_micro / MICRO).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_base(base_minor: int, rate_micro: int) -> int:
    return int((Decimal(base_minor) * MICRO / rate_micro).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def rate_on(db: Session, currency: str, d: date, base: str = "INR") -> int | None:
    """Latest rate on or before d (banks publish no weekend rates). None when no rate exists at all."""
    if currency == base:
        return MICRO
    r = db.execute(select(ExchangeRate).where(ExchangeRate.currency == currency, ExchangeRate.rate_date <= d)
                   .order_by(ExchangeRate.rate_date.desc()).limit(1)).scalars().first()
    return r.rate_micro if r else None


def need_rate(db: Session, currency: str, d: date, base: str = "INR") -> int:
    r = rate_on(db, currency, d, base)
    if r is None:
        raise MissingRate(f"No {currency}/{base} exchange rate on or before {d}. Add one under Settings → FX rates.")
    return r


def parse_rate(text: str) -> int:
    d = Decimal(str(text).strip())
    if d <= 0:
        raise ValueError("rate must be positive")
    return int((d * MICRO).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
