"""Persistence side of ingestion: idempotent statement load, open-item (ERP) import, remittance intake."""
from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..indian import (is_valid_gstin, is_valid_pan, normalise_account, normalise_name, normalise_ref, pan_from_gstin,
                      parse_narration)
from ..models import (BankAccount, Customer, CustomerIdentifier, Invoice, LegalEntity, MatchAllocation, MatchProposal,
                      PaymentTransaction, ProposalStatus, RemittanceDocument, TxnStatus)
from ..money import parse_amount, to_minor
from .remittance import RemittanceExtractor, get_extractor
from .statements import RawTxn, StatementError, detect_and_parse, parse_date, parse_gateway_csv


def _dedupe(bank_account_id: int, t: RawTxn, utr: str | None, nth: int) -> str:
    ident = (utr or t.bank_ref or "").upper() or normalise_ref(t.narration)[:60]
    base = f"{bank_account_id}|{t.txn_date.isoformat()}|{t.direction}|{t.amount_minor}|{ident}|{t.balance_minor}|{nth}"
    return hashlib.sha256(base.encode()).hexdigest()


def ingest_statement(db: Session, bank: BankAccount, filename: str, data: bytes, actor: str, actor_id: int | None,
                     gateway: bool = False) -> dict:
    try:
        fmt, raws = ("gateway_csv", parse_gateway_csv(data)) if gateway else detect_and_parse(filename, data)
    except StatementError:
        raise
    except Exception as e:  # malformed input must never 500
        raise StatementError(f"Could not parse '{filename}': {e}") from e

    batch_id = hashlib.sha256(data).hexdigest()[:12]
    seen: Counter = Counter()
    inserted = dups = credits = debits = 0
    created_ids: list[int] = []
    for t in raws:
        pn = parse_narration(t.narration)
        utr = t.utr_hint or pn.utr
        key_base = (t.txn_date, t.direction, t.amount_minor, (utr or t.bank_ref or normalise_ref(t.narration)[:60]), t.balance_minor)
        seen[key_base] += 1
        key = _dedupe(bank.id, t, utr, seen[key_base])
        if db.execute(select(PaymentTransaction.id).where(PaymentTransaction.dedupe_key == key)).first():
            dups += 1
            continue
        status = TxnStatus.new if t.direction == "credit" else TxnStatus.ignored
        txn = PaymentTransaction(
            entity_id=bank.entity_id, bank_account_id=bank.id, source="gateway" if gateway else "statement", source_batch=batch_id,
            txn_date=t.txn_date, value_date=t.value_date, direction=t.direction, amount_minor=t.amount_minor,
            fee_minor=t.fee_minor, currency=t.currency or bank.currency, channel=pn.channel if not gateway else "GATEWAY",
            utr=utr, narration=t.narration, payer_name=pn.payer_name, payer_ifsc=pn.payer_ifsc, payer_vpa=pn.payer_vpa,
            bank_ref=t.bank_ref, balance_minor=t.balance_minor, dedupe_key=key, status=status,
            extra={"fmt": fmt, "kind": pn.kind, "remark": pn.remark, **(t.extra or {})},
        )
        db.add(txn)
        inserted += 1
        credits += t.direction == "credit"
        debits += t.direction == "debit"
    db.flush()
    audit.log(db, actor, "statement.ingested", "BankAccount", bank.id, bank.entity_id,
              {"file": filename, "format": fmt, "parsed": len(raws), "inserted": inserted, "duplicates": dups, "batch": batch_id}, actor_id)
    return {"format": fmt, "parsed": len(raws), "inserted": inserted, "duplicates_skipped": dups, "credits": credits, "debits": debits,
            "batch": batch_id}


# ---------------------------------------------------------------------------- open items from ERP
_COLS = {
    "invoice_no": ["invoice_no", "invoice number", "invoice no", "doc no", "document no", "bill no", "number", "voucher no"],
    "customer_code": ["customer_code", "customer code", "party code", "customer id", "party id", "debtor code"],
    "customer_name": ["customer_name", "customer name", "party name", "party", "debtor name", "customer"],
    "gstin": ["customer_gstin", "gstin", "customer gstin", "party gstin", "gstin/uin"],
    "pan": ["customer_pan", "pan", "customer pan", "party pan"],
    "invoice_date": ["invoice_date", "invoice date", "doc date", "bill date", "date"],
    "due_date": ["due_date", "due date", "payment due date"],
    "taxable": ["taxable_value", "taxable value", "taxable amount", "base amount", "net amount"],
    "gst": ["gst_amount", "gst amount", "tax amount", "gst", "igst+cgst+sgst"],
    "total": ["total", "invoice amount", "invoice value", "gross amount", "total amount", "amount"],
    "open": ["open_amount", "open amount", "outstanding", "balance", "pending amount", "outstanding amount"],
    "doc_type": ["doc_type", "document type", "type"],
    "irn": ["irn", "e-invoice irn", "einvoice irn"],
    "ack_no": ["ack_no", "ack no", "acknowledgement no"],
    "eway": ["eway_bill", "e-way bill", "eway bill no", "ewb no"],
    "po_ref": ["po_ref", "po number", "po no", "customer po"],
    "currency": ["currency", "ccy"],
    "payment_terms": ["payment_terms_days", "payment terms", "credit days", "terms"],
}


def csv_to_open_items(data: bytes) -> list[dict]:
    rd = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    if not rd.fieldnames:
        raise ValueError("Empty file")
    low = {h.strip().lower(): h for h in rd.fieldnames}
    cmap = {k: next((low[s] for s in syns if s in low), None) for k, syns in _COLS.items()}
    if not cmap["invoice_no"] or not (cmap["customer_code"] or cmap["customer_name"]) or not cmap["total"]:
        raise ValueError("Need columns for invoice number, customer (code or name) and total amount")
    rows = []
    for r in rd:
        g = lambda k: (r.get(cmap[k]) or "").strip() if cmap[k] else ""
        rows.append({k: g(k) for k in cmap})
    return rows


def import_open_items(db: Session, entity: LegalEntity, rows: list[dict], actor: str, actor_id: int | None,
                      full_snapshot: bool = False) -> dict:
    """Upsert customers and invoices from an ERP AR ageing extract. Idempotent.

    The ERP is the source of truth for balances, EXCEPT cash we have already applied but not yet posted
    back: re-importing must not resurrect those balances, so open = erp_open - pending_applied.
    """
    stats = Counter()
    warnings: list[str] = []
    pending = _pending_by_invoice(db, entity.id)
    seen_numbers: set[str] = set()
    for i, r in enumerate(rows, start=1):
        try:
            number = (r.get("invoice_no") or "").strip()
            name = (r.get("customer_name") or "").strip()
            code = (r.get("customer_code") or "").strip() or normalise_ref(name)[:20]
            if not number or not code:
                raise ValueError("missing invoice number or customer")
            gstin = (r.get("gstin") or "").strip().upper() or None
            pan = ((r.get("pan") or "").strip().upper() or pan_from_gstin(gstin))
            if gstin and not is_valid_gstin(gstin):
                warnings.append(f"row {i}: GSTIN {gstin} failed checksum for {name or code}")
            if pan and not is_valid_pan(pan):
                warnings.append(f"row {i}: PAN {pan} invalid for {name or code}")
                pan = None
            cust = db.execute(select(Customer).where(Customer.entity_id == entity.id, Customer.code == code)).scalar_one_or_none()
            if not cust:
                cust = Customer(entity_id=entity.id, code=code, name=name or code, gstin=gstin, pan=pan,
                                payment_terms_days=int(r.get("payment_terms") or 30))
                db.add(cust)
                db.flush()
                stats["customers_created"] += 1
                _learn(db, entity.id, cust.id, "name", normalise_name(cust.name), "master")
            else:
                if gstin and not cust.gstin:
                    cust.gstin = gstin
                if pan and not cust.pan:
                    cust.pan = pan
            d_inv = parse_date(r.get("invoice_date")) or date.today()
            d_due = parse_date(r.get("due_date")) or d_inv + timedelta(days=cust.payment_terms_days)
            total = parse_amount(r.get("total"))
            if total is None:
                raise ValueError("invalid total")
            doc_type = (r.get("doc_type") or "invoice").lower().replace(" ", "_")
            if doc_type.startswith("credit") or doc_type == "cn":
                doc_type, total = "credit_note", -abs(total)
            elif doc_type.startswith("debit"):
                doc_type = "debit_note"
            else:
                doc_type = "invoice"
            gst = parse_amount(r.get("gst")) or 0
            taxable = parse_amount(r.get("taxable"))
            if taxable is None:
                taxable = total - gst
            if doc_type == "credit_note":
                gst, taxable = -abs(gst), -abs(taxable)
            erp_open = parse_amount(r.get("open"))
            erp_open = total if erp_open is None else (-abs(erp_open) if doc_type == "credit_note" else erp_open)
            inv = db.execute(select(Invoice).where(Invoice.entity_id == entity.id, Invoice.number == number)).scalar_one_or_none()
            if inv:
                inv.open_minor = erp_open - pending.get(inv.id, 0)
                inv.total_minor, inv.taxable_minor, inv.gst_minor = total, taxable, gst
                stats["invoices_updated"] += 1
            else:
                inv = Invoice(entity_id=entity.id, customer_id=cust.id, number=number, doc_type=doc_type, invoice_date=d_inv,
                              due_date=d_due, currency=(r.get("currency") or "INR").upper(), taxable_minor=taxable, gst_minor=gst,
                              total_minor=total, open_minor=erp_open, irn=(r.get("irn") or None), ack_no=(r.get("ack_no") or None),
                              eway_bill_no=(r.get("eway") or None), po_ref=(r.get("po_ref") or None))
                db.add(inv)
                stats["invoices_created"] += 1
            seen_numbers.add(number)
        except Exception as e:
            stats["rejected"] += 1
            warnings.append(f"row {i}: {e}")
    if full_snapshot:
        for inv in db.execute(select(Invoice).where(Invoice.entity_id == entity.id, Invoice.open_minor != 0)).scalars():
            if inv.number not in seen_numbers and pending.get(inv.id, 0) == 0:
                inv.open_minor = 0  # absent from a full ERP snapshot => settled in ERP
                stats["closed_by_snapshot"] += 1
    db.flush()
    audit.log(db, actor, "open_items.imported", "LegalEntity", entity.id, entity.id, dict(stats), actor_id)
    return {**stats, "warnings": warnings[:50]}


def _pending_by_invoice(db: Session, entity_id: int) -> dict[int, int]:
    q = (select(MatchAllocation.invoice_id, func.sum(MatchAllocation.applied_minor + MatchAllocation.tds_minor + MatchAllocation.writeoff_minor))
         .join(MatchProposal).where(MatchProposal.entity_id == entity_id,
                                    MatchProposal.status.in_([ProposalStatus.accepted, ProposalStatus.auto_approved]))
         .group_by(MatchAllocation.invoice_id))
    return {i: int(s) for i, s in db.execute(q).all()}


def _learn(db: Session, entity_id: int, customer_id: int, kind: str, value: str, source: str) -> None:
    if not value:
        return
    row = db.execute(select(CustomerIdentifier).where(
        CustomerIdentifier.entity_id == entity_id, CustomerIdentifier.customer_id == customer_id,
        CustomerIdentifier.kind == kind, CustomerIdentifier.value == value)).scalar_one_or_none()
    if row:
        row.hits += 1
    else:
        db.add(CustomerIdentifier(entity_id=entity_id, customer_id=customer_id, kind=kind, value=value, source=source))


# ---------------------------------------------------------------------------- remittance intake
def ingest_remittance(db: Session, entity_id: int, text: str, filename: str | None, source: str, actor: str,
                      actor_id: int | None, extractor: RemittanceExtractor | None = None) -> RemittanceDocument:
    ex = extractor or get_extractor()
    data = ex.extract(text)
    doc = RemittanceDocument(entity_id=entity_id, source=source, filename=filename, raw_text=text, extracted=data, extractor=ex.name)
    db.add(doc)
    db.flush()
    link_remittance(db, doc)
    audit.log(db, actor, "remittance.ingested", "RemittanceDocument", doc.id, entity_id,
              {"extractor": ex.name, "lines": len(data.get("lines", [])), "linked_txn": doc.linked_txn_id}, actor_id)
    return doc


def link_remittance(db: Session, doc: RemittanceDocument) -> PaymentTransaction | None:
    """Attach an advice to its bank credit: by UTR first, else by total amount within +/-7 days (unique only)."""
    ex = doc.extracted or {}
    q = select(PaymentTransaction).where(PaymentTransaction.entity_id == doc.entity_id, PaymentTransaction.direction == "credit",
                                         PaymentTransaction.remittance_id.is_(None),
                                         PaymentTransaction.status.in_([TxnStatus.new, TxnStatus.review, TxnStatus.exception]))
    utr = (ex.get("utr") or "").upper()
    cands = []
    if utr:
        cands = [t for t in db.execute(q).scalars() if t.utr and (t.utr.upper() == utr or utr in (t.narration or "").upper())]
    if not cands and ex.get("total_minor"):
        cands = list(db.execute(q.where(PaymentTransaction.amount_minor + PaymentTransaction.fee_minor == ex["total_minor"],
                                        PaymentTransaction.txn_date >= date.today() - timedelta(days=3650))).scalars())
        if ex.get("payer_name"):
            n = normalise_name(ex["payer_name"])
            narrowed = [t for t in cands if n and n.split()[0] in normalise_name(t.payer_name or t.narration)]
            cands = narrowed or cands
    if len(cands) == 1:
        t = cands[0]
        t.remittance_id, doc.linked_txn_id = doc.id, t.id
        return t
    return None
