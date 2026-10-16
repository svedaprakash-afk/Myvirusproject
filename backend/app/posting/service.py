"""Canonical journal construction, batching with maker-checker, export and acknowledgement."""
from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import settings
from ..models import (Adjustment, BatchStatus, Customer, Invoice, LegalEntity, PaymentTransaction, PostingBatch, PostingLine,
                      ProposalStatus, MatchProposal, TxnStatus, User, utcnow, DEFAULT_LEDGERS, DEFAULT_ENTITY_CONFIG)
from .connectors import REGISTRY


class PostingError(ValueError):
    pass


def _ledger(entity: LegalEntity, key: str) -> str:
    return (entity.ledgers or {}).get(key) or DEFAULT_LEDGERS[key]


def voucher_for(txn: PaymentTransaction) -> str:
    return f"CA-{txn.entity_id}-{txn.txn_date:%y%m%d}-{txn.id}"


def journal_for_proposal(db: Session, entity: LegalEntity, p: MatchProposal) -> list[PostingLine]:
    txn = db.get(PaymentTransaction, p.txn_id)
    bank = txn.bank_account
    vno, d, ccy = voucher_for(txn), txn.value_date or txn.txn_date, txn.currency
    nar = f"Receipt {txn.utr or txn.bank_ref or txn.id} {txn.payer_name or ''}".strip()[:290]
    L: list[PostingLine] = []

    def add(line_type, account, dr=0, cr=0, inv=None, narration=nar, cust=None):
        if not (dr or cr):
            return
        pl = PostingLine(voucher_no=vno, txn_id=txn.id, proposal_id=p.id, invoice_id=inv.id if inv else None, customer_id=cust or p.customer_id,
                         line_type=line_type, account=account, debit_minor=dr, credit_minor=cr, currency=ccy, posting_date=d, narration=narration)
        pl._inv = inv.number if inv else None  # type: ignore[attr-defined]
        pl._party = db.get(Customer, pl.customer_id).code if pl.customer_id else None  # type: ignore[attr-defined]
        L.append(pl)

    add("bank_receipt", bank.ledger_code, dr=txn.amount_minor)
    if txn.fee_minor:
        fee_only = (txn.extra or {}).get("fee_only")
        gst = (txn.extra or {}).get("tax_on_fee")
        if fee_only is not None and gst is not None and fee_only + gst == txn.fee_minor:
            add("gateway_fee", _ledger(entity, "bank_charges"), dr=fee_only, narration=f"Gateway fee {nar}"[:290])
            add("gst_on_fee", _ledger(entity, "gst_input_on_fees"), dr=gst, narration=f"GST on gateway fee {nar}"[:290])
        else:
            add("gateway_fee", _ledger(entity, "bank_charges"), dr=txn.fee_minor, narration=f"Gateway fee {nar}"[:290])
    ar_default = _ledger(entity, "ar_control")
    for a in p.allocations:
        inv: Invoice = a.invoice
        cust = db.get(Customer, inv.customer_id)
        acct = cust.ar_ledger or ar_default
        if a.tds_minor:
            add("tds_receivable", _ledger(entity, "tds_receivable"), dr=a.tds_minor, inv=inv, narration=f"TDS {a.tds_section or ''} on {inv.number}".strip())
        cleared = a.applied_minor + a.tds_minor + a.writeoff_minor
        add("ar_clearance", acct, cr=cleared if cleared > 0 else 0, dr=-cleared if cleared < 0 else 0, inv=inv, cust=inv.customer_id,
            narration=f"Against {inv.number}")
    w = p.writeoff_minor
    if w > 0:
        add("writeoff_tolerance", _ledger(entity, "write_off"), dr=w, narration=f"Tolerance write-off {nar}"[:290])
    elif w < 0:
        add("rounding_gain", _ledger(entity, "write_off"), cr=-w, narration=f"Rounding gain {nar}"[:290])
    add("unapplied_cash", _ledger(entity, "unapplied_cash"), cr=p.unapplied_minor, narration=f"On-account / overpayment {nar}"[:290])
    dr, cr = sum(x.debit_minor for x in L), sum(x.credit_minor for x in L)
    if dr != cr:
        raise PostingError(f"Journal for receipt {txn.id} does not balance (Dr {dr} / Cr {cr}); refusing to post")
    return L


def journal_for_adjustment(db: Session, adj: Adjustment) -> list[PostingLine]:
    cust = db.get(Customer, adj.customer_id) if adj.customer_id else None
    inv = db.get(Invoice, adj.invoice_id) if adj.invoice_id else None
    vno = f"ADJ-{adj.entity_id}-{adj.posting_date:%y%m%d}-{adj.id}"
    out = []
    for t, acct, dr, cr in (("adj_debit", adj.debit_account, adj.amount_minor, 0), ("adj_credit", adj.credit_account, 0, adj.amount_minor)):
        pl = PostingLine(voucher_no=vno, txn_id=adj.txn_id, adjustment_id=adj.id, invoice_id=adj.invoice_id, customer_id=adj.customer_id, line_type=t,
                         account=acct, debit_minor=dr, credit_minor=cr, currency=adj.currency, posting_date=adj.posting_date, narration=adj.reason[:290])
        pl._inv, pl._party = (inv.number if inv else None), (cust.code if cust else None)  # type: ignore[attr-defined]
        out.append(pl)
    return out


def build_batch(db: Session, entity: LegalEntity, user: User) -> PostingBatch:
    props = db.execute(select(MatchProposal).where(MatchProposal.entity_id == entity.id, MatchProposal.batch_id.is_(None),
                                                   MatchProposal.status.in_([ProposalStatus.accepted, ProposalStatus.auto_approved]))
                       .order_by(MatchProposal.id)).scalars().all()
    adjs = db.execute(select(Adjustment).where(Adjustment.entity_id == entity.id, Adjustment.batch_id.is_(None), Adjustment.posted.is_(False))
                      .order_by(Adjustment.id)).scalars().all()
    if not props and not adjs:
        raise PostingError("Nothing to post: no applied matches or approved adjustments are waiting")
    key = hashlib.sha256(("P" + ",".join(str(p.id) for p in props) + "|A" + ",".join(str(a.id) for a in adjs)).encode()).hexdigest()
    if db.execute(select(PostingBatch.id).where(PostingBatch.idempotency_key == key)).first():
        raise PostingError("An identical batch was already built")
    batch = PostingBatch(entity_id=entity.id, idempotency_key=key, created_by=user.id if user else None)
    db.add(batch)
    db.flush()
    lines: list[PostingLine] = []
    for p in props:
        lines += journal_for_proposal(db, entity, p)
        p.batch_id = batch.id
    for a in adjs:
        lines += journal_for_adjustment(db, a)
        a.batch_id = batch.id
    for ln in lines:
        ln.batch_id = batch.id
        db.add(ln)
    batch.total_minor = sum(x.debit_minor for x in lines)
    cfg = {**DEFAULT_ENTITY_CONFIG, **(entity.config or {})}
    if cfg.get("straight_through_release"):
        batch.status = BatchStatus.approved
    db.flush()
    batch.lines = lines  # populate relationship for callers
    audit.log(db, user.name if user else "system", "batch.built", "PostingBatch", batch.id, entity.id,
              {"receipts": len(props), "adjustments": len(adjs), "total_minor": batch.total_minor, "auto_release": batch.status.value == "approved"}, user.id if user else None)
    return batch


def release_batch(db: Session, batch: PostingBatch, user: User) -> None:
    if batch.status != BatchStatus.draft:
        raise PostingError(f"Batch is {batch.status.value}")
    if batch.created_by == user.id:
        raise PostingError("Segregation of duties: the person who built a batch cannot release it")
    # also: nobody may release a batch containing matches they personally accepted
    mine = db.execute(select(MatchProposal.id).where(MatchProposal.batch_id == batch.id, MatchProposal.decided_by == user.id)).first()
    if mine:
        raise PostingError("Segregation of duties: you accepted matches in this batch and cannot release it")
    batch.status, batch.approved_by = BatchStatus.approved, user.id
    audit.log(db, user.name, "batch.released", "PostingBatch", batch.id, batch.entity_id, {}, user.id)


def export_batch(db: Session, batch: PostingBatch, connector: str, user: User) -> tuple[str, bytes, str]:
    if batch.status not in (BatchStatus.approved, BatchStatus.exported, BatchStatus.failed):
        raise PostingError("Batch must be released before export")
    c = REGISTRY.get(connector)
    if not c:
        raise PostingError(f"Unknown connector '{connector}'")
    if c.status != "available":
        raise PostingError(f"Connector '{c.label}' is planned but not implemented yet")
    entity = db.get(LegalEntity, batch.entity_id)
    _attach_refs(db, batch)
    r = c.render(batch, entity)
    batch.checksum = hashlib.sha256(r.content).hexdigest()
    batch.status, batch.connector, batch.exported_at = BatchStatus.exported, connector, utcnow()
    os.makedirs(settings.export_dir, exist_ok=True)
    with open(os.path.join(settings.export_dir, r.filename), "wb") as f:
        f.write(r.content)
    audit.log(db, user.name, "batch.exported", "PostingBatch", batch.id, batch.entity_id, {"connector": connector, "sha256": batch.checksum, "file": r.filename}, user.id)
    return r.filename, r.content, r.content_type


def _attach_refs(db: Session, batch: PostingBatch) -> None:
    for ln in batch.lines:
        if not hasattr(ln, "_inv"):
            inv = db.get(Invoice, ln.invoice_id) if ln.invoice_id else None
            cust = db.get(Customer, ln.customer_id) if ln.customer_id else None
            ln._inv, ln._party = (inv.number if inv else None), (cust.code if cust else None)  # type: ignore[attr-defined]


def acknowledge(db: Session, batch: PostingBatch, erp_docs: dict[str, str], user: User) -> None:
    if batch.status != BatchStatus.exported:
        raise PostingError("Only an exported batch can be acknowledged")
    vouchers = {ln.voucher_no for ln in batch.lines}
    missing = vouchers - set(erp_docs)
    if missing:
        raise PostingError(f"ERP acknowledgement is missing {len(missing)} voucher(s), e.g. {sorted(missing)[0]}")
    for ln in batch.lines:
        ln.erp_doc_no = erp_docs[ln.voucher_no]
    for p in db.execute(select(MatchProposal).where(MatchProposal.batch_id == batch.id)).scalars():
        p.status = ProposalStatus.posted
        db.get(PaymentTransaction, p.txn_id).status = TxnStatus.posted
    for a in db.execute(select(Adjustment).where(Adjustment.batch_id == batch.id)).scalars():
        a.posted = True
    batch.status, batch.acked_at = BatchStatus.acknowledged, utcnow()
    audit.log(db, user.name, "batch.acknowledged", "PostingBatch", batch.id, batch.entity_id, {"vouchers": len(vouchers)}, user.id)


def fail_batch(db: Session, batch: PostingBatch, error: str, user: User) -> None:
    batch.status, batch.error = BatchStatus.failed, error[:2000]
    audit.log(db, user.name, "batch.failed", "PostingBatch", batch.id, batch.entity_id, {"error": error[:500]}, user.id)


def cancel_batch(db: Session, batch: PostingBatch, user: User) -> None:
    if batch.status in (BatchStatus.exported, BatchStatus.acknowledged):
        raise PostingError("An exported batch cannot be cancelled; reverse it in the ERP")
    for p in db.execute(select(MatchProposal).where(MatchProposal.batch_id == batch.id)).scalars():
        p.batch_id = None
    for a in db.execute(select(Adjustment).where(Adjustment.batch_id == batch.id)).scalars():
        a.batch_id = None
    for ln in list(batch.lines):
        db.delete(ln)
    batch.status = BatchStatus.failed
    batch.error = "cancelled"
    batch.idempotency_key = batch.idempotency_key + f"-x{batch.id}"
    audit.log(db, user.name, "batch.cancelled", "PostingBatch", batch.id, batch.entity_id, {}, user.id)
