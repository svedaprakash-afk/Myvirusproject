"""Canonical journal construction, batching with maker-checker, export and acknowledgement."""
from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, notify
from ..config import settings
from ..models import (Adjustment, BatchStatus, Customer, Invoice, LegalEntity, PaymentTransaction, PeriodLock, PostingBatch, PostingLine,
                      ProposalStatus, MatchProposal, TxnStatus, User, utcnow, DEFAULT_LEDGERS, DEFAULT_ENTITY_CONFIG)
from ..fx import MissingRate, need_rate, to_base
from ..matching.tds import split_components
from ..storage import StorageError, get_store
from . import mapped
from .connectors import REGISTRY


class PostingError(ValueError):
    pass


def _ledger(entity: LegalEntity, key: str) -> str:
    return (entity.ledgers or {}).get(key) or DEFAULT_LEDGERS[key]


def effective_date(db: Session, entity: LegalEntity, d: date) -> tuple[date, bool]:
    """Posting date after applying the period lock. Returns (date, was_redated); the policy 'block' raises instead of re-dating."""
    lock = db.get(PeriodLock, entity.id)
    if not lock or d > lock.locked_through:
        return d, False
    cfg = {**DEFAULT_ENTITY_CONFIG, **(entity.config or {})}
    if cfg.get("period_lock_policy") == "block":
        raise PostingError(f"Books are closed through {lock.locked_through:%d %b %Y}; an item dated {d:%d %b %Y} cannot be posted (policy: block)")
    return lock.locked_through + timedelta(days=1), True


def voucher_for(txn: PaymentTransaction) -> str:
    return f"CA-{txn.entity_id}-{txn.txn_date:%y%m%d}-{txn.id}"


FX_PLUG_MAX_BP = 500   # a realised FX difference above 5% of the receipt means a wrong rate or wrong match: refuse, don't post


def journal_for_proposal(db: Session, entity: LegalEntity, p: MatchProposal) -> list[PostingLine]:
    """Balanced (in base currency) journal for one receipt. Lines carry document-currency AND base amounts so any ERP can
    consume them. Foreign settlements get an explicit realised FX gain/loss line computed from booking vs settlement rates."""
    base = entity.base_currency
    txn = db.get(PaymentTransaction, p.txn_id)
    bank = txn.bank_account
    orig_d = txn.value_date or txn.txn_date
    vno, ccy = voucher_for(txn), txn.currency
    d, redated = effective_date(db, entity, orig_d)
    nar = f"Receipt {txn.utr or txn.bank_ref or txn.id} {txn.payer_name or ''}".strip()
    nar = (nar + f" [receipt dated {orig_d:%d-%b-%Y}, period closed]" if redated else nar)[:290]
    try:
        txn_rate = txn.fx_rate_micro or need_rate(db, ccy, orig_d, base)   # the rate of the day the cash arrived, not the (re-dated) posting day
    except MissingRate as e:
        raise PostingError(str(e))
    L: list[PostingLine] = []

    def add(line_type, account, dr=0, cr=0, *, currency=None, rate=None, inv=None, narration=nar, cust=None):
        if not (dr or cr):
            return
        currency = currency or ccy
        rate = rate or txn_rate
        pl = PostingLine(voucher_no=vno, txn_id=txn.id, proposal_id=p.id, invoice_id=inv.id if inv else None, customer_id=cust or p.customer_id,
                         line_type=line_type, account=account, debit_minor=dr, credit_minor=cr, currency=currency,
                         base_debit_minor=dr if currency == base else to_base(dr, rate), base_credit_minor=cr if currency == base else to_base(cr, rate),
                         rate_micro=None if currency == base else rate, posting_date=d, narration=narration)
        pl._inv = inv.number if inv else None  # type: ignore[attr-defined]
        pl._party = db.get(Customer, pl.customer_id).code if pl.customer_id else None  # type: ignore[attr-defined]
        L.append(pl)

    add("bank_receipt", bank.ledger_code, dr=txn.amount_minor)
    if txn.fee_minor:
        fee_only, gst = (txn.extra or {}).get("fee_only"), (txn.extra or {}).get("tax_on_fee")
        if fee_only is not None and gst is not None and fee_only + gst == txn.fee_minor:
            add("gateway_fee", _ledger(entity, "bank_charges"), dr=fee_only, narration=f"Gateway fee {nar}"[:290])
            add("gst_on_fee", _ledger(entity, "gst_input_on_fees"), dr=gst, narration=f"GST on gateway fee {nar}"[:290])
        else:
            add("gateway_fee", _ledger(entity, "bank_charges"), dr=txn.fee_minor, narration=f"Gateway fee {nar}"[:290])
    ar_default = _ledger(entity, "ar_control")
    cross = bool(p.settle_currency and p.settle_currency != ccy)
    for a in p.allocations:
        inv: Invoice = a.invoice
        cust = db.get(Customer, inv.customer_id)
        icur, irate = inv.currency, inv.booking_rate_micro or 1_000_000
        for kind, section, _rate, amt in (split_components(a.tds_section, a.tds_rate_bp, a.tds_minor) if a.tds_minor else []):
            if kind == "gst":   # GST-TDS is credited to the GST electronic cash ledger, not claimed through Form 16A / 26AS
                add("gst_tds_receivable", _ledger(entity, "gst_tds_receivable"), dr=amt, inv=inv, currency=icur, rate=irate, narration=f"GST-TDS on {inv.number}")
            else:
                add("tds_receivable", _ledger(entity, "tds_receivable"), dr=amt, inv=inv, currency=icur, rate=irate, narration=f"TDS {section or ''} on {inv.number}".strip())
        cleared = a.applied_minor + a.tds_minor + a.writeoff_minor
        add("ar_clearance", cust.ar_ledger or ar_default, cr=cleared if cleared > 0 else 0, dr=-cleared if cleared < 0 else 0, inv=inv, cust=inv.customer_id,
            currency=icur, rate=irate, narration=f"Against {inv.number}")
    w = p.writeoff_minor
    if w > 0:
        acct, lt = (_ledger(entity, "bank_charges"), "bank_charges_foreign") if p.writeoff_kind == "bank_charge" else (_ledger(entity, "write_off"), "writeoff_tolerance")
        add(lt, acct, dr=w, narration=f"{'Intermediary bank charges' if p.writeoff_kind == 'bank_charge' else 'Tolerance write-off'} {nar}"[:290])
    elif w < 0:
        add("rounding_gain", _ledger(entity, "write_off"), cr=-w, narration=f"Rounding gain {nar}"[:290])
    add("unapplied_cash", _ledger(entity, "unapplied_cash"), cr=p.unapplied_minor, narration=f"On-account / overpayment {nar}"[:290])

    # Same-currency, base-currency receipts must balance exactly in document currency too (no FX involved).
    if ccy == base and not cross:
        dr, cr = sum(x.debit_minor for x in L), sum(x.credit_minor for x in L)
        if dr != cr:
            raise PostingError(f"Journal for receipt {txn.id} does not balance (Dr {dr} / Cr {cr}); refusing to post")
    bdr, bcr = sum(x.base_debit_minor for x in L), sum(x.base_credit_minor for x in L)
    plug = bdr - bcr           # >0: debits exceed credits -> we received more base value than AR carried -> gain
    if plug:
        limit = max(txn.amount_minor if ccy == base else to_base(txn.amount_minor, txn_rate), 1) * FX_PLUG_MAX_BP // 10000
        if ccy == base and not cross or abs(plug) > limit:
            raise PostingError(f"Receipt {txn.id}: realised FX difference {plug / 100:,.2f} {base} is implausible (limit {limit / 100:,.2f}); check exchange rates and the match")
        fx = PostingLine(voucher_no=vno, txn_id=txn.id, proposal_id=p.id, customer_id=p.customer_id, line_type="fx_gain" if plug > 0 else "fx_loss",
                         account=_ledger(entity, "fx_gain_loss"), debit_minor=0 if plug > 0 else -plug, credit_minor=plug if plug > 0 else 0, currency=base,
                         base_debit_minor=0 if plug > 0 else -plug, base_credit_minor=plug if plug > 0 else 0, posting_date=d,
                         narration=f"Realised FX {'gain' if plug > 0 else 'loss'} {nar}"[:290])
        fx._inv, fx._party = None, None  # type: ignore[attr-defined]
        L.append(fx)
    assert sum(x.base_debit_minor for x in L) == sum(x.base_credit_minor for x in L)
    return L


def journal_for_adjustment(db: Session, adj: Adjustment) -> list[PostingLine]:
    entity = db.get(LegalEntity, adj.entity_id)
    post_date, redated = effective_date(db, entity, adj.posting_date)
    reason = (adj.reason + f" [dated {adj.posting_date:%d-%b-%Y}, period closed]" if redated else adj.reason)[:290]
    cust = db.get(Customer, adj.customer_id) if adj.customer_id else None
    inv = db.get(Invoice, adj.invoice_id) if adj.invoice_id else None
    vno = f"ADJ-{adj.entity_id}-{adj.posting_date:%y%m%d}-{adj.id}"
    base = entity.base_currency
    rate = 1_000_000
    if adj.currency != base:
        try:
            rate = inv.booking_rate_micro if inv else need_rate(db, adj.currency, adj.posting_date, base)
        except MissingRate as e:
            raise PostingError(str(e))
    out = []
    for t, acct, dr, cr in (("adj_debit", adj.debit_account, adj.amount_minor, 0), ("adj_credit", adj.credit_account, 0, adj.amount_minor)):
        b = adj.amount_minor if adj.currency == base else to_base(adj.amount_minor, rate)
        pl = PostingLine(voucher_no=vno, txn_id=adj.txn_id, adjustment_id=adj.id, invoice_id=adj.invoice_id, customer_id=adj.customer_id, line_type=t,
                         account=acct, debit_minor=dr, credit_minor=cr, currency=adj.currency, base_debit_minor=b if dr else 0, base_credit_minor=b if cr else 0,
                         rate_micro=None if adj.currency == base else rate, posting_date=post_date, narration=reason)
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
    batch.total_minor = sum(x.base_debit_minor for x in lines)
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


def export_batch(db: Session, batch: PostingBatch, connector: str, user: User, transport=None) -> tuple[str, bytes, str]:
    if batch.status not in (BatchStatus.approved, BatchStatus.exported, BatchStatus.failed):
        raise PostingError("Batch must be released before export")
    entity = db.get(LegalEntity, batch.entity_id)
    c = mapped.resolve(entity, connector, transport)
    if not c:
        raise PostingError(f"Unknown or unconfigured connector '{connector}'")
    if c.status != "available":
        raise PostingError(f"Connector '{c.label}' is planned but not implemented yet")
    _attach_refs(db, batch)
    r = c.render(batch, entity)
    checksum = hashlib.sha256(r.content).hexdigest()
    key = f"{batch.entity_id}/{batch.id}/{r.filename}"
    try:                      # store FIRST: a batch is only 'exported' if its file is durably stored and readable back
        store = get_store("exports")
        store.put(key, r.content, r.content_type, checksum)
        store.get(key, verify_sha256=checksum)
    except StorageError as e:
        raise PostingError(f"Could not store the export file, so the batch was NOT marked exported: {e}")
    batch.checksum, batch.export_key = checksum, key
    batch.status, batch.connector, batch.exported_at = BatchStatus.exported, connector, utcnow()
    audit.log(db, user.name, "batch.exported", "PostingBatch", batch.id, batch.entity_id,
              {"connector": connector, "sha256": batch.checksum, "file": r.filename, "key": key, "backend": store.backend}, user.id)
    if hasattr(c, "deliver"):   # push-style connector: deliver now, and acknowledge automatically if the ERP returns document numbers
        try:
            docs = c.deliver(batch, entity)
        except Exception as e:
            fail_batch(db, batch, f"delivery failed: {e}", user)
            raise PostingError(f"Delivery to ERP failed: {e}")
        if docs:
            acknowledge(db, batch, docs, user)
    return r.filename, r.content, r.content_type


def download_stored(db: Session, batch: PostingBatch, user: User) -> tuple[str, bytes]:
    """Re-read an exported file from storage, refusing to serve it unless it still matches the checksum recorded at export time."""
    if not batch.export_key or not batch.checksum:
        raise PostingError("This batch has not been exported (no stored file)")
    try:
        data = get_store("exports").get(batch.export_key, verify_sha256=batch.checksum)
    except StorageError as e:
        raise PostingError(f"Stored export cannot be served: {e}")
    audit.log(db, user.name, "batch.downloaded", "PostingBatch", batch.id, batch.entity_id, {"key": batch.export_key, "sha256": batch.checksum}, user.id)
    return batch.export_key.rsplit("/", 1)[-1], data


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
    notify.emit(db, batch.entity_id, "batch.failed", f"Posting batch #{batch.id} failed", f"Marked failed by {user.name}: {error[:500]}", f"batchfail:{batch.id}:{utcnow():%H%M%S}")


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
    batch.idempotency_key = hashlib.sha256(f"{batch.idempotency_key}|cancelled|{batch.id}".encode()).hexdigest()   # stays 64 chars (PostgreSQL enforces VARCHAR length; SQLite does not)
    audit.log(db, user.name, "batch.cancelled", "PostingBatch", batch.id, batch.entity_id, {}, user.id)
