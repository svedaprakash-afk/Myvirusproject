"""TDS-aware expectation: what a customer is likely to have withheld from an invoice."""
from __future__ import annotations

from dataclasses import dataclass

from ..indian import CANDIDATE_TDS_RATES_BP, default_tds_section_for_bp
from ..models import Customer, Invoice
from ..money import pct_of


@dataclass(frozen=True)
class TdsScenario:
    rate_bp: int
    base: str            # taxable | gross
    section: str | None
    usual: bool          # equals the customer's configured/learned habit


def scenarios_for(customer: Customer | None) -> list[TdsScenario]:
    """Customer's usual scenario first, then generic candidates. 0% is handled by the caller."""
    out: list[TdsScenario] = []
    seen: set[tuple[int, str]] = set()
    if customer and customer.tds_rate_bp:
        out.append(TdsScenario(customer.tds_rate_bp, customer.tds_base or "taxable",
                               customer.tds_section or default_tds_section_for_bp(customer.tds_rate_bp), True))
        seen.add((customer.tds_rate_bp, customer.tds_base or "taxable"))
    for bp in CANDIDATE_TDS_RATES_BP:
        for base in ("taxable", "gross"):
            if (bp, base) not in seen:
                out.append(TdsScenario(bp, base, default_tds_section_for_bp(bp), False))
                seen.add((bp, base))
    return out


def tds_on(inv: Invoice, sc: TdsScenario, amount_cleared: int | None = None) -> int:
    """TDS on an invoice. TDS under 194C/194J etc. is normally computed on the value EXCLUDING GST when GST is
    shown separately — hence 'taxable' is the default base. Credit notes carry no TDS here."""
    if inv.open_minor <= 0 or inv.total_minor <= 0:
        return 0
    base_full = inv.taxable_minor if sc.base == "taxable" else inv.total_minor
    # pro-rate for part-paid invoices
    base = base_full if inv.open_minor == inv.total_minor else base_full * inv.open_minor // inv.total_minor
    return pct_of(base, sc.rate_bp)
