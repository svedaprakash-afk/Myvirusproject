"""Maker-checker approvals, exception resolution, manual matching. All state changes are audited."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .indian import normalise_name
from .matching import actions, advances, engine
from .matching.context import build_ctx
from .models import (Adjustment, ApprovalKind, ApprovalPolicy, ApprovalRequest, ApprovalStatus, Customer, CustomerIdentifier,
                     ExceptionCase, ExceptionNote, ExceptionStatus, ExceptionType, Invoice, LegalEntity, MatchAllocation, MatchFeedback, MatchProposal, OnAccountBalance,
                     PaymentTransaction, ProposalStatus, Role, TxnStatus, User, utcnow, DEFAULT_LEDGERS)
from .security import RANK, P


class WorkflowError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------- approvals
DEFAULT_LIMITS = [(0, 25_000_00, Role.ar_manager), (25_000_00, None, Role.finance_head)]


def required_role(db: Session, entity_id: int, kind: ApprovalKind, amount: int) -> Role:
    rows = db.execute(select(ApprovalPolicy).where(ApprovalPolicy.entity_id == entity_id, ApprovalPolicy.kind == kind)
                      .order_by(ApprovalPolicy.min_minor)).scalars().all()
    amt = abs(amount)
    for r in rows:
        if r.min_minor <= amt and (r.max_minor is None or amt < r.max_minor):
            return r.approver_role
    for lo, hi, role in DEFAULT_LIMITS:
        if lo <= amt and (hi is None or amt < hi):
            return role
    return Role.finance_head


def request_approval(db: Session, user: User, entity_id: int, kind: ApprovalKind, amount: int, reason: str, payload: dict,
                     ref_type: str | None = None, ref_id: int | None = None) -> ApprovalRequest:
    if not reason or len(reason.strip()) < 5:
        raise WorkflowError("A reason of at least 5 characters is required")
    req = ApprovalRequest(entity_id=entity_id, kind=kind, amount_minor=amount, required_role=required_role(db, entity_id, kind, amount),
                          requested_by=user.id, reason=reason.strip(), payload=payload, ref_type=ref_type, ref_id=ref_id)
    db.add(req)
    db.flush()
    audit.log(db, user.name, "approval.requested", "ApprovalRequest", req.id, entity_id,
              {"kind": kind.value, "amount_minor": amount, "required_role": req.required_role.value, "reason": reason}, user.id)
    return req


def decide(db: Session, user: User, req: ApprovalRequest, approve: bool, note: str = "") -> ApprovalRequest:
    if req.status != ApprovalStatus.pending:
        raise WorkflowError(f"Request is already {req.status.value}")
    if req.requested_by == user.id:
        raise WorkflowError("Segregation of duties: you cannot decide your own request")
    if user.role not in P["approve"] or RANK[user.role] < RANK[req.required_role]:
        raise WorkflowError(f"This request needs a {req.required_role.value} or higher (you are {user.role.value})")
    req.status = ApprovalStatus.approved if approve else ApprovalStatus.rejected
    req.decided_by, req.decided_at, req.decision_note = user.id, utcnow(), note
    audit.log(db, user.name, "approval.approved" if approve else "approval.rejected", "ApprovalRequest", req.id, req.entity_id,
              {"kind": req.kind.value, "amount_minor": req.amount_minor, "note": note}, user.id)
    if approve:
        _execute(db, user, req)
    else:
        _on_reject(db, req)
    return req


def _execute(db: Session, user: User, req: ApprovalRequest) -> None:
    pl = req.payload
    entity = db.get(LegalEntity, req.entity_id)
    if req.kind == ApprovalKind.write_off:
        inv = db.get(Invoice, pl["invoice_id"])
        amt = pl["amount_minor"]
        if amt <= 0 or amt > inv.open_minor:
            raise WorkflowError(f"Invoice {inv.number} open balance is {inv.open_minor}; cannot write off {amt}")
        inv.open_minor -= amt
        ledgers = {**DEFAULT_LEDGERS, **(entity.ledgers or {})}
        cust = db.get(Customer, inv.customer_id)
        adj = Adjustment(entity_id=entity.id, kind="write_off", invoice_id=inv.id, customer_id=inv.customer_id, exception_id=pl.get("exception_id"),
                         approval_id=req.id, amount_minor=amt, currency=inv.currency, debit_account=ledgers["write_off"],
                         credit_account=cust.ar_ledger or ledgers["ar_control"], reason=f"Write-off {inv.number}: {req.reason}"[:300],
                         posting_date=date.today(), created_by=req.requested_by)
        db.add(adj)
        _close_exception(db, pl.get("exception_id"), ExceptionStatus.written_off, "written_off", req.reason)
    elif req.kind == ApprovalKind.refund:
        ex_r = db.get(ExceptionCase, pl.get("exception_id")) if pl.get("exception_id") else None
        if ex_r:
            advances.mark_refunded(db, ex_r.proposal_id, ex_r.txn_id)
        _close_exception(db, pl.get("exception_id"), ExceptionStatus.resolved, "refund_approved", req.reason)
    elif req.kind == ApprovalKind.manual_match:
        p = db.get(MatchProposal, pl["proposal_id"])
        actions.apply_proposal(db, None, p, actor=user.name, actor_id=req.requested_by)
    elif req.kind == ApprovalKind.adjustment:
        txn = db.get(PaymentTransaction, pl["txn_id"])
        ledgers = {**DEFAULT_LEDGERS, **(entity.ledgers or {})}
        credit = {"non_ar_receipt": pl.get("credit_account") or "4900-OTHER-INCOME", "interco_receipt": ledgers["intercompany"]}[pl["adj_kind"]]
        db.add(Adjustment(entity_id=entity.id, kind=pl["adj_kind"], txn_id=txn.id, exception_id=pl.get("exception_id"), approval_id=req.id,
                          amount_minor=txn.receivable_minor, currency=txn.currency, debit_account=txn.bank_account.ledger_code, credit_account=credit,
                          reason=req.reason[:300], posting_date=txn.value_date or txn.txn_date, created_by=req.requested_by))
        txn.status = TxnStatus.matched
        _close_exception(db, pl.get("exception_id"), ExceptionStatus.resolved, pl["adj_kind"], req.reason)


def _on_reject(db: Session, req: ApprovalRequest) -> None:
    ex_id = req.payload.get("exception_id")
    if ex_id:
        ex = db.get(ExceptionCase, ex_id)
        if ex and ex.status == ExceptionStatus.pending_approval:
            ex.status = ExceptionStatus.in_progress
            db.add(ExceptionNote(case_id=ex.id, user_id=None, text=f"Approval rejected: {req.decision_note or 'no reason given'}"))
    if req.kind == ApprovalKind.manual_match:
        p = db.get(MatchProposal, req.payload["proposal_id"])
        if p and p.status == ProposalStatus.pending_review:
            p.status = ProposalStatus.rejected
            db.get(PaymentTransaction, p.txn_id).status = TxnStatus.new


def _close_exception(db: Session, ex_id: int | None, status: ExceptionStatus, resolution: str, note: str) -> None:
    if not ex_id:
        return
    ex = db.get(ExceptionCase, ex_id)
    if ex:
        ex.status, ex.resolution, ex.resolved_at = status, resolution, utcnow()
        db.add(ExceptionNote(case_id=ex.id, user_id=None, text=f"Resolved: {resolution}. {note}"))


# ---------------------------------------------------------------------------------------------- exceptions
def resolve_exception(db: Session, user: User, ex: ExceptionCase, action: str, params: dict) -> dict:
    if ex.status in (ExceptionStatus.resolved, ExceptionStatus.written_off):
        raise WorkflowError("Exception is already closed")
    if ex.status == ExceptionStatus.pending_approval:
        raise WorkflowError("Exception is waiting for an approval decision")
    entity = db.get(LegalEntity, ex.entity_id)
    txn = db.get(PaymentTransaction, ex.txn_id) if ex.txn_id else None
    audit.log(db, user.name, f"exception.{action}", "ExceptionCase", ex.id, ex.entity_id, {k: v for k, v in params.items() if k != "note"}, user.id)

    if action == "link_customer":
        cust = db.get(Customer, params["customer_id"])
        if not cust or cust.entity_id != ex.entity_id or not txn:
            raise WorkflowError("Choose a customer of this entity")
        txn.customer_id, txn.customer_confidence = cust.id, 1.0
        n = normalise_name(txn.payer_name)[:200]
        if n and not db.execute(select(CustomerIdentifier).where(CustomerIdentifier.entity_id == ex.entity_id, CustomerIdentifier.customer_id == cust.id,
                                                                 CustomerIdentifier.kind == "name", CustomerIdentifier.value == n)).first():
            db.add(CustomerIdentifier(entity_id=ex.entity_id, customer_id=cust.id, kind="name", value=n, source="analyst"))
        if txn.payer_vpa:
            db.add(CustomerIdentifier(entity_id=ex.entity_id, customer_id=cust.id, kind="vpa", value=txn.payer_vpa.lower(), source="analyst"))
        db.flush()
        txn.status = TxnStatus.new
        res = engine.run_matching(db, entity, [txn.id], user.name, user.id)
        return {"rematched": res, "note": "Payer linked; learned for next time and matching re-run"}

    if action == "on_account":
        cust = db.get(Customer, params.get("customer_id") or ex.customer_id or (txn.customer_id if txn else None))
        if not cust or not txn:
            raise WorkflowError("Link a customer first")
        prior = db.get(MatchProposal, ex.proposal_id) if ex.proposal_id else None
        if prior and prior.status in (ProposalStatus.accepted, ProposalStatus.auto_approved, ProposalStatus.posted) and prior.unapplied_minor > 0:
            # an overpayment: the excess is already in the unapplied-cash ledger through the applied proposal. Creating another
            # proposal here would post the whole bank receipt a second time.
            _close(ex, "on_account", user, params)
            return {"proposal_id": prior.id, "note": "Excess is held on account and is available for future invoices"}
        p = _manual_proposal(db, entity, txn, cust, [], user, "manual_on_account", "Held on account (unapplied advance) pending invoice")
        p.unapplied_minor = txn.receivable_minor
        actions.apply_proposal(db, None, p, user.name, user.id)
        if params.get("reference"):
            bal = db.execute(select(OnAccountBalance).where(OnAccountBalance.proposal_id == p.id)).scalar_one_or_none()
            if bal:
                bal.reference = str(params["reference"])[:60]
        _close(ex, "on_account", user, params)
        return {"proposal_id": p.id}

    if action == "apply_fifo":
        cust = db.get(Customer, ex.customer_id or (txn.customer_id if txn else None))
        if not cust or not txn:
            raise WorkflowError("Link a customer first")
        ctx = build_ctx(db, entity)
        cand = engine.fifo_candidate(ctx, txn, cust, None)
        if not cand:
            raise WorkflowError("Customer has no open invoices")
        engine.finish(ctx, cand)
        p = _proposal_from_cand(db, ctx, txn, cand, user, "manual_fifo")
        actions.apply_proposal(db, ctx, p, user.name, user.id)
        _close(ex, "applied_fifo", user, params)
        return {"proposal_id": p.id}

    if action == "mark_duplicate":
        if not txn:
            raise WorkflowError("No receipt on this exception")
        txn.status = TxnStatus.ignored
        _close(ex, "duplicate_statement_line", user, params)
        return {}

    if action in ("write_off", "refund", "classify_non_ar", "classify_interco"):
        reason = (params.get("note") or "").strip()
        if action == "write_off":
            inv = db.get(Invoice, params.get("invoice_id") or ex.invoice_id)
            amt = int(params.get("amount_minor") or ex.amount_minor)
            if not inv:
                raise WorkflowError("Choose the invoice to write off")
            if amt > inv.open_minor:
                raise WorkflowError(f"Only {inv.open_minor} is open on {inv.number}")
            req = request_approval(db, user, ex.entity_id, ApprovalKind.write_off, amt, reason, {"invoice_id": inv.id, "amount_minor": amt, "exception_id": ex.id}, "ExceptionCase", ex.id)
        elif action == "refund":
            req = request_approval(db, user, ex.entity_id, ApprovalKind.refund, ex.amount_minor, reason, {"exception_id": ex.id}, "ExceptionCase", ex.id)
        else:
            kind = "non_ar_receipt" if action == "classify_non_ar" else "interco_receipt"
            req = request_approval(db, user, ex.entity_id, ApprovalKind.adjustment, txn.receivable_minor if txn else ex.amount_minor, reason,
                                   {"adj_kind": kind, "txn_id": ex.txn_id, "exception_id": ex.id, "credit_account": params.get("credit_account")}, "ExceptionCase", ex.id)
        ex.status = ExceptionStatus.pending_approval
        return {"approval_id": req.id, "required_role": req.required_role.value}

    if action == "dispute":
        inv = db.get(Invoice, params.get("invoice_id") or ex.invoice_id)
        if not inv:
            raise WorkflowError("Choose the invoice to dispute")
        inv.disputed = True
        _close(ex, "invoice_disputed", user, params)
        return {}

    if action in ("await_balance", "request_remittance"):
        ex.status = ExceptionStatus.in_progress
        db.add(ExceptionNote(case_id=ex.id, user_id=user.id, text=params.get("note") or action.replace("_", " ")))
        return {}
    raise WorkflowError(f"Unknown action '{action}'")


def _close(ex: ExceptionCase, resolution: str, user: User, params: dict) -> None:
    ex.status, ex.resolution, ex.resolved_at = ExceptionStatus.resolved, resolution, utcnow()
    ex.notes.append(ExceptionNote(user_id=user.id, text=f"Resolved: {resolution}. {params.get('note') or ''}".strip()))


# ---------------------------------------------------------------------------------------------- manual matching
def _manual_proposal(db: Session, entity: LegalEntity, txn: PaymentTransaction, cust: Customer | None, allocs: list[dict], user: User,
                     strategy: str, summary: str) -> MatchProposal:
    p = MatchProposal(entity_id=entity.id, txn_id=txn.id, customer_id=cust.id if cust else None, status=ProposalStatus.pending_review, strategy=strategy,
                      confidence=1.0, model_version="manual", explanation={"summary": summary, "evidence": [f"Decided by {user.name}"], "warnings": [], "contributions": []},
                      features={}, guardrails=[], fee_minor=txn.fee_minor, created_by=f"user:{user.id}")
    db.add(p)
    db.flush()
    return p


def _proposal_from_cand(db, ctx, txn, c, user, strategy) -> MatchProposal:
    p = _manual_proposal(db, ctx.entity, txn, ctx.customers.get(c.customer_id), [], user, strategy, engine._summary(txn, c))
    p.features, p.applied_minor, p.tds_minor, p.writeoff_minor = {k: round(v, 4) for k, v in c.feats.items()}, c.applied, c.tds, c.writeoff
    p.unapplied_minor, p.short_minor = c.unapplied, c.short
    for a in c.allocs:
        p.allocations.append(MatchAllocation(invoice_id=a.invoice.id, applied_minor=a.applied, tds_minor=a.tds, writeoff_minor=a.writeoff,
                                             tds_section=a.tds_section, tds_rate_bp=a.tds_bp, reference_hit=a.hit))
    db.flush()
    return p


def manual_match(db: Session, user: User, txn: PaymentTransaction, allocations: list[dict], reason: str) -> dict:
    """Analyst-built allocation. allocations: [{invoice_id, applied_minor, tds_minor?, writeoff_minor?}].
    Cash must reconcile exactly: sum(applied) + unapplied == receipt. Unusual items go through maker-checker."""
    if txn.status in (TxnStatus.matched, TxnStatus.posted, TxnStatus.ignored):
        raise WorkflowError(f"Receipt is {txn.status.value}")
    entity = db.get(LegalEntity, txn.entity_id)
    if not allocations:
        raise WorkflowError("Add at least one invoice")
    pay = txn.receivable_minor
    applied = sum(a["applied_minor"] for a in allocations)
    if applied > pay:
        raise WorkflowError(f"Allocations ({applied}) exceed the receipt ({pay})")
    cust_ids = set()
    for a in allocations:
        inv = db.get(Invoice, a["invoice_id"])
        if not inv or inv.entity_id != entity.id:
            raise WorkflowError("Unknown invoice")
        if inv.currency != txn.currency:
            raise WorkflowError(f"{inv.number} is in {inv.currency} but the receipt is in {txn.currency}. Manual cross-currency allocation is not supported; "
                                f"let the engine propose it (it needs the exchange rate) or add the rate under FX rates")
        cust_ids.add(inv.customer_id)
        cleared = a["applied_minor"] + a.get("tds_minor", 0) + a.get("writeoff_minor", 0)
        if (inv.open_minor > 0 and not 0 < cleared <= inv.open_minor) or (inv.open_minor < 0 and not inv.open_minor <= cleared < 0):
            raise WorkflowError(f"{inv.number}: clearing {cleared} is outside its open balance {inv.open_minor}")
        if inv.disputed and not reason:
            raise WorkflowError(f"{inv.number} is disputed; explain why you are applying cash to it")
    cust = db.get(Customer, next(iter(cust_ids))) if len(cust_ids) == 1 else None
    for old in db.execute(select(MatchProposal).where(MatchProposal.txn_id == txn.id, MatchProposal.status == ProposalStatus.pending_review)).scalars():
        old.status = ProposalStatus.superseded   # the engine's suggestion was overridden: a negative learning signal
        old.decided_by, old.decided_at = user.id, utcnow()
        if old.features:
            db.add(MatchFeedback(entity_id=old.entity_id, proposal_id=old.id, label=0, features=old.features, user_id=user.id))
    p = _manual_proposal(db, entity, txn, cust, allocations, user, "manual", f"Manual allocation by {user.name}: {reason}"[:300])
    for a in allocations:
        p.allocations.append(MatchAllocation(invoice_id=a["invoice_id"], applied_minor=a["applied_minor"], tds_minor=a.get("tds_minor", 0),
                                             writeoff_minor=a.get("writeoff_minor", 0), tds_section=a.get("tds_section"), tds_rate_bp=a.get("tds_rate_bp"),
                                             reference_hit="manual"))
    p.applied_minor, p.tds_minor = applied, sum(a.get("tds_minor", 0) for a in allocations)
    p.writeoff_minor = sum(a.get("writeoff_minor", 0) for a in allocations)
    p.unapplied_minor = pay - applied
    db.flush()
    ctx_cfg = {**__import__("app.models", fromlist=["DEFAULT_ENTITY_CONFIG"]).DEFAULT_ENTITY_CONFIG, **(entity.config or {})}
    tol = 500
    needs = abs(p.writeoff_minor) > tol or pay >= ctx_cfg.get("manual_match_approval_minor", 10_00_00_000) or len(cust_ids) > 1
    if needs:
        txn.status = TxnStatus.review
        why = "write-off beyond tolerance" if abs(p.writeoff_minor) > tol else ("multiple customers" if len(cust_ids) > 1 else "high-value receipt")
        req = request_approval(db, user, entity.id, ApprovalKind.manual_match, pay, reason or why, {"proposal_id": p.id}, "MatchProposal", p.id)
        p.guardrails = [f"Awaiting approval ({why})"]
        return {"proposal_id": p.id, "status": "awaiting_approval", "approval_id": req.id, "required_role": req.required_role.value}
    actions.apply_proposal(db, None, p, user.name, user.id)
    audit.log(db, user.name, "match.manual", "MatchProposal", p.id, entity.id, {"txn": txn.id, "reason": reason}, user.id)
    return {"proposal_id": p.id, "status": "applied"}
