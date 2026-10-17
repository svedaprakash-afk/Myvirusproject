"""Form 26AS / AIS import and reconciliation against the TDS credit we took on receipts.

FORMAT STATUS: written from the published layout of the TRACES text file ('^' separated, Part A with a deductor header row followed by
detail rows) and from generic CSV/XLSX exports with named columns. It has NOT been run against a real downloaded file: import one of your
own and check the first result before relying on it. AIS is supported only through CSV/XLSX exports (its JSON is not parsed).

A quarter is taken from the date of payment/credit reported by the deductor. Banks credit us on a different day, so a receipt near a quarter
boundary can fall in a different quarter in the two sources: the reconciliation says so ("timing") instead of reporting a false difference.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..indian import financial_year, normalise_name
from ..models import Customer, Form26asEntry, TdsEntry, utcnow
from ..money import parse_amount

TAN_RE = re.compile(r"^[A-Z]{4}\d{5}[A-Z]$")
SECTION_RE = re.compile(r"^19[2-6][A-Z]{0,2}(?:\([a-z]\))?$")      # TDS sections only: Part B (TCS, 206C) is tax WE paid to sellers, not credit on our receipts
DATE_RE = re.compile(r"^\d{1,2}[-/][A-Za-z]{3}[-/]\d{4}$|^\d{1,2}[-/]\d{1,2}[-/]\d{4}$|^\d{4}-\d{2}-\d{2}(?:[ T].*)?$")
TOL = 100            # ₹1
MAX_ROWS = 20000


class Form26asError(ValueError):
    pass


@dataclass
class Row26:
    tan: str
    name: str
    section: str | None
    txn_date: date
    booking_date: date | None
    status: str | None
    paid: int
    deducted: int
    deposited: int


def _date(s: str) -> date | None:
    s = (s or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        s = s[:10]                      # spreadsheet exports append a time
    for f in ("%d-%b-%Y", "%d/%b/%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def _amt(s: str) -> int | None:
    s = (s or "").strip().replace("₹", "").replace("Rs.", "").strip()
    return parse_amount(s) if re.fullmatch(r"-?\(?[\d,]+(?:\.\d{1,2})?\)?", s) else None


# ---------------------------------------------------------------------------------------------- TRACES text
def parse_txt(text: str) -> list[Row26]:
    """'^'-separated TRACES layout. A deductor line carries a TAN; detail lines carry a section, a transaction date, a booking status and amounts."""
    out: list[Row26] = []
    tan = name = None
    for raw in text.splitlines():
        if "^" not in raw:
            continue
        cells = [c.strip() for c in raw.split("^")]
        tans = [c for c in cells if TAN_RE.match(c)]
        if tans:
            tan = tans[0]
            names = [c for c in cells if c and c != tan and not re.fullmatch(r"[\d,.\-]+", c)]
            name = names[0] if names else ""
            continue
        sec = next((c for c in cells if SECTION_RE.match(c)), None)
        dates = [c for c in cells if DATE_RE.match(c)]
        if not (tan and sec and dates):
            continue
        status = next((c for c in cells if c in ("F", "P", "U", "O", "Z", "M", "G", "H", "A")), None)
        nums = [a for a in (_amt(c) for c in cells[cells.index(sec) + 1:] if c not in ("", "-") and not DATE_RE.match(c)) if a is not None]
        if len(nums) < 3:
            continue
        paid, ded, dep = nums[-3:]
        td = _date(dates[0])
        if td is None:
            continue
        out.append(Row26(tan, name or "", sec, td, _date(dates[1]) if len(dates) > 1 else None, status, paid, ded, dep))
        if len(out) > MAX_ROWS:
            raise Form26asError("File has too many rows")
    return out


# ---------------------------------------------------------------------------------------------- CSV / XLSX with named columns
_COLS = {
    "tan": r"(?:deductor\s*)?tan(?:\s*of\s*deductor)?|tan\s*no\.?",
    "name": r"name\s*of\s*(?:the\s*)?deductor|deductor(?:\s*name)?|information\s*source(?:\s*name)?|deducted\s*by",
    "section": r"section(?:\s*code)?|information\s*description",
    "date": r"(?:transaction\s*date|date\s*of\s*(?:payment|transaction)(?:\s*/\s*credit)?|payment\s*date|date)",
    "bdate": r"date\s*of\s*booking|booking\s*date",
    "status": r"status\s*of\s*booking|booking\s*status|status",
    "paid": r"amount\s*paid\s*/?\s*(?:credited)?(?:\s*\(?rs\.?\)?)?|amount\s*reported|amount\s*paid",
    "deducted": r"tax\s*deducted(?:\s*\(?rs\.?\)?)?|tds\s*deducted",
    "deposited": r"(?:total\s*)?tds\s*deposited(?:\s*\(?rs\.?\)?)?|tax\s*deposited|tds\s*amount",
}


def _kind(cell: str) -> str | None:
    c = re.sub(r"\s+", " ", cell.strip().lower())
    for k in ("bdate", "deposited", "deducted", "paid", "status", "section", "tan", "name", "date"):   # most specific first
        if re.fullmatch(_COLS[k], c):
            return k
    return None


def parse_table(text: str) -> list[Row26]:
    lines = text.splitlines()
    for hi, raw in enumerate(lines):
        cells = [c.strip() for c in raw.split("\t")]
        kinds = [_kind(c) for c in cells]
        if "date" not in kinds or not ({"deducted", "deposited"} & set(kinds)) or not ({"tan", "name"} & set(kinds)):
            continue
        ix = {k: kinds.index(k) for k in set(kinds) if k}
        out: list[Row26] = []
        for r in lines[hi + 1:]:
            p = [c.strip() for c in r.split("\t")]
            if len(p) < len(cells) or not any(p):
                continue
            g = lambda k: p[ix[k]] if k in ix and ix[k] < len(p) else ""
            td = _date(g("date"))
            ded, dep = _amt(g("deducted")), _amt(g("deposited"))
            if td is None or (ded is None and dep is None):
                continue
            tan = g("tan").upper()
            if tan and not TAN_RE.match(tan):
                continue
            out.append(Row26(tan, g("name"), g("section") or None, td, _date(g("bdate")), (g("status") or "")[:1].upper() or None,
                             _amt(g("paid")) or 0, ded if ded is not None else dep, dep if dep is not None else ded))
            if len(out) > MAX_ROWS:
                raise Form26asError("File has too many rows")
        return out
    return []


def parse_upload(filename: str, data: bytes) -> list[Row26]:
    from .remittance import extract_upload

    n = (filename or "").lower()
    if n.endswith(".json"):
        raise Form26asError("AIS JSON is not supported: export the TDS section of AIS as CSV or XLSX and import that")
    if n.endswith(".pdf"):
        raise Form26asError("PDF statements are not supported: download the 26AS text file from TRACES (or export CSV/XLSX)")
    text = data.decode("utf-8-sig", errors="replace") if n.endswith((".txt", ".csv", ".tsv")) and "^" in data[:5000].decode("utf-8", "replace") else None
    if text is not None:
        rows = parse_txt(text)
    else:
        rows = parse_table(extract_upload(filename, data).text)
    if not rows:
        raise Form26asError("No TDS lines were recognised. Expected the TRACES 26AS text file or a CSV/XLSX with columns such as Deductor TAN, Section, "
                            "Transaction date, Tax deducted and TDS deposited.")
    return rows


# ---------------------------------------------------------------------------------------------- import
def resolve_customer(db: Session, entity_id: int, tan: str, name: str, customers: list[Customer]) -> tuple[Customer | None, str | None]:
    """TAN first; else an unambiguous name match, which also records the TAN on the customer (visible and editable)."""
    for c in customers:
        if c.tan == tan:
            return c, None
    from rapidfuzz import fuzz

    n = normalise_name(name)
    if not n:
        return None, None
    scored = sorted(((fuzz.token_sort_ratio(n, normalise_name(c.name)), c) for c in customers if not c.tan), key=lambda t: -t[0])
    if scored and scored[0][0] >= 92 and (len(scored) == 1 or scored[1][0] <= scored[0][0] - 8):
        c = scored[0][1]
        c.tan = tan
        return c, (f"{name} -> {c.name} ({scored[0][0]:.0f}% name match): check the TAN" if scored[0][0] < 99 else None)
    return None, None


def import_entries(db: Session, entity_id: int, rows: list[Row26], source: str, actor: str, actor_id: int | None) -> dict:
    source = source.upper()
    if source not in ("26AS", "AIS"):
        raise Form26asError("source must be 26AS or AIS")
    customers = list(db.execute(select(Customer).where(Customer.entity_id == entity_id)).scalars())
    existing = {e.dedupe_key: e for e in db.execute(select(Form26asEntry).where(Form26asEntry.entity_id == entity_id, Form26asEntry.source == source)).scalars()}
    seen: dict[str, int] = defaultdict(int)
    ins = upd = same = 0
    notes: list[str] = []
    cache: dict[str, Customer | None] = {}
    for r in rows:
        base = f"{r.tan}|{r.section}|{r.txn_date}|{r.paid}|{r.deducted}"
        seen[base] += 1
        key = hashlib.sha256(f"{base}|{seen[base]}".encode()).hexdigest()
        if r.tan not in cache:
            cache[r.tan], note = resolve_customer(db, entity_id, r.tan, r.name, customers)
            if note:
                notes.append(note)
        cust = cache[r.tan]
        fy, q = financial_year(r.txn_date)
        e = existing.get(key)
        if e is None:
            db.add(Form26asEntry(entity_id=entity_id, source=source, fy=fy, quarter=q, deductor_tan=r.tan, deductor_name=r.name[:250], section=r.section,
                                 txn_date=r.txn_date, booking_date=r.booking_date, booking_status=r.status, amount_paid_minor=r.paid, tax_deducted_minor=r.deducted,
                                 tds_deposited_minor=r.deposited, customer_id=cust.id if cust else None, dedupe_key=key))
            ins += 1
        else:
            changed = (e.booking_status, e.booking_date, e.tds_deposited_minor) != (r.status, r.booking_date, r.deposited)
            if changed:      # provisional lines become final over time
                e.booking_status, e.booking_date, e.tds_deposited_minor = r.status, r.booking_date, r.deposited
                upd += 1
            else:
                same += 1
            if cust and not e.customer_id:
                e.customer_id = cust.id
    db.flush()
    unmapped = sorted({(r.tan, r.name) for r in rows if cache.get(r.tan) is None})
    audit.log(db, actor, "tax.26as_imported", "LegalEntity", entity_id, entity_id, {"source": source, "inserted": ins, "updated": upd, "unchanged": same,
                                                                                 "unmapped_deductors": len(unmapped)}, actor_id)
    return {"source": source, "rows": len(rows), "inserted": ins, "updated": upd, "unchanged": same, "mapped_notes": notes,
            "unmapped_deductors": [{"tan": t, "name": n} for t, n in unmapped]}


def map_deductor(db: Session, entity_id: int, tan: str, customer_id: int, actor: str, actor_id: int) -> int:
    tan = tan.upper().strip()
    if not TAN_RE.match(tan):
        raise Form26asError("A TAN is 4 letters, 5 digits and 1 letter")
    c = db.get(Customer, customer_id)
    if not c or c.entity_id != entity_id:
        raise Form26asError("Customer not found in this entity")
    other = db.execute(select(Customer).where(Customer.entity_id == entity_id, Customer.tan == tan, Customer.id != c.id)).scalars().first()
    if other:
        raise Form26asError(f"TAN {tan} is already assigned to {other.name}")
    c.tan = tan
    n = 0
    for e in db.execute(select(Form26asEntry).where(Form26asEntry.entity_id == entity_id, Form26asEntry.deductor_tan == tan)).scalars():
        if e.customer_id != c.id:
            e.customer_id, n = c.id, n + 1
    audit.log(db, actor, "tax.26as_mapped", "Customer", c.id, entity_id, {"tan": tan, "entries_linked": n}, actor_id)
    return n


# ---------------------------------------------------------------------------------------------- reconciliation
def _counted(e: Form26asEntry) -> int:
    return e.tds_deposited_minor or e.tax_deducted_minor


def reconcile(db: Session, entity_id: int, fy: str, source: str = "26AS", apply_flags: bool = False) -> dict:
    ours: dict[tuple[int, str], list[TdsEntry]] = defaultdict(list)
    for t in db.execute(select(TdsEntry).where(TdsEntry.entity_id == entity_id, TdsEntry.fy == fy, TdsEntry.kind == "income_tax", TdsEntry.reversed.is_(False))
                        .order_by(TdsEntry.txn_date, TdsEntry.id)).scalars():
        ours[(t.customer_id, t.quarter)].append(t)
    theirs: dict[tuple[int, str], list[Form26asEntry]] = defaultdict(list)
    unmapped: dict[tuple[str, str], int] = defaultdict(int)
    for e in db.execute(select(Form26asEntry).where(Form26asEntry.entity_id == entity_id, Form26asEntry.fy == fy, Form26asEntry.source == source)).scalars():
        if e.customer_id:
            theirs[(e.customer_id, e.quarter)].append(e)
        else:
            unmapped[(e.deductor_tan, e.deductor_name)] += _counted(e)
    names = {c.id: c for c in db.execute(select(Customer).where(Customer.entity_id == entity_id)).scalars()}
    rows, by_cust = [], defaultdict(lambda: [0, 0])
    for key in sorted(set(ours) | set(theirs), key=lambda k: (names[k[0]].name, k[1])):
        cid, q = key
        o = sum(t.amount_minor for t in ours.get(key, []))
        final = sum(_counted(e) for e in theirs.get(key, []) if e.booking_status in (None, "F"))
        other = sum(_counted(e) for e in theirs.get(key, []) if e.booking_status not in (None, "F"))
        by_cust[cid][0] += o
        by_cust[cid][1] += final
        rows.append({"customer_id": cid, "customer": names[cid].name, "code": names[cid].code, "tan": names[cid].tan, "quarter": q, "ours_minor": o,
                     "reported_minor": final, "unconfirmed_minor": other, "diff_minor": o - final, "entries": len(ours.get(key, [])),
                     "sections_ours": sorted({t.section or "" for t in ours.get(key, [])}), "sections_reported": sorted({e.section or "" for e in theirs.get(key, [])})})
    for r in rows:
        d = r["diff_minor"]
        fy_diff = by_cust[r["customer_id"]][0] - by_cust[r["customer_id"]][1]
        if abs(d) <= TOL:
            r["status"], r["action"] = "matched", ""
        elif abs(fy_diff) <= TOL:
            r["status"], r["action"] = "timing", "Financial-year totals agree: the receipt and the deductor's payment fall in different quarters."
        elif r["ours_minor"] and not r["reported_minor"] and not r["unconfirmed_minor"]:
            r["status"], r["action"] = "not_reported", "Nothing for this quarter in the statement. Ask the customer whether they filed their TDS return; do not rely on this credit yet."
        elif d > 0:
            r["status"] = "ours_higher"
            r["action"] = ("Part of it is provisional or unmatched in the statement: follow up." if r["unconfirmed_minor"] else
                           "We took more credit than the deductor reported. Ask them to correct or file their return.")
        else:
            r["status"], r["action"] = "reported_higher", "The statement shows more TDS than we recorded. Check receipts where a TDS shortfall was written off or paid as short-pay."
    if apply_flags:
        flagged = 0
        for r in rows:
            if r["status"] in ("not_reported",):
                continue
            budget = r["reported_minor"] if r["status"] in ("ours_higher",) else None
            for t in ours.get((r["customer_id"], r["quarter"]), []):
                if t.in_26as:
                    continue
                if budget is not None:
                    if t.amount_minor > budget:
                        break
                    budget -= t.amount_minor
                t.in_26as, flagged = True, flagged + 1
        db.flush()
    else:
        flagged = 0
    unmatched_cust = [{"tan": t, "name": n, "amount_minor": a} for (t, n), a in sorted(unmapped.items(), key=lambda kv: -kv[1])]
    summ = {s: sum(1 for r in rows if r["status"] == s) for s in ("matched", "timing", "ours_higher", "reported_higher", "not_reported")}
    return {"fy": fy, "source": source, "rows": rows, "unmapped": unmatched_cust, "flagged": flagged, "summary": summ,
            "ours_minor": sum(r["ours_minor"] for r in rows), "reported_minor": sum(r["reported_minor"] for r in rows),
            "at_risk_minor": sum(r["diff_minor"] for r in rows if r["status"] in ("ours_higher", "not_reported")),
            "unclaimed_minor": sum(-r["diff_minor"] for r in rows if r["status"] == "reported_higher"),
            "note": "Only lines with booking status F (final) count as reported; provisional and unmatched lines are shown separately. Written from the published TRACES layout and not yet run on a real file."}
