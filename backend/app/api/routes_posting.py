"""Posting batches, analytics, audit, scoring models, demo."""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import analytics, audit, evidence
from ..config import settings
from ..db import get_db
from ..matching import monitoring, scoring
from ..models import utcnow, AuditLog, PeriodLock, MatchFeedback, MatchProposal, PostingBatch, ProposalStatus, ScoringModel, User
from ..posting import service as posting
from ..security import require
from . import serializers as S
from .deps import entity_for, get_or_404, guard, page

router = APIRouter()


def _p(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except posting.PostingError as e:
        raise HTTPException(409, str(e))


@router.get("/posting/batches")
def batches(entity_id: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    rows = db.execute(select(PostingBatch).where(PostingBatch.entity_id == entity_id).order_by(PostingBatch.id.desc()).limit(100)).scalars().all()
    pending = db.scalar(select(func.count()).select_from(MatchProposal).where(
        MatchProposal.entity_id == entity_id, MatchProposal.batch_id.is_(None),
        MatchProposal.status.in_([ProposalStatus.accepted, ProposalStatus.auto_approved])))
    return {"items": [S.batch(b) for b in rows], "ready_to_batch": pending}


class BuildIn(BaseModel):
    entity_id: int


@router.post("/posting/batches")
def build(body: BuildIn, user: User = Depends(require("build_batch")), db: Session = Depends(get_db)):
    e = entity_for(db, user, body.entity_id)
    return S.batch(_p(posting.build_batch, db, e, user), lines=True)


@router.get("/posting/batches/{bid}")
def batch_detail(bid: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    b = get_or_404(db, PostingBatch, bid)
    guard(user, b)
    return S.batch(b, lines=True)


@router.post("/posting/batches/{bid}/release")
def release(bid: int, user: User = Depends(require("release_batch")), db: Session = Depends(get_db)):
    b = get_or_404(db, PostingBatch, bid)
    guard(user, b)
    _p(posting.release_batch, db, b, user)
    return S.batch(b)


class ExportIn(BaseModel):
    connector: str = "generic_csv"


@router.post("/posting/batches/{bid}/export")
def export(bid: int, body: ExportIn, user: User = Depends(require("release_batch", "build_batch")), db: Session = Depends(get_db)):
    b = get_or_404(db, PostingBatch, bid)
    guard(user, b)
    name, content, ctype = _p(posting.export_batch, db, b, body.connector, user)
    return Response(content, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Checksum-SHA256": b.checksum or ""})


@router.get("/posting/batches/{bid}/download")
def download(bid: int, user: User = Depends(require("release_batch", "build_batch", "audit")), db: Session = Depends(get_db)):
    b = get_or_404(db, PostingBatch, bid)
    guard(user, b)
    name, data = _p(posting.download_stored, db, b, user)
    return Response(data, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Checksum-SHA256": b.checksum or ""})


class AckIn(BaseModel):
    erp_docs: dict[str, str] = Field(..., description="voucher_no -> document number returned by the ERP")


@router.post("/posting/batches/{bid}/ack")
def ack(bid: int, body: AckIn, user: User = Depends(require("release_batch", "build_batch")), db: Session = Depends(get_db)):
    b = get_or_404(db, PostingBatch, bid)
    guard(user, b)
    _p(posting.acknowledge, db, b, body.erp_docs, user)
    return S.batch(b)


class FailIn(BaseModel):
    error: str = Field(..., min_length=3)


@router.post("/posting/batches/{bid}/fail")
def fail(bid: int, body: FailIn, user: User = Depends(require("release_batch", "build_batch")), db: Session = Depends(get_db)):
    b = get_or_404(db, PostingBatch, bid)
    guard(user, b)
    posting.fail_batch(db, b, body.error, user)
    return S.batch(b)


@router.post("/posting/batches/{bid}/cancel")
def cancel(bid: int, user: User = Depends(require("release_batch", "build_batch")), db: Session = Depends(get_db)):
    b = get_or_404(db, PostingBatch, bid)
    guard(user, b)
    _p(posting.cancel_batch, db, b, user)
    return S.batch(b)


# ---------------------------------------------------------------------------------------------- analytics
@router.get("/analytics/summary")
def a_summary(entity_id: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    return analytics.summary(db, entity_id)


@router.get("/analytics/trend")
def a_trend(entity_id: int, days: int = 30, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    return analytics.match_trend(db, entity_id, min(days, 365))


@router.get("/analytics/cash-position")
def a_cash(entity_id: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    return analytics.cash_position(db, entity_id)


@router.get("/analytics/forecast")
def a_forecast(entity_id: int, weeks: int = 8, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    return analytics.forecast(db, entity_id, min(weeks, 26))


@router.get("/analytics/productivity")
def a_prod(entity_id: int, days: int = 30, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    return analytics.productivity(db, entity_id, min(days, 365))


# ---------------------------------------------------------------------------------------------- audit
@router.get("/audit")
def audit_list(entity_id: int | None = None, object_type: str = "", object_id: str = "", action: str = "", actor: str = "", limit: int = 100, offset: int = 0,
               user: User = Depends(require("audit")), db: Session = Depends(get_db)):
    limit, offset = page(limit, offset)
    q = select(AuditLog)
    if entity_id:
        entity_for(db, user, entity_id)
        q = q.where(AuditLog.entity_id == entity_id)
    if object_type:
        q = q.where(AuditLog.object_type == object_type)
    if object_id:
        q = q.where(AuditLog.object_id == object_id)
    if action:
        q = q.where(AuditLog.action.like(f"{action}%"))
    if actor:
        q = q.where(AuditLog.actor.ilike(f"%{actor}%"))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.execute(q.order_by(AuditLog.seq.desc()).limit(limit).offset(offset)).scalars().all()
    return {"total": total, "items": [S.audit_row(a) for a in rows]}


@router.get("/audit/verify")
def audit_verify(user: User = Depends(require("audit")), db: Session = Depends(get_db)):
    return audit.verify_chain(db)


@router.get("/audit/export")
def audit_export(entity_id: int | None = None, user: User = Depends(require("audit")), db: Session = Depends(get_db)):
    q = select(AuditLog).order_by(AuditLog.seq)
    if entity_id:
        entity_for(db, user, entity_id)
        q = q.where(AuditLog.entity_id == entity_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["seq", "timestamp_utc", "actor", "action", "object_type", "object_id", "entity_id", "detail_json", "prev_hash", "hash"])
    import json

    for a in db.execute(q).scalars():
        w.writerow([a.seq, a.ts.isoformat(), a.actor, a.action, a.object_type, a.object_id, a.entity_id, json.dumps(a.detail, sort_keys=True), a.prev_hash, a.hash])
    audit.log(db, user.name, "audit.exported", "AuditLog", "-", entity_id, {}, user.id)
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="audit_log.csv"'})


@router.get("/audit/evidence-pack")
def evidence_pack(entity_id: int, date_from: date | None = None, date_to: date | None = None, user: User = Depends(require("audit")), db: Session = Depends(get_db)):
    e = entity_for(db, user, entity_id)
    end = date_to or date.today()
    start = date_from or (end - timedelta(days=90))
    if start > end:
        raise HTTPException(400, "date_from is after date_to")
    if (end - start).days > 800:
        raise HTTPException(400, "Choose a range of at most about two years")
    import hashlib

    blob, manifest = evidence.build(db, e, start, end, user)
    audit.log(db, user.name, "evidence.exported", "LegalEntity", e.id, e.id, {"from": str(start), "to": str(end), "sha256": hashlib.sha256(blob).hexdigest(),
                                                                                "chain_ok": manifest["audit_chain"]["ok"]}, user.id)
    return Response(blob, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="evidence_{e.id}_{start}_{end}.zip"'})


# ---------------------------------------------------------------------------------------------- period lock
@router.get("/periods/lock")
def get_lock(entity_id: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    lk = db.get(PeriodLock, entity_id)
    return {"locked_through": S.d(lk.locked_through) if lk else None, "note": lk.note if lk else "", "locked_at": S.d(lk.locked_at) if lk else None,
            "locked_by": lk.locked_by if lk else None}


class LockIn(BaseModel):
    entity_id: int
    locked_through: date | None = None      # None removes the lock
    note: str = Field("", max_length=300)


@router.post("/periods/lock")
def set_lock(body: LockIn, user: User = Depends(require("configure")), db: Session = Depends(get_db)):
    """Close the books through a date. Closing needs no reason; moving the lock earlier or removing it (re-opening) needs one and is audited."""
    entity_for(db, user, body.entity_id)
    lk = db.get(PeriodLock, body.entity_id)
    new = body.locked_through
    if new and new > date.today():
        raise HTTPException(422, "A period cannot be closed in the future")
    reopening = lk is not None and (new is None or new < lk.locked_through)
    if reopening and len(body.note.strip()) < 5:
        raise HTTPException(422, "Give a reason (at least 5 characters) to re-open a closed period")
    before = lk.locked_through.isoformat() if lk else None
    if new is None:
        if lk:
            db.delete(lk)
    elif lk:
        lk.locked_through, lk.locked_by, lk.locked_at, lk.note = new, user.id, utcnow(), body.note
    else:
        db.add(PeriodLock(entity_id=body.entity_id, locked_through=new, locked_by=user.id, note=body.note))
    if reopening:
        from .. import notify

        notify.emit(db, body.entity_id, "period.reopened", "A closed accounting period was re-opened", f"By {user.name}: {body.note}", f"reopen:{body.entity_id}:{utcnow():%Y%m%d%H%M%S}")
    audit.log(db, user.name, "period.reopened" if reopening else "period.locked", "LegalEntity", body.entity_id, body.entity_id,
              {"before": before, "after": new.isoformat() if new else None, "note": body.note}, user.id)
    return {"locked_through": new.isoformat() if new else None}


# ---------------------------------------------------------------------------------------------- scoring models
@router.get("/models")
def models(user: User = Depends(require("view")), db: Session = Depends(get_db)):
    scoring.ensure_model(db)
    rows = db.execute(select(ScoringModel).order_by(ScoringModel.id.desc())).scalars().all()
    pos = db.scalar(select(func.coalesce(func.sum(MatchFeedback.label), 0))) or 0
    n = db.scalar(select(func.count()).select_from(MatchFeedback)) or 0
    return {"feedback": {"total": n, "positive": int(pos), "negative": n - int(pos)},
            "items": [{"id": m.id, "version": m.version, "active": m.active, "shadow": m.shadow, "weights": m.weights, "metrics": m.metrics, "note": m.note, "created_at": S.d(m.created_at)} for m in rows]}


@router.post("/models/retrain")
def retrain(user: User = Depends(require("train")), db: Session = Depends(get_db)):
    m = scoring.retrain_candidate(db)
    if not m:
        raise HTTPException(409, "Not enough balanced feedback yet (need 20+ decisions with at least 10 accepted and 5 rejected)")
    audit.log(db, user.name, "model.retrained", "ScoringModel", m.id, None, {"version": m.version, "metrics": m.metrics}, user.id)
    return {"id": m.id, "version": m.version, "metrics": m.metrics}


@router.post("/models/{mid}/activate")
def activate(mid: int, user: User = Depends(require("train")), db: Session = Depends(get_db)):
    m = get_or_404(db, ScoringModel, mid)
    for x in db.execute(select(ScoringModel)).scalars():
        x.active = x.id == m.id
    m.shadow = False   # a promoted model is no longer a shadow
    audit.log(db, user.name, "model.activated", "ScoringModel", m.id, None, {"version": m.version}, user.id)
    return {"active": m.version}


class ShadowIn(BaseModel):
    enabled: bool = True


@router.post("/models/{mid}/shadow")
def set_shadow(mid: int, body: ShadowIn, user: User = Depends(require("train")), db: Session = Depends(get_db)):
    """Score this candidate alongside the active model on live proposals. It never decides anything; only one shadow runs at a time."""
    m = get_or_404(db, ScoringModel, mid)
    if m.active and body.enabled:
        raise HTTPException(409, "The active model cannot be a shadow")
    if body.enabled:
        for x in db.execute(select(ScoringModel).where(ScoringModel.shadow.is_(True))).scalars():
            x.shadow = False
    m.shadow = body.enabled
    audit.log(db, user.name, "model.shadow_started" if body.enabled else "model.shadow_stopped", "ScoringModel", m.id, None, {"version": m.version}, user.id)
    return {"shadow": m.version if body.enabled else None}


@router.get("/models/health")
def model_health(entity_id: int, window_days: int = 14, baseline_days: int = 60, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    return monitoring.health(db, entity_id, max(3, min(window_days, 90)), max(7, min(baseline_days, 365)))


@router.get("/models/shadow")
def model_shadow(entity_id: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    return monitoring.shadow_report(db, entity_id)


# ---------------------------------------------------------------------------------------------- demo
@router.post("/demo/seed")
def demo_seed(seed: int = 42, run_matching: bool = True, user: User = Depends(require("demo")), db: Session = Depends(get_db)):
    if not settings.allow_demo:
        raise HTTPException(403, "Demo seeding is disabled")
    from ..seed.demo import seed_demo

    return seed_demo(db, run_matching=run_matching, seed=seed)
