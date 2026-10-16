"""Append-only, hash-chained audit log. Tampering with or deleting any row breaks verification."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog, utcnow

GENESIS = "0" * 64


def _digest(prev: str, seq: int, ts: str, actor: str, action: str, otype: str, oid: str, entity_id, detail: dict) -> str:
    body = json.dumps([prev, seq, ts, actor, action, otype, oid, entity_id, detail], sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def log(db: Session, actor: str, action: str, object_type: str, object_id: Any, entity_id: int | None = None,
        detail: dict | None = None, actor_id: int | None = None) -> AuditLog:
    last = db.execute(select(AuditLog).order_by(AuditLog.seq.desc()).limit(1).with_for_update()).scalar_one_or_none()
    seq = (last.seq + 1) if last else 1
    prev = last.hash if last else GENESIS
    ts = utcnow()
    detail = json.loads(json.dumps(detail or {}, default=str))
    h = _digest(prev, seq, ts.isoformat(), actor, action, object_type, str(object_id), entity_id, detail)
    row = AuditLog(seq=seq, ts=ts, actor=actor, actor_id=actor_id, action=action, object_type=object_type,
                   object_id=str(object_id), entity_id=entity_id, detail=detail, prev_hash=prev, hash=h)
    db.add(row)
    db.flush()
    return row


def verify_chain(db: Session) -> dict:
    prev, n = GENESIS, 0
    for r in db.execute(select(AuditLog).order_by(AuditLog.seq)).scalars():
        n += 1
        if r.seq != n:
            return {"ok": False, "checked": n - 1, "broken_at_seq": n, "reason": "sequence gap (row deleted?)"}
        if r.prev_hash != prev:
            return {"ok": False, "checked": n - 1, "broken_at_seq": r.seq, "reason": "prev_hash mismatch"}
        if _digest(prev, r.seq, r.ts.isoformat(), r.actor, r.action, r.object_type, r.object_id, r.entity_id, r.detail) != r.hash:
            return {"ok": False, "checked": n - 1, "broken_at_seq": r.seq, "reason": "row content altered"}
        prev = r.hash
    return {"ok": True, "checked": n, "head": prev}
