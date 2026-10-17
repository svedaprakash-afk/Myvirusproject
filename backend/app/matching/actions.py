"""State transitions on proposals: apply (accept/auto-approve), reject, reverse, manual match. Also learning hooks."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..indian import financial_year, normalise_account, normalise_name
from ..models import (Customer, CustomerIdentifier, CustomerProfile, ExceptionCase, ExceptionStatus, ExceptionType, Invoice,
                      MatchAllocation, MatchFeedback, MatchProposal, PaymentTransaction, ProposalStatus, TdsEntry, TxnStatus, utcnow)


from . import advances, trust


class ActionError(ValueError):
    pass


def apply_proposal(db: Session, ctx, p: MatchProposal, actor: str, actor_id: int | None, auto: bool = False) -> None:
    """Reduce invoice balances, record TDS, raise residual exceptions, learn. Idempotent-guarded by status."""
    if p.status not in (ProposalStatus.pending_review, ProposalStatus.auto_approved):
        raise ActionError(f"Proposal is {p.status.value}; it cannot be applied")
    txn = db.get(PaymentTransaction, p.txn_id)
    # validate first, mutate second — all or nothing
    for a in p.allocations:
        inv = a.invoice
        cleared = a.applied_minor + a.tds_minor + a.writeoff_minor
        after = inv.open_minor - cleared
        if (inv.open_minor > 0 and after < 0) or (inv.open_minor < 0 and after > 0) or (inv.open_minor == 0 and cleared):
            raise ActionError(f"Invoice {inv.number} balance changed since this was proposed (open {inv.open_minor}, clearing {cleared}); re-run matching")
    for a in p.allocations:
        a.invoice.open_minor -= a.applied_minor + a.tds_minor + a.writeoff_minor
        if ctx is not None and a.invoice.open_minor == 0:
            lst = ctx.open_by_cust.get(a.invoice.customer_id, [])
            if a.invoice in lst:
                lst.remove(a.invoice)
    if ctx is not None:
        for a in p.allocations:
            ctx.reserved.get(a.invoice_id, set()).discard(p.txn_id)
    if not auto:
        p.status = ProposalStatus.accepted
        p.decided_by, p.decided_at = actor_id, utcnow()
    txn.status = TxnStatus.partially_matched if (p.unapplied_minor or p.short_minor) else TxnStatus.matched
    txn.matched_at = utcnow()
    txn.customer_id = p.customer_id or txn.customer_id
    fy, q = financial_year(txn.txn_date)
    for a in p.allocations:
        if a.tds_minor:
            db.add(TdsEntry(entity_id=p.entity_id, customer_id=a.invoice.customer_id, invoice_id=a.invoice_id, proposal_id=p.id,
                            txn_date=txn.txn_date, fy=fy, quarter=q, section=a.tds_section, rate_bp=a.tds_rate_bp, amount_minor=a.tds_minor))
    _residual_exceptions(db, p, txn, ctx)
    advances.create_balance(db, p, txn)
    # close open exceptions on this txn that this proposal superseded
    for ex in db.execute(select(ExceptionCase).where(ExceptionCase.txn_id == txn.id, ExceptionCase.status == ExceptionStatus.open,
                                                     ExceptionCase.type.in_([ExceptionType.unidentified_receipt, ExceptionType.on_account]))).scalars():
        ex.status, ex.resolved_at, ex.resolution = ExceptionStatus.resolved, utcnow(), "matched"
    _learn(db, p, txn)
    audit.log(db, actor, "proposal.auto_applied" if auto else "proposal.accepted", "MatchProposal", p.id, p.entity_id,
              {"txn": txn.id, "confidence": p.confidence, "applied_minor": p.applied_minor, "tds_minor": p.tds_minor,
               "writeoff_minor": p.writeoff_minor, "invoices": [a.invoice.number for a in p.allocations]}, actor_id)
    if not auto and p.features:  # manual allocations carry no engine features, so they cannot be a training example
        db.add(MatchFeedback(entity_id=p.entity_id, proposal_id=p.id, label=1, features=p.features, user_id=actor_id))
    if not auto:
        if trust.is_engine_proposal(p):
            trust.record_confirmation(db, p, actor, actor_id)
        elif p.strategy.startswith("manual") and p.strategy != "manual_on_account":
            trust.record_correction(db, p.customer_id, p.entity_id, "analyst overrode the engine with a manual allocation", actor_id)
    db.flush()


def _residual_exceptions(db: Session, p: MatchProposal, txn: PaymentTransaction, ctx=None) -> None:
    from types import SimpleNamespace

    from ..models import DEFAULT_ENTITY_CONFIG, LegalEntity
    from . import engine as eng

    if ctx is None:  # manual accept path: engine.open_exception only needs db, entity id and config
        entity = db.get(LegalEntity, p.entity_id)
        ctx = SimpleNamespace(db=db, entity=entity, cfg={**DEFAULT_ENTITY_CONFIG, **(entity.config or {})})
    cust = db.get(Customer, p.customer_id) if p.customer_id else None
    if p.short_minor > 0:
        inv = max(p.allocations, key=lambda a: a.invoice.open_minor).invoice if p.allocations else None
        eng.open_exception(ctx, txn, ExceptionType.short_pay, p.short_minor,
                           f"Customer paid {eng.inr(p.applied_minor, txn.currency)} against {eng.inr(p.applied_minor + p.short_minor, txn.currency)} due; "
                           f"{eng.inr(p.short_minor, txn.currency)} remains open with no stated reason (not TDS, not within tolerance)",
                           cust, [{"action": "request_remittance", "label": "Ask customer why it was short-paid"},
                                  {"action": "write_off", "label": "Write off (needs approval)"},
                                  {"action": "dispute", "label": "Mark invoice as disputed"},
                                  {"action": "await_balance", "label": "Wait for balance payment"}], p, inv)
    if p.unapplied_minor > 0:
        etype = ExceptionType.overpayment if p.allocations else ExceptionType.on_account
        eng.open_exception(ctx, txn, etype, p.unapplied_minor,
                           f"{eng.inr(p.unapplied_minor, txn.currency)} received beyond the invoices applied",
                           cust, [{"action": "on_account", "label": "Hold on account for future invoices"},
                                  {"action": "refund", "label": "Refund to customer (needs approval)"}], p)


def _learn(db: Session, p: MatchProposal, txn: PaymentTransaction) -> None:
    if not p.customer_id:
        return
    cid = p.customer_id
    from ..models import RemittanceDocument

    doc = db.get(RemittanceDocument, txn.remittance_id) if txn.remittance_id else None
    sender = (doc.sender or "").lower() if doc and doc.auth_result and "dkim=pass" in doc.auth_result else ""   # learn only authenticated senders
    for kind, val in (("name", normalise_name(txn.payer_name)), ("vpa", (txn.payer_vpa or "").lower()), ("account", normalise_account(txn.payer_account)),
                      ("email", sender)):
        val = val[:200]
        if not val:
            continue
        row = db.execute(select(CustomerIdentifier).where(CustomerIdentifier.entity_id == p.entity_id, CustomerIdentifier.customer_id == cid,
                                                          CustomerIdentifier.kind == kind, CustomerIdentifier.value == val)).scalar_one_or_none()
        if row:
            row.hits += 1
            row.last_seen = utcnow()
        else:
            db.add(CustomerIdentifier(entity_id=p.entity_id, customer_id=cid, kind=kind, value=val))
    prof = db.get(CustomerProfile, cid) or CustomerProfile(customer_id=cid, payments=0, multi_invoice_payments=0, tds_payments=0,
                                                           ref_in_narration=0, avg_days_to_pay=0.0, avg_days_late=0.0)
    db.add(prof)
    n = prof.payments or 0
    if p.allocations:
        dtp = sum((txn.txn_date - a.invoice.invoice_date).days for a in p.allocations) / len(p.allocations)
        late = sum((txn.txn_date - a.invoice.due_date).days for a in p.allocations) / len(p.allocations)
        prof.avg_days_to_pay = ((prof.avg_days_to_pay or 0) * n + dtp) / (n + 1)
        prof.avg_days_late = ((prof.avg_days_late or 0) * n + late) / (n + 1)
    prof.payments = n + 1
    prof.multi_invoice_payments = (prof.multi_invoice_payments or 0) + (1 if len(p.allocations) > 1 else 0)
    prof.tds_payments = (prof.tds_payments or 0) + (1 if p.tds_minor else 0)
    prof.ref_in_narration = (prof.ref_in_narration or 0) + (1 if any(a.reference_hit in ("exact", "irn", "remittance") for a in p.allocations) else 0)
    prof.last_paid = txn.txn_date
    # adopt the customer's TDS habit once seen twice at the same rate (never overrides a configured value)
    cust = db.get(Customer, cid)
    rates = {a.tds_rate_bp for a in p.allocations if a.tds_minor and a.tds_rate_bp}
    if cust and not cust.tds_rate_bp and len(rates) == 1 and prof.tds_payments >= 2:
        cust.tds_rate_bp = rates.pop()


def reject_proposal(db: Session, p: MatchProposal, actor: str, actor_id: int | None, reason: str = "") -> None:
    if p.status != ProposalStatus.pending_review:
        raise ActionError(f"Proposal is {p.status.value}; only pending proposals can be rejected")
    p.status = ProposalStatus.rejected
    p.decided_by, p.decided_at = actor_id, utcnow()
    txn = db.get(PaymentTransaction, p.txn_id)
    txn.status = TxnStatus.new
    db.add(MatchFeedback(entity_id=p.entity_id, proposal_id=p.id, label=0, features=p.features, user_id=actor_id))
    trust.record_correction(db, p.customer_id, p.entity_id, "engine proposal rejected by a reviewer", actor_id)
    audit.log(db, actor, "proposal.rejected", "MatchProposal", p.id, p.entity_id, {"txn": txn.id, "reason": reason}, actor_id)


def reverse_proposal(db: Session, p: MatchProposal, actor: str, actor_id: int | None, reason: str) -> None:
    """Undo an applied (not yet exported) proposal: restore balances, void TDS credit, reopen the receipt."""
    if p.status not in (ProposalStatus.accepted, ProposalStatus.auto_approved):
        raise ActionError("Only applied, unposted proposals can be reversed")
    if p.batch_id:
        raise ActionError("Already included in a posting batch; cancel the batch first or post a reversal in the ERP")
    try:
        advances.void_balance_for(db, p)
    except advances.AdvanceError as e:
        raise ActionError(str(e))
    was_auto = p.status == ProposalStatus.auto_approved
    for a in p.allocations:
        a.invoice.open_minor += a.applied_minor + a.tds_minor + a.writeoff_minor
    for t in db.execute(select(TdsEntry).where(TdsEntry.proposal_id == p.id)).scalars():
        t.reversed = True
    for ex in db.execute(select(ExceptionCase).where(ExceptionCase.proposal_id == p.id, ExceptionCase.status.in_([ExceptionStatus.open, ExceptionStatus.in_progress]))).scalars():
        ex.status, ex.resolved_at, ex.resolution = ExceptionStatus.resolved, utcnow(), "match_reversed"
    p.status = ProposalStatus.reversed
    txn = db.get(PaymentTransaction, p.txn_id)
    txn.status = TxnStatus.new
    # a human reversing an applied match is the strongest negative signal we get
    db.add(MatchFeedback(entity_id=p.entity_id, proposal_id=p.id, label=0, features=p.features, user_id=actor_id))
    trust.record_correction(db, p.customer_id, p.entity_id, "applied match was reversed", actor_id, was_auto=was_auto)
    audit.log(db, actor, "proposal.reversed", "MatchProposal", p.id, p.entity_id, {"txn": txn.id, "reason": reason, "was_auto": was_auto}, actor_id)
