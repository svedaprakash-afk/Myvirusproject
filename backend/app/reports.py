"""Statutory and finance reports: TDS register with Form 16A due dates, receivables ageing and the expected-credit-loss style provision.

All figures are base currency. These are decision aids built on the matching ledger, not filed returns.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .analytics import Conv
from .indian import financial_year
from .models import (DEFAULT_ENTITY_CONFIG, Customer, Invoice, LegalEntity, OnAccountBalance, TdsEntry)

# Provision matrix: share of each overdue bucket expected to be uncollectable, in basis points. ILLUSTRATIVE defaults only:
# the right percentages come from your credit-loss history, your auditor and your accounting policy (Ind AS 109 simplified approach).
DEFAULT_PROVISION_MATRIX = [
    {"from_days": 0, "pct_bp": 50}, {"from_days": 31, "pct_bp": 200}, {"from_days": 61, "pct_bp": 500},
    {"from_days": 91, "pct_bp": 1500}, {"from_days": 181, "pct_bp": 4000}, {"from_days": 366, "pct_bp": 10000},
]
BUCKETS = [("not_due", None, 0), ("1-30", 1, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("91-180", 91, 180), ("181-365", 181, 365), ("365+", 366, None)]


# ------------------------------------------------------------------------------------------------ TDS register
def form16a_due(fy: str, quarter: str) -> date:
    """Latest date a deductor should issue Form 16A for the quarter: 15 days after the TDS return due date
    (Q1 31 Jul, Q2 31 Oct, Q3 31 Jan, Q4 31 May). Statutory dates can be extended by the CBDT: confirm against the current notification."""
    y = int(fy[:4])
    return {"Q1": date(y, 8, 15), "Q2": date(y, 11, 15), "Q3": date(y + 1, 2, 15), "Q4": date(y + 1, 6, 15)}[quarter]


def tds_register(db: Session, entity_id: int, fy: str | None = None, quarter: str | None = None, customer_id: int | None = None,
                 today: date | None = None) -> dict:
    """Income-tax TDS credits grouped by financial year, quarter, customer and section, with certificate and 26AS status."""
    today = today or date.today()
    q = select(TdsEntry).where(TdsEntry.entity_id == entity_id, TdsEntry.reversed.is_(False), TdsEntry.kind == "income_tax")
    if fy:
        q = q.where(TdsEntry.fy == fy)
    if quarter:
        q = q.where(TdsEntry.quarter == quarter)
    if customer_id:
        q = q.where(TdsEntry.customer_id == customer_id)
    groups: dict[tuple, dict] = {}
    custs = {c.id: c for c in db.execute(select(Customer).where(Customer.entity_id == entity_id)).scalars()}
    for t in db.execute(q.order_by(TdsEntry.fy, TdsEntry.quarter, TdsEntry.txn_date)).scalars():
        k = (t.fy, t.quarter, t.customer_id, t.section or "")
        g = groups.setdefault(k, {"fy": t.fy, "quarter": t.quarter, "customer_id": t.customer_id, "customer": custs[t.customer_id].name,
                                  "customer_code": custs[t.customer_id].code, "pan": custs[t.customer_id].pan, "section": t.section or "", "entries": 0,
                                  "amount_minor": 0, "certificate_minor": 0, "pending_minor": 0, "in_26as_minor": 0, "not_in_26as_minor": 0,
                                  "first_date": t.txn_date, "last_date": t.txn_date})
        g["entries"] += 1
        g["amount_minor"] += t.amount_minor
        g["certificate_minor" if t.certificate_received else "pending_minor"] += t.amount_minor
        g["in_26as_minor" if t.in_26as else "not_in_26as_minor"] += t.amount_minor
        g["last_date"] = max(g["last_date"], t.txn_date)
    rows = []
    for g in groups.values():
        due = form16a_due(g["fy"], g["quarter"])
        g["certificate_due"] = due
        g["overdue"] = g["pending_minor"] > 0 and today > due
        g["days_overdue"] = (today - due).days if g["overdue"] else 0
        rows.append(g)
    rows.sort(key=lambda r: (r["fy"], r["quarter"], r["customer"], r["section"]))
    tot = lambda k: sum(r[k] for r in rows)
    return {"rows": rows, "totals": {k: tot(k) for k in ("amount_minor", "certificate_minor", "pending_minor", "in_26as_minor", "not_in_26as_minor")},
            "overdue_minor": sum(r["pending_minor"] for r in rows if r["overdue"]), "overdue_customers": len({r["customer_id"] for r in rows if r["overdue"]}),
            "note": "Quarter is taken from the receipt date. Form 16A due dates are the statutory ones (return due date + 15 days); extensions by the CBDT are not tracked."}


# ------------------------------------------------------------------------------------------------ ageing and provision
def provision_matrix(cfg: dict) -> list[dict]:
    return sorted(cfg.get("provision_matrix") or DEFAULT_PROVISION_MATRIX, key=lambda r: r["from_days"])


def pct_for(matrix: list[dict], days_overdue: int) -> int:
    pct = 0
    for r in matrix:
        if days_overdue >= r["from_days"]:
            pct = r["pct_bp"]
    return pct


def bucket_of(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "not_due"
    for name, lo, hi in BUCKETS[1:]:
        if days_overdue >= lo and (hi is None or days_overdue <= hi):
            return name
    return "365+"


def ageing_provision(db: Session, entity: LegalEntity, as_of: date | None = None) -> dict:
    """Open invoices by days past due per customer, netted against credit notes and cash held on account (oldest items first), with the provision.
    Disputed invoices are provided at the higher of the matrix rate and `provision_disputed_bp`."""
    as_of = as_of or date.today()
    cfg = {**DEFAULT_ENTITY_CONFIG, **(entity.config or {})}
    matrix = provision_matrix(cfg)
    disp_bp = int(cfg.get("provision_disputed_bp", 5000))
    cv = Conv(db, entity.id, as_of)
    custs = {c.id: c for c in db.execute(select(Customer).where(Customer.entity_id == entity.id)).scalars()}
    inv_by: dict[int, list[Invoice]] = defaultdict(list)
    credit_by: dict[int, int] = defaultdict(int)
    for i in db.execute(select(Invoice).where(Invoice.entity_id == entity.id, Invoice.open_minor != 0, Invoice.invoice_date <= as_of)).scalars():
        if i.open_minor > 0:
            inv_by[i.customer_id].append(i)
        else:
            credit_by[i.customer_id] += cv(-i.open_minor, i.currency)
    advance_by: dict[int, int] = defaultdict(int)
    for b in db.execute(select(OnAccountBalance).where(OnAccountBalance.entity_id == entity.id, OnAccountBalance.status == "open")).scalars():
        advance_by[b.customer_id] += cv(b.remaining_minor, b.currency)

    rows, totals = [], {"gross_minor": 0, "credits_minor": 0, "advances_minor": 0, "net_minor": 0, "provision_minor": 0, **{b[0]: 0 for b in BUCKETS}}
    for cid in sorted(set(inv_by) | set(credit_by) | set(advance_by), key=lambda c: custs[c].name if c in custs else ""):
        if cid not in custs:
            continue
        items = []
        for i in sorted(inv_by.get(cid, []), key=lambda i: (i.due_date, i.id)):
            items.append({"inv": i, "amount": cv(i.open_minor, i.currency), "days": (as_of - i.due_date).days})
        gross = sum(x["amount"] for x in items)
        net_off = credit_by.get(cid, 0) + advance_by.get(cid, 0)
        left = net_off
        for x in items:               # credits and advances settle the oldest items first
            take = min(left, x["amount"])
            x["net"] = x["amount"] - take
            left -= take
        buckets = {b[0]: 0 for b in BUCKETS}
        prov = 0
        disputed = 0
        for x in items:
            buckets[bucket_of(x["days"])] += x["net"]
            bp = pct_for(matrix, x["days"]) if x["days"] > 0 else pct_for(matrix, 0)
            if x["inv"].disputed:
                bp = max(bp, disp_bp)
                disputed += x["net"]
            prov += x["net"] * bp // 10000
        net = max(gross - net_off, 0)
        row = {"customer_id": cid, "customer": custs[cid].name, "code": custs[cid].code, "tier": custs[cid].tier, "gross_minor": gross,
               "credits_minor": credit_by.get(cid, 0), "advances_minor": advance_by.get(cid, 0), "unabsorbed_credit_minor": max(net_off - gross, 0),
               "net_minor": net, "disputed_minor": disputed, "provision_minor": prov, "oldest_days_overdue": max((x["days"] for x in items), default=0), **buckets}
        rows.append(row)
        for k in totals:
            totals[k] += row.get(k, 0)
    rows.sort(key=lambda r: -r["provision_minor"])
    return {"as_of": as_of, "currency": entity.base_currency, "rows": rows, "totals": totals, "matrix": matrix, "disputed_bp": disp_bp,
            "missing_rates": sorted(cv.missing), "coverage_pct": round(totals["provision_minor"] * 100 / totals["net_minor"], 2) if totals["net_minor"] else 0.0,
            "note": ("Illustrative matrix unless you have set your own. Credit notes and cash on account are netted against the oldest invoices first. "
                     "Foreign-currency items are converted at the rate on the as-of date; items with no rate are excluded and listed under missing_rates.")}


# ------------------------------------------------------------------------------------------------ CSV
def _money(m: int) -> str:
    return f"{m / 100:.2f}"


def csv_tds_register(data: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["FY", "Quarter", "Customer code", "Customer", "PAN", "Section", "Entries", "TDS amount", "Certificate received", "Certificate pending",
                "In 26AS", "Not in 26AS", "Form 16A due", "Overdue days"])
    for r in data["rows"]:
        w.writerow([r["fy"], r["quarter"], r["customer_code"], r["customer"], r["pan"] or "", r["section"], r["entries"], _money(r["amount_minor"]),
                    _money(r["certificate_minor"]), _money(r["pending_minor"]), _money(r["in_26as_minor"]), _money(r["not_in_26as_minor"]),
                    r["certificate_due"].isoformat(), r["days_overdue"]])
    return buf.getvalue()


def csv_ageing(data: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = [b[0] for b in BUCKETS]
    w.writerow(["Customer code", "Customer", "Tier", "Gross open", "Credit notes", "Cash on account", "Net exposure", "Disputed", *cols, "Oldest days overdue", "Provision"])
    for r in data["rows"]:
        w.writerow([r["code"], r["customer"], r["tier"], _money(r["gross_minor"]), _money(r["credits_minor"]), _money(r["advances_minor"]), _money(r["net_minor"]),
                    _money(r["disputed_minor"]), *[_money(r[c]) for c in cols], r["oldest_days_overdue"], _money(r["provision_minor"])])
    t = data["totals"]
    w.writerow(["", "TOTAL", "", _money(t["gross_minor"]), _money(t["credits_minor"]), _money(t["advances_minor"]), _money(t["net_minor"]), "",
                *[_money(t[c]) for c in cols], "", _money(t["provision_minor"])])
    return buf.getvalue()
