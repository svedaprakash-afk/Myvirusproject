"""Operational workflow: receipts, matching, exceptions, approvals, TDS."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import audit, workflow
from ..db import get_db
from ..matching import actions, engine
from ..models import (ApprovalRequest, ApprovalStatus, ExceptionCase, ExceptionNote, ExceptionStatus, ExceptionType, MatchProposal,
                      PaymentTransaction, ProposalStatus, RemittanceDocument, TdsEntry, TxnStatus, User)
from ..security import can_see_pii, require
from . import serializers as S
from .deps import entity_for, get_or_404, guard, page

router = APIRouter()


def _wf(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except (workflow.WorkflowError, actions.ActionError) as e:
        raise HTTPException(409, str(e))


# ---------------------------------------------------------------------------------------------- receipts
@router.get("/transactions")
def transactions(entity_id: int, status: str = "", q: str = "", direction: str = "credit", date_from: date | None = None, date_to: date | None = None,
                 limit: int = 100, offset: int = 0, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    limit, offset = page(limit, offset)
    base = select(PaymentTransaction).where(PaymentTransaction.entity_id == entity_id)
    if direction:
        base = base.where(PaymentTransaction.direction == direction)
    if status:
        try:
            base = base.where(PaymentTransaction.status.in_([TxnStatus(s) for s in status.split(",")]))
        except ValueError:
            raise HTTPException(400, "Unknown status")
    if q:
        base = base.where(or_(PaymentTransaction.narration.ilike(f"%{q}%"), PaymentTransaction.utr.ilike(f"%{q}%"), PaymentTransaction.payer_name.ilike(f"%{q}%")))
    if date_from:
        base = base.where(PaymentTransaction.txn_date >= date_from)
    if date_to:
        base = base.where(PaymentTransaction.txn_date <= date_to)
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    rows = db.execute(base.order_by(PaymentTransaction.txn_date.desc(), PaymentTransaction.id.desc()).limit(limit).offset(offset)).scalars().all()
    counts = dict(db.execute(select(PaymentTransaction.status, func.count()).where(PaymentTransaction.entity_id == entity_id, PaymentTransaction.direction == "credit")
                             .group_by(PaymentTransaction.status)).all())
    return {"total": total, "items": [S.txn(t) for t in rows], "status_counts": {k.value: v for k, v in counts.items()}}


@router.get("/transactions/{tid}")
def transaction_detail(tid: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    t = get_or_404(db, PaymentTransaction, tid)
    guard(user, t)
    props = db.execute(select(MatchProposal).where(MatchProposal.txn_id == t.id).order_by(MatchProposal.id.desc())).scalars().all()
    excs = db.execute(select(ExceptionCase).where(ExceptionCase.txn_id == t.id).order_by(ExceptionCase.id.desc())).scalars().all()
    rem = db.get(RemittanceDocument, t.remittance_id) if t.remittance_id else None
    return {**S.txn(t), "extra": t.extra, "proposals": [S.proposal(p) for p in props], "exceptions": [S.exception(x) for x in excs],
            "remittance": None if not rem else {"id": rem.id, "source": rem.source, "extractor": rem.extractor, "text": rem.raw_text[:6000], "extracted": rem.extracted}}


class AllocIn(BaseModel):
    invoice_id: int
    applied_minor: int = Field(..., gt=-10**13)
    tds_minor: int = 0
    writeoff_minor: int = 0
    tds_section: str | None = None
    tds_rate_bp: int | None = None


class ManualIn(BaseModel):
    allocations: list[AllocIn]
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("/transactions/{tid}/manual-match")
def manual_match(tid: int, body: ManualIn, user: User = Depends(require("match")), db: Session = Depends(get_db)):
    t = get_or_404(db, PaymentTransaction, tid)
    guard(user, t)
    return _wf(workflow.manual_match, db, user, t, [a.model_dump() for a in body.allocations], body.reason)


# ---------------------------------------------------------------------------------------------- matching
class RunIn(BaseModel):
    entity_id: int
    txn_ids: list[int] | None = None


@router.post("/matching/run")
def run_matching(body: RunIn, user: User = Depends(require("match", "ingest")), db: Session = Depends(get_db)):
    e = entity_for(db, user, body.entity_id)
    return engine.run_matching(db, e, body.txn_ids, user.name, user.id)


@router.get("/proposals")
def proposals(entity_id: int, status: str = "pending_review", min_conf: float = 0.0, max_conf: float = 1.0, limit: int = 100, offset: int = 0,
              user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    limit, offset = page(limit, offset)
    try:
        sts = [ProposalStatus(s) for s in status.split(",")]
    except ValueError:
        raise HTTPException(400, "Unknown status")
    base = select(MatchProposal).where(MatchProposal.entity_id == entity_id, MatchProposal.status.in_(sts),
                                       MatchProposal.confidence >= min_conf, MatchProposal.confidence <= max_conf)
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    rows = db.execute(base.order_by(MatchProposal.confidence.desc(), MatchProposal.id).limit(limit).offset(offset)).scalars().all()
    return {"total": total, "items": [S.proposal(p, with_txn=True) for p in rows]}


@router.get("/proposals/{pid}")
def proposal_detail(pid: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    p = get_or_404(db, MatchProposal, pid)
    guard(user, p)
    return S.proposal(p, with_txn=True)


@router.post("/proposals/{pid}/accept")
def accept(pid: int, user: User = Depends(require("match")), db: Session = Depends(get_db)):
    p = get_or_404(db, MatchProposal, pid)
    guard(user, p)
    # Four-eyes on risky matches: anything the model could not clear by itself needs a *different* person than whoever
    # will later release the batch (enforced at release) — and write-off beyond tolerance never comes through here.
    _wf(actions.apply_proposal, db, None, p, user.name, user.id)
    return S.proposal(p, with_txn=True)


class RejectIn(BaseModel):
    reason: str = Field("", max_length=300)


@router.post("/proposals/{pid}/reject")
def reject(pid: int, body: RejectIn, user: User = Depends(require("match")), db: Session = Depends(get_db)):
    p = get_or_404(db, MatchProposal, pid)
    guard(user, p)
    _wf(actions.reject_proposal, db, p, user.name, user.id, body.reason)
    return S.proposal(p)


@router.post("/proposals/{pid}/reverse")
def reverse(pid: int, body: RejectIn, user: User = Depends(require("match", "approve")), db: Session = Depends(get_db)):
    if len(body.reason.strip()) < 5:
        raise HTTPException(422, "Give a reason (5+ characters) for reversing an applied match")
    p = get_or_404(db, MatchProposal, pid)
    guard(user, p)
    _wf(actions.reverse_proposal, db, p, user.name, user.id, body.reason)
    return S.proposal(p)


# ---------------------------------------------------------------------------------------------- exceptions
@router.get("/exceptions")
def exceptions(entity_id: int, status: str = "open,in_progress,pending_approval", type: str = "", assignee_id: int | None = None, limit: int = 100,
               offset: int = 0, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    limit, offset = page(limit, offset)
    try:
        sts = [ExceptionStatus(s) for s in status.split(",") if s]
        types = [ExceptionType(s) for s in type.split(",") if s]
    except ValueError:
        raise HTTPException(400, "Unknown status or type")
    base = select(ExceptionCase).where(ExceptionCase.entity_id == entity_id)
    if sts:
        base = base.where(ExceptionCase.status.in_(sts))
    if types:
        base = base.where(ExceptionCase.type.in_(types))
    if assignee_id:
        base = base.where(ExceptionCase.assignee_id == assignee_id)
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    rows = db.execute(base.order_by(ExceptionCase.priority.desc(), ExceptionCase.id).limit(limit).offset(offset)).scalars().all()
    return {"total": total, "items": [S.exception(x) for x in rows]}


@router.get("/exceptions/{xid}")
def exception_detail(xid: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    x = get_or_404(db, ExceptionCase, xid)
    guard(user, x)
    out = S.exception(x)
    if x.txn_id:
        out["txn"] = S.txn(db.get(PaymentTransaction, x.txn_id))
    return out


class AssignIn(BaseModel):
    assignee_id: int | None


@router.post("/exceptions/{xid}/assign")
def assign(xid: int, body: AssignIn, user: User = Depends(require("match", "approve")), db: Session = Depends(get_db)):
    x = get_or_404(db, ExceptionCase, xid)
    guard(user, x)
    if body.assignee_id and not db.get(User, body.assignee_id):
        raise HTTPException(404, "User not found")
    x.assignee_id = body.assignee_id
    if x.status == ExceptionStatus.open and body.assignee_id:
        x.status = ExceptionStatus.in_progress
    audit.log(db, user.name, "exception.assigned", "ExceptionCase", x.id, x.entity_id, {"assignee_id": body.assignee_id}, user.id)
    return S.exception(x)


class NoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


@router.post("/exceptions/{xid}/notes")
def add_note(xid: int, body: NoteIn, user: User = Depends(require("match", "approve")), db: Session = Depends(get_db)):
    x = get_or_404(db, ExceptionCase, xid)
    guard(user, x)
    x.notes.append(ExceptionNote(user_id=user.id, text=body.text))
    db.flush()
    return S.exception(x)


class ResolveIn(BaseModel):
    action: str
    params: dict = {}


@router.post("/exceptions/{xid}/resolve")
def resolve(xid: int, body: ResolveIn, user: User = Depends(require("match")), db: Session = Depends(get_db)):
    x = get_or_404(db, ExceptionCase, xid)
    guard(user, x)
    res = _wf(workflow.resolve_exception, db, user, x, body.action, body.params)
    db.flush()
    return {"result": res, "exception": S.exception(x)}


# ---------------------------------------------------------------------------------------------- approvals
@router.get("/approvals")
def approvals(entity_id: int, status: str = "pending", limit: int = 100, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    try:
        sts = [ApprovalStatus(s) for s in status.split(",")]
    except ValueError:
        raise HTTPException(400, "Unknown status")
    rows = db.execute(select(ApprovalRequest).where(ApprovalRequest.entity_id == entity_id, ApprovalRequest.status.in_(sts))
                      .order_by(ApprovalRequest.id.desc()).limit(min(limit, 500))).scalars().all()
    users = {u.id: u for u in db.execute(select(User)).scalars()}
    return [{**S.approval(a, users), "can_decide": user.role in workflow.P["approve"] and a.requested_by != user.id and workflow.RANK[user.role] >= workflow.RANK[a.required_role]}
            for a in rows]


class DecideIn(BaseModel):
    approve: bool
    note: str = Field("", max_length=500)


@router.post("/approvals/{aid}/decide")
def decide(aid: int, body: DecideIn, user: User = Depends(require("approve")), db: Session = Depends(get_db)):
    a = get_or_404(db, ApprovalRequest, aid)
    guard(user, a)
    if not body.approve and not body.note.strip():
        raise HTTPException(422, "Give a reason when rejecting")
    _wf(workflow.decide, db, user, a, body.approve, body.note)
    return S.approval(a)


# ---------------------------------------------------------------------------------------------- TDS
@router.get("/tds")
def tds(entity_id: int, certificate: str = "", fy: str = "", limit: int = 200, offset: int = 0, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    limit, offset = page(limit, offset)
    base = select(TdsEntry).where(TdsEntry.entity_id == entity_id, TdsEntry.reversed.is_(False))
    if certificate == "pending":
        base = base.where(TdsEntry.certificate_received.is_(False))
    elif certificate == "received":
        base = base.where(TdsEntry.certificate_received.is_(True))
    if fy:
        base = base.where(TdsEntry.fy == fy)
    rows = db.execute(base.order_by(TdsEntry.txn_date.desc()).limit(limit).offset(offset)).scalars().all()
    total_amt = db.scalar(select(func.coalesce(func.sum(TdsEntry.amount_minor), 0)).select_from(base.subquery()))
    return {"items": [S.tds(t) for t in rows], "total_amount_minor": int(total_amt or 0)}


class CertIn(BaseModel):
    certificate_received: bool | None = None
    in_26as: bool | None = None


@router.post("/tds/{tid}/status")
def tds_status(tid: int, body: CertIn, user: User = Depends(require("match", "approve")), db: Session = Depends(get_db)):
    t = get_or_404(db, TdsEntry, tid)
    guard(user, t)
    if body.certificate_received is not None:
        t.certificate_received = body.certificate_received
    if body.in_26as is not None:
        t.in_26as = body.in_26as
    audit.log(db, user.name, "tds.status", "TdsEntry", t.id, t.entity_id, body.model_dump(exclude_none=True), user.id)
    return S.tds(t)
