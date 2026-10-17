"""TDS-aware expectation: what a customer is likely to have withheld from an invoice."""
from __future__ import annotations

from dataclasses import dataclass

from ..indian import CANDIDATE_TDS_RATES_BP, TDS_SECTIONS, default_tds_section_for_bp
from ..models import Customer, Invoice
from ..money import pct_of


@dataclass(frozen=True)
class TdsScenario:
    rate_bp: int
    base: str            # taxable | gross
    section: str | None
    usual: bool          # equals the customer's configured/learned habit
    threshold_minor: int = 0   # section 194Q: deduction applies only to the part of the year's purchases above this


GST_TDS_RATE_BP = TDS_SECTIONS["GST-TDS"]["rate_bp"]


def split_components(section: str | None, rate_bp: int | None, amount: int) -> list[tuple[str, str | None, int | None, int]]:
    """Break one combined deduction (e.g. '194C+GST-TDS' at 4%) into its legal components:
    [(kind, section, rate_bp, amount)], kind = income_tax | gst. The parts always add up to `amount`."""
    if not section or "GST-TDS" not in section:
        return [("income_tax", section, rate_bp, amount)]
    if section == "GST-TDS" or not rate_bp or rate_bp <= GST_TDS_RATE_BP:
        return [("gst", "GST-TDS", rate_bp, amount)]
    gst = amount * GST_TDS_RATE_BP // rate_bp
    out = [("gst", "GST-TDS", GST_TDS_RATE_BP, gst)]
    if amount - gst:
        out.insert(0, ("income_tax", section.replace("+GST-TDS", ""), rate_bp - GST_TDS_RATE_BP, amount - gst))
    return out


def scenarios_for(customer: Customer | None, cfg: dict | None = None) -> list[TdsScenario]:
    """Customer's usual scenario first, then generic candidates. 0% is handled by the caller."""
    out: list[TdsScenario] = []
    seen: set[tuple[int, str]] = set()
    if customer and customer.tds_rate_bp:
        thr = int((cfg or {}).get("tds_194q_threshold_minor", 50_00_000_00)) if customer.tds_section == "194Q" else 0
        out.append(TdsScenario(customer.tds_rate_bp, customer.tds_base or "taxable",
                               customer.tds_section or default_tds_section_for_bp(customer.tds_rate_bp), True, thr))
        seen.add((customer.tds_rate_bp, customer.tds_base or "taxable"))
    for bp in CANDIDATE_TDS_RATES_BP:
        for base in ("taxable", "gross"):
            if (bp, base) not in seen:
                out.append(TdsScenario(bp, base, default_tds_section_for_bp(bp), False))
                seen.add((bp, base))
    return out


def tds_on(inv: Invoice, sc: TdsScenario, amount_cleared: int | None = None, cum_before: int = 0) -> int:
    """TDS on an invoice. TDS under 194C/194J etc. is normally computed on the value EXCLUDING GST when GST is
    shown separately — hence 'taxable' is the default base. Credit notes carry no TDS here."""
    if inv.open_minor <= 0 or inv.total_minor <= 0:
        return 0
    base_full = inv.taxable_minor if sc.base == "taxable" else inv.total_minor
    if sc.threshold_minor:   # 194Q: only the slice of this invoice that lies above the yearly threshold is deductible
        base_full = max(0, min(base_full, cum_before + base_full - sc.threshold_minor))
    # pro-rate for part-paid invoices
    base = base_full if inv.open_minor == inv.total_minor else base_full * inv.open_minor // inv.total_minor
    return pct_of(base, sc.rate_bp)
