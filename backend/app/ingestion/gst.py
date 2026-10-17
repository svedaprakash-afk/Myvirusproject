"""GST filing cross-checks for the receivables team.

Why this belongs in cash application: a customer usually cannot claim input tax credit, and often will not pay, until our invoice appears in
their GSTR-2B, which depends on us reporting it in GSTR-1 (and, above the e-invoicing threshold, on a valid IRN). A late payment can be our
own filing gap. These checks compare the open items from the ERP with what the GST system holds.

FORMAT STATUS: GSTR-1 JSON is read in the structure of the GST offline-tool / portal download (b2b[].inv[], cdnr[].nt[]); the IRP register is read
from CSV/XLSX exports with named columns. NEITHER HAS BEEN RUN AGAINST A REAL DOWNLOAD: import one of your own and check the counts. We do not
parse GSTR-2B: that is the customer's view of our invoices, which a supplier cannot download.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..indian import is_valid_gstin, normalise_ref
from ..models import DEFAULT_ENTITY_CONFIG, Customer, GstDocument, Invoice, LegalEntity
from ..money import parse_amount

TOL = 100
MAX_DOCS = 50000


class GstError(ValueError):
    pass


def _d(s) -> date | None:
    s = str(s or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        s = s[:10]
    for f in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d/%b/%Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def _m(v) -> int:
    """Rupee amount (number or string) to paise."""
    if v in (None, ""):
        return 0
    if isinstance(v, (int, float)):
        return int(round(float(v) * 100))
    p = parse_amount(str(v).replace("₹", "").strip())
    return p or 0


# ---------------------------------------------------------------------------------------------- GSTR-1 JSON
def parse_gstr1(data: bytes) -> tuple[list[dict], str | None]:
    try:
        j = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        raise GstError(f"Not valid JSON: {e}")
    if not isinstance(j, dict) or not ({"b2b", "cdnr"} & set(j)):
        raise GstError("This does not look like a GSTR-1 JSON: expected b2b and/or cdnr sections")
    fp = str(j.get("fp") or "")
    if not re.fullmatch(r"(0[1-9]|1[0-2])\d{4}", fp):
        raise GstError("The GSTR-1 JSON has no valid filing period (fp, like 092026)")
    docs: list[dict] = []
    for party in j.get("b2b") or []:
        ctin = (party.get("ctin") or "").upper()
        for inv in party.get("inv") or []:
            tx = sum(_m((i.get("itm_det") or {}).get("txval")) for i in inv.get("itms") or [])
            docs.append({"doc_type": "INV", "doc_no": str(inv.get("inum") or "")[:40], "doc_date": _d(inv.get("idt")), "party_gstin": ctin,
                         "taxable_minor": tx, "total_minor": _m(inv.get("val")), "irn": (inv.get("irn") or None), "period": fp})
    for party in j.get("cdnr") or []:
        ctin = (party.get("ctin") or "").upper()
        for nt in party.get("nt") or []:
            tx = sum(_m((i.get("itm_det") or {}).get("txval")) for i in nt.get("itms") or [])
            docs.append({"doc_type": "CRN" if str(nt.get("ntty", "C")).upper() == "C" else "DBN", "doc_no": str(nt.get("nt_num") or "")[:40],
                         "doc_date": _d(nt.get("nt_dt")), "party_gstin": ctin, "taxable_minor": tx, "total_minor": _m(nt.get("val")), "irn": None, "period": fp})
    docs = [d for d in docs if d["doc_no"] and d["doc_date"]]
    if not docs:
        raise GstError("No invoices or notes were found in the GSTR-1 JSON")
    if len(docs) > MAX_DOCS:
        raise GstError("File has too many documents")
    return docs, fp


# ---------------------------------------------------------------------------------------------- IRP register (CSV / XLSX)
_COLS = {
    "irn": r"irn|invoice\s*reference\s*number",
    "ack_date": r"ack(?:nowledgement)?\s*date|irn\s*date",
    "doc_no": r"doc(?:ument)?\s*(?:no\.?|number)|invoice\s*(?:no\.?|number)",
    "doc_date": r"doc(?:ument)?\s*date|invoice\s*date",
    "doc_type": r"doc(?:ument)?\s*type|supply\s*type",
    "party": r"buyer\s*gstin|recipient\s*gstin|ctin|gstin\s*of\s*(?:buyer|recipient)",
    "total": r"(?:total|invoice)\s*(?:value|amount)|val",
    "taxable": r"taxable\s*(?:value|amount)|assessable\s*value",
    "status": r"(?:irn\s*)?status|e-?invoice\s*status",
}


def _kind(cell: str) -> str | None:
    c = re.sub(r"\s+", " ", cell.strip().lower())
    for k, pat in _COLS.items():
        if re.fullmatch(pat, c):
            return k
    return None


def parse_irp(text: str) -> list[dict]:
    lines = text.splitlines()
    for hi, raw in enumerate(lines):
        cells = [c.strip() for c in raw.split("\t")]
        kinds = [_kind(c) for c in cells]
        if "irn" not in kinds or "doc_no" not in kinds:
            continue
        ix = {k: kinds.index(k) for k in set(kinds) if k}
        out = []
        for r in lines[hi + 1:]:
            p = [c.strip() for c in r.split("\t")]
            if len(p) < len(cells) or not any(p):
                continue
            g = lambda k: p[ix[k]] if k in ix and ix[k] < len(p) else ""
            dd, no = _d(g("doc_date")), g("doc_no")
            if not no or dd is None:
                continue
            t = g("doc_type").upper()
            st = g("status").upper()
            out.append({"doc_type": "CRN" if t in ("CRN", "CN") or t.startswith("CRE") else "DBN" if t in ("DBN", "DN") or t.startswith("DEB") else "INV",
                        "doc_no": no[:40], "doc_date": dd, "party_gstin": g("party").upper() or None, "taxable_minor": _m(g("taxable")), "total_minor": _m(g("total")),
                        "irn": (g("irn") or None), "ack_date": _d(g("ack_date")), "status": "CNL" if st.startswith(("CNL", "CANC")) else "ACT", "period": None})
        if out:
            return out
    raise GstError("No e-invoices were recognised. Expected a CSV/XLSX export with columns such as IRN, Document number, Document date, Total value and Status.")


def import_documents(db: Session, entity_id: int, docs: list[dict], source: str, actor: str, actor_id: int | None) -> dict:
    if source not in ("GSTR1", "IRP"):
        raise GstError("source must be GSTR1 or IRP")
    existing = {(g.doc_type, g.doc_no_norm): g for g in db.execute(select(GstDocument).where(GstDocument.entity_id == entity_id, GstDocument.source == source)).scalars()}
    ins = upd = same = 0
    for d in docs:
        key = (d["doc_type"], normalise_ref(d["doc_no"]))
        row = existing.get(key)
        vals = dict(doc_no=d["doc_no"], doc_date=d["doc_date"], party_gstin=d.get("party_gstin"), taxable_minor=d["taxable_minor"], total_minor=d["total_minor"],
                    irn=d.get("irn"), ack_date=d.get("ack_date"), status=d.get("status", "ACT"), period=d.get("period"))
        if row is None:
            row = GstDocument(entity_id=entity_id, source=source, doc_type=d["doc_type"], doc_no_norm=key[1], **vals)
            db.add(row)
            existing[key] = row
            ins += 1
        elif any(getattr(row, k) != v for k, v in vals.items()):   # a later GSTR-1 amendment or an IRN cancellation replaces the earlier record
            for k, v in vals.items():
                setattr(row, k, v)
            upd += 1
        else:
            same += 1
    db.flush()
    audit.log(db, actor, "tax.gst_imported", "LegalEntity", entity_id, entity_id, {"source": source, "inserted": ins, "updated": upd, "unchanged": same}, actor_id)
    return {"source": source, "documents": len(docs), "inserted": ins, "updated": upd, "unchanged": same}


# ---------------------------------------------------------------------------------------------- cross-check
def _period_end(d: date) -> date:
    return (d.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def _return_due(d: date, quarterly: bool) -> date:
    """When the GSTR-1 covering this invoice date falls due: the 11th after the month (monthly) or the 13th after the quarter (QRMP)."""
    if not quarterly:
        return _period_end(d) + timedelta(days=11)
    qend_month = ((d.month - 1) // 3 + 1) * 3
    return _period_end(date(d.year, qend_month, 1)) + timedelta(days=13)


def _fp_to_date(fp: str) -> date:
    return date(int(fp[2:]), int(fp[:2]), 1)


SEVERITY = {"irn_cancelled": "high", "irn_missing": "high", "not_in_gstr1": "high", "gstin_mismatch": "high", "value_mismatch": "medium",
            "irn_mismatch": "high", "not_on_irp": "medium"}
TEXT = {
    "irn_cancelled": "The IRN was cancelled on the IRP. Do not chase this invoice: raise a fresh invoice or confirm the cancellation with the customer.",
    "irn_missing": "No IRN on this invoice although e-invoicing applies to this entity. The customer may reject it for ITC.",
    "not_in_gstr1": "Not found in any imported GSTR-1 although its return period has passed. The customer cannot see it in GSTR-2B, a common reason for non-payment.",
    "gstin_mismatch": "The GSTIN in the filing differs from the customer's GSTIN in the ERP.",
    "value_mismatch": "The invoice value in the filing differs from the ERP.",
    "irn_mismatch": "The IRN in the ERP differs from the IRN registered for this document number.",
    "not_on_irp": "The ERP carries an IRN but the document is not in the imported e-invoice register.",
}


def crosscheck(db: Session, entity: LegalEntity, today: date | None = None, only_open: bool = True) -> dict:
    today = today or date.today()
    cfg = {**DEFAULT_ENTITY_CONFIG, **(entity.config or {})}
    quarterly = cfg.get("gstr1_frequency", "monthly") == "quarterly"
    require_irn = bool(cfg.get("e_invoice_required", False))
    docs = db.execute(select(GstDocument).where(GstDocument.entity_id == entity.id)).scalars().all()
    g1 = {(d.doc_type, d.doc_no_norm): d for d in docs if d.source == "GSTR1"}
    irp = {(d.doc_type, d.doc_no_norm): d for d in docs if d.source == "IRP"}
    periods = sorted({_fp_to_date(d.period) for d in docs if d.source == "GSTR1" and d.period})
    latest_period = periods[-1] if periods else None
    custs = {c.id: c for c in db.execute(select(Customer).where(Customer.entity_id == entity.id)).scalars()}
    q = select(Invoice).where(Invoice.entity_id == entity.id)
    if only_open:
        q = q.where(Invoice.open_minor != 0)
    rows, counts = [], defaultdict(int)
    for inv in db.execute(q.order_by(Invoice.invoice_date, Invoice.id)).scalars():
        if inv.currency != entity.base_currency:
            continue                       # exports are not B2B domestic supplies
        typ = "CRN" if inv.doc_type == "credit_note" else "INV"
        key = (typ, normalise_ref(inv.number))
        cust = custs.get(inv.customer_id)
        flags: list[str] = []
        r = irp.get(key)
        if r is not None:
            if r.status == "CNL":
                flags.append("irn_cancelled")
            elif inv.irn and r.irn and inv.irn.upper() != r.irn.upper():
                flags.append("irn_mismatch")
            if abs(abs(r.total_minor) - abs(inv.total_minor)) > TOL and r.total_minor:
                flags.append("value_mismatch")
        elif inv.irn and irp:
            flags.append("not_on_irp")
        if require_irn and typ in ("INV", "CRN") and not inv.irn and r is None and cust and cust.gstin and is_valid_gstin(cust.gstin):
            flags.append("irn_missing")
        g = g1.get(key)
        g1_state = "unknown"
        if g is not None:
            g1_state = "reported"
            if cust and cust.gstin and g.party_gstin and g.party_gstin != cust.gstin.upper():
                flags.append("gstin_mismatch")
            if abs(abs(g.total_minor) - abs(inv.total_minor)) > TOL and "value_mismatch" not in flags:
                flags.append("value_mismatch")
        elif latest_period is not None and cust and cust.gstin:
            due = _return_due(inv.invoice_date, quarterly)
            covered_through = _period_end(latest_period)
            if due < today and inv.invoice_date <= covered_through:
                flags.append("not_in_gstr1")
                g1_state = "missing"
        for f in flags:
            counts[f] += 1
        rows.append({"invoice_id": inv.id, "number": inv.number, "doc_type": typ, "invoice_date": inv.invoice_date, "customer_id": inv.customer_id,
                     "customer": cust.name if cust else None, "gstin": cust.gstin if cust else None, "open_minor": inv.open_minor, "total_minor": inv.total_minor,
                     "irn": inv.irn, "on_irp": r is not None, "gstr1": g1_state, "flags": flags,
                     "severity": "high" if any(SEVERITY[f] == "high" for f in flags) else "medium" if flags else "ok", "issues": [TEXT[f] for f in flags]})
    flagged = [r for r in rows if r["flags"]]
    return {"as_of": today, "gstr1_periods": [p.strftime("%m%Y") for p in periods], "irp_documents": len(irp), "gstr1_documents": len(g1),
            "e_invoice_required": require_irn, "gstr1_frequency": "quarterly" if quarterly else "monthly",
            "checked": len(rows), "flagged": len(flagged), "counts": dict(counts), "open_flagged_minor": sum(r["open_minor"] for r in flagged if r["open_minor"] > 0),
            "rows": sorted(flagged, key=lambda r: (r["severity"] != "high", -abs(r["open_minor"]))),
            "note": "Invoices are only judged against what has been imported: import every GSTR-1 period and the IRP register you rely on. Written from the published formats and not yet run on real downloads."}
