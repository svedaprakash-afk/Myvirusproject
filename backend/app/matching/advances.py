"""On-account (advance / overpayment) balances and their application to later invoices.

Cash that reaches the unapplied-cash ledger without clearing an invoice becomes an `OnAccountBalance` the moment its proposal is
applied. When invoices are later raised, `propose_applications` suggests settling them from the balance (a PO/reference the payer
quoted wins, otherwise oldest due first). A human accepts each application; it posts as Dr unapplied cash / Cr AR (no second bank
line). Disputed invoices and foreign-currency balances are never proposed: the latter need realised-FX treatment and go through
manual matching.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..indian import normalise_ref
from ..models import (DEFAULT_LEDGERS, Adjustment, AdvanceApplication, Customer, ExceptionCase, ExceptionStatus, ExceptionType, Invoice, LegalEntity, MatchProposal, OnAccountBalance,
                      PaymentTransaction, RemittanceDocument, utcnow)


class AdvanceError(ValueError):
    pass


def create_balance(db: Session, p: MatchProposal, txn: PaymentTransaction, reference: str | None = None) -> OnAccountBalance | None:
    if p.unapplied_minor <= 0 or not p.customer_id:
        return None
    existing = db.execute(select(OnAccountBalance).where(OnAccountBalance.proposal_id == p.id)).scalar_one_or_none()
    if existing:
        return existing
    b = OnAccountBalance(entity_id=p.entity_id, customer_id=p.customer_id, txn_id=txn.id, proposal_id=p.id, currency=txn.currency,
                         original_minor=p.unapplied_minor, remaining_minor=p.unapplied_minor, reference=(reference or None) and reference[:60], status="open")
    db.add(b)
    db.flush()
    audit.log(db, "engine", "advance.balance_created", "OnAccountBalance", b.id, p.entity_id,
              {"customer_id": p.customer_id, "amount_minor": b.original_minor, "currency": b.currency, "txn": txn.id}, None)
    return b


def void_balance_for(db: Session, p: MatchProposal) -> None:
    """Called when a proposal is reversed. A balance that has been partly applied blocks the reversal."""
    b = db.execute(select(OnAccountBalance).where(OnAccountBalance.proposal_id == p.id)).scalar_one_or_none()
    if not b:
        return
    if b.remaining_minor != b.original_minor:
        raise AdvanceError("Part of this on-account cash has already been applied to invoices; reject or reverse those applications first")
    b.status, b.remaining_minor = "void", 0
    for a in db.execute(select(AdvanceApplication).where(AdvanceApplication.balance_id == b.id, AdvanceApplication.status == "proposed")).scalars():
        a.status = "rejected"


def mark_refunded(db: Session, proposal_id: int | None, txn_id: int | None) -> None:
    q = select(OnAccountBalance).where(OnAccountBalance.status == "open")
    q = q.where(OnAccountBalance.proposal_id == proposal_id) if proposal_id else q.where(OnAccountBalance.txn_id == txn_id)
    for b in db.execute(q).scalars():
        b.status, b.remaining_minor = "refunded", 0
        for a in db.execute(select(AdvanceApplication).where(AdvanceApplication.balance_id == b.id, AdvanceApplication.status == "proposed")).scalars():
            a.status = "rejected"


def _proposed_total(db: Session, col, key: int) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(AdvanceApplication.amount_minor), 0)).where(col == key, AdvanceApplication.status == "proposed")) or 0)


def propose_applications(db: Session, entity: LegalEntity) -> dict:
    base = entity.base_currency
    made = skipped_foreign = 0
    bals = db.execute(select(OnAccountBalance).where(OnAccountBalance.entity_id == entity.id, OnAccountBalance.status == "open",
                                                     OnAccountBalance.remaining_minor > 0).order_by(OnAccountBalance.id)).scalars().all()
    undecided = {x for (x,) in db.execute(select(ExceptionCase.proposal_id).where(
        ExceptionCase.entity_id == entity.id, ExceptionCase.type == ExceptionType.overpayment, ExceptionCase.proposal_id.is_not(None),
        ExceptionCase.status.in_([ExceptionStatus.open, ExceptionStatus.in_progress, ExceptionStatus.pending_approval])))}
    for b in bals:
        if b.currency != base:
            skipped_foreign += 1
            continue
        if b.proposal_id in undecided:     # someone still has to choose between holding the excess on account and refunding it
            continue
        txn = db.get(PaymentTransaction, b.txn_id)
        remit = db.get(RemittanceDocument, txn.remittance_id) if txn.remittance_id else None
        text = normalise_ref(" ".join(filter(None, [txn.narration, remit.raw_text if remit else None, b.reference])))
        avail = b.remaining_minor - _proposed_total(db, AdvanceApplication.balance_id, b.id)
        if avail <= 0:
            continue
        done = {a.invoice_id for a in db.execute(select(AdvanceApplication).where(AdvanceApplication.balance_id == b.id)).scalars()}
        invs = db.execute(select(Invoice).where(Invoice.entity_id == entity.id, Invoice.customer_id == b.customer_id, Invoice.open_minor > 0,
                                                Invoice.currency == b.currency, Invoice.disputed.is_(False))).scalars().all()

        def by_ref(i: Invoice) -> bool:
            return bool(i.po_ref and len(normalise_ref(i.po_ref)) >= 4 and normalise_ref(i.po_ref) in text)

        for inv in sorted(invs, key=lambda i: (not by_ref(i), i.due_date, i.id)):
            if avail <= 0:
                break
            if inv.id in done:
                continue
            room = inv.open_minor - _proposed_total(db, AdvanceApplication.invoice_id, inv.id)
            if room <= 0:
                continue
            amt = min(room, avail)
            db.add(AdvanceApplication(entity_id=entity.id, balance_id=b.id, invoice_id=inv.id, customer_id=b.customer_id, amount_minor=amt,
                                      basis="reference" if by_ref(inv) else "oldest_first", status="proposed"))
            db.flush()
            avail -= amt
            made += 1
    if made:
        audit.log(db, "engine", "advance.proposed", "LegalEntity", entity.id, entity.id, {"applications": made}, None)
    return {"proposed": made, "skipped_foreign_balances": skipped_foreign}


def accept(db: Session, a: AdvanceApplication, user) -> AdvanceApplication:
    if a.status != "proposed":
        raise AdvanceError(f"Application is already {a.status}")
    b, inv = db.get(OnAccountBalance, a.balance_id), db.get(Invoice, a.invoice_id)
    if b.status != "open" or b.remaining_minor < a.amount_minor:
        raise AdvanceError("The on-account balance no longer covers this application")
    if inv.open_minor < a.amount_minor or inv.open_minor <= 0:
        raise AdvanceError(f"Invoice {inv.number} balance changed since this was proposed (open {inv.open_minor}); re-run the suggestions")
    if inv.disputed:
        raise AdvanceError(f"Invoice {inv.number} is under dispute")
    entity = db.get(LegalEntity, a.entity_id)
    ledgers = {**DEFAULT_LEDGERS, **(entity.ledgers or {})}
    cust = db.get(Customer, a.customer_id)
    inv.open_minor -= a.amount_minor
    b.remaining_minor -= a.amount_minor
    if b.remaining_minor == 0:
        b.status = "used"
    adj = Adjustment(entity_id=a.entity_id, kind="advance_application", txn_id=b.txn_id, invoice_id=inv.id, customer_id=a.customer_id, amount_minor=a.amount_minor,
                     currency=inv.currency, debit_account=ledgers["unapplied_cash"], credit_account=cust.ar_ledger or ledgers["ar_control"],
                     reason=f"Advance applied to {inv.number}"[:300], posting_date=date.today(), created_by=user.id)
    db.add(adj)
    db.flush()
    a.status, a.decided_by, a.decided_at, a.adjustment_id = "applied", user.id, utcnow(), adj.id
    audit.log(db, user.name, "advance.applied", "AdvanceApplication", a.id, a.entity_id,
              {"invoice": inv.number, "amount_minor": a.amount_minor, "balance": b.id, "basis": a.basis}, user.id)
    return a


def reject(db: Session, a: AdvanceApplication, user, reason: str = "") -> AdvanceApplication:
    if a.status != "proposed":
        raise AdvanceError(f"Application is already {a.status}")
    a.status, a.decided_by, a.decided_at = "rejected", user.id, utcnow()
    audit.log(db, user.name, "advance.rejected", "AdvanceApplication", a.id, a.entity_id, {"reason": reason}, user.id)
    return a
