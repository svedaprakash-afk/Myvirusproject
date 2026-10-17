"""Auditor evidence pack: one zip of CSV/JSON extracts that lets an auditor re-perform the key controls without database access.

A SHA-256 manifest covers every file, and the audit-log hash chain is verified at export time and the result recorded in it. The pack is a
snapshot: it states what the system held and who decided what, it does not certify that the controls were adequate.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import date, datetime, time, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .models import (DEFAULT_ENTITY_CONFIG, AdvanceApplication, ApprovalPolicy, ApprovalRequest, AuditLog, Customer, ExceptionCase, LegalEntity, MatchProposal,
                     PeriodLock, PostingBatch, PostingLine, ProposalStatus, ScoringModel, ToleranceRule, User, utcnow)

README = """Evidence pack
=============
Generated {when} UTC by {who} for {entity} ({start} to {end}).

manifest.json            SHA-256 of every file below, the audit-chain verification result and row counts
audit_log.csv            hash-chained audit entries in range (seq, hash and prev_hash let you re-verify the chain)
approvals.csv            maker-checker requests: requester, decider, required role, amounts, notes
segregation_checks.csv   every case where one person appears on both sides of a control (should be empty)
match_decisions.csv      each match proposal: confidence, model version, strategy, guardrails that blocked auto-approval, who decided
auto_reversals.csv       straight-through matches that a person later reversed
posting_batches.csv      batches with builder, releaser, export checksum and ERP acknowledgement
posting_lines.csv        the journal lines of those batches
exceptions.csv           exceptions with their resolution
advance_applications.csv on-account cash applied to later invoices
users_roles.csv          who could do what (no credentials)
config_snapshot.json     matching thresholds, ledgers, tolerances, approval limits and the period lock as they stood at export
models.csv               scoring-model versions and whether each is active

Limits: this is a snapshot of system records. It does not prove the underlying bank or ERP data is complete.
"""


def _csv(header: list[str], rows) -> tuple[bytes, int]:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    n = 0
    for r in rows:
        w.writerow(r)
        n += 1
    return buf.getvalue().encode(), n


def _in(dt: datetime | None, lo: datetime, hi: datetime) -> bool:
    return bool(dt) and lo <= dt <= hi


def build(db: Session, entity: LegalEntity, start: date, end: date, actor: User) -> tuple[bytes, dict]:
    lo, hi = datetime.combine(start, time.min), datetime.combine(end, time.max)
    users = {u.id: u for u in db.execute(select(User)).scalars()}
    nm = lambda i: users[i].email if i in users else ""
    files: dict[str, tuple[bytes, int]] = {}

    logs = db.execute(select(AuditLog).where(AuditLog.entity_id == entity.id, AuditLog.ts >= lo, AuditLog.ts <= hi).order_by(AuditLog.seq)).scalars().all()
    files["audit_log.csv"] = _csv(["seq", "timestamp_utc", "actor", "action", "object_type", "object_id", "detail_json", "prev_hash", "hash"],
                                  ([a.seq, a.ts.isoformat(), a.actor, a.action, a.object_type, a.object_id, json.dumps(a.detail, sort_keys=True), a.prev_hash, a.hash] for a in logs))

    apps = [a for a in db.execute(select(ApprovalRequest).where(ApprovalRequest.entity_id == entity.id)).scalars() if _in(a.created_at, lo, hi)]
    files["approvals.csv"] = _csv(["id", "kind", "status", "amount_minor", "required_role", "requested_by", "decided_by", "reason", "decision_note", "created_at", "decided_at"],
                                  ([a.id, a.kind.value, a.status.value, a.amount_minor, a.required_role.value, nm(a.requested_by), nm(a.decided_by), a.reason, a.decision_note or "",
                                    a.created_at.isoformat(), a.decided_at.isoformat() if a.decided_at else ""] for a in apps))

    props = [p for p in db.execute(select(MatchProposal).where(MatchProposal.entity_id == entity.id)).scalars() if _in(p.created_at, lo, hi)]
    files["match_decisions.csv"] = _csv(["id", "txn_id", "customer_id", "status", "auto", "strategy", "confidence", "model_version", "applied_minor", "tds_minor", "writeoff_minor",
                                         "unapplied_minor", "short_minor", "guardrails", "decided_by", "decided_at", "batch_id", "summary"],
                                        ([p.id, p.txn_id, p.customer_id or "", p.status.value, int(p.auto), p.strategy, p.confidence, p.model_version, p.applied_minor, p.tds_minor,
                                          p.writeoff_minor, p.unapplied_minor, p.short_minor, "; ".join(p.guardrails or []), nm(p.decided_by),
                                          p.decided_at.isoformat() if p.decided_at else "", p.batch_id or "", (p.explanation or {}).get("summary", "")] for p in props))
    files["auto_reversals.csv"] = _csv(["proposal_id", "txn_id", "confidence", "model_version", "created_at"],
                                       ([p.id, p.txn_id, p.confidence, p.model_version, p.created_at.isoformat()] for p in props if p.auto and p.status == ProposalStatus.reversed))

    batches = [b for b in db.execute(select(PostingBatch).where(PostingBatch.entity_id == entity.id)).scalars() if _in(b.created_at, lo, hi)]
    files["posting_batches.csv"] = _csv(["id", "status", "connector", "created_by", "approved_by", "created_at", "exported_at", "acked_at", "checksum", "total_minor", "error"],
                                        ([b.id, b.status.value, b.connector or "", nm(b.created_by), nm(b.approved_by), b.created_at.isoformat(), b.exported_at.isoformat() if b.exported_at else "",
                                          b.acked_at.isoformat() if b.acked_at else "", b.checksum or "", b.total_minor, b.error or ""] for b in batches))
    bids = {b.id for b in batches}
    lines = db.execute(select(PostingLine).where(PostingLine.batch_id.in_(bids))).scalars().all() if bids else []
    files["posting_lines.csv"] = _csv(["batch_id", "voucher_no", "line_type", "account", "debit_minor", "credit_minor", "currency", "base_debit_minor", "base_credit_minor", "posting_date",
                                       "narration", "erp_doc_no"],
                                      ([x.batch_id, x.voucher_no, x.line_type, x.account, x.debit_minor, x.credit_minor, x.currency, x.base_debit_minor, x.base_credit_minor,
                                        x.posting_date.isoformat(), x.narration, x.erp_doc_no or ""] for x in lines))

    sod = []
    for b in batches:
        if b.created_by and b.created_by == b.approved_by:
            sod.append(["batch", b.id, "built and released by the same person", nm(b.created_by)])
        for pid, who in {(p.id, p.decided_by) for p in props if p.batch_id == b.id and p.decided_by}:
            if who == b.approved_by:
                sod.append(["batch", b.id, f"released by the person who accepted match {pid}", nm(who)])
    for a in apps:
        if a.decided_by and a.decided_by == a.requested_by:
            sod.append(["approval", a.id, "requested and decided by the same person", nm(a.requested_by)])
    files["segregation_checks.csv"] = _csv(["object", "id", "finding", "person"], sod)

    excs = [x for x in db.execute(select(ExceptionCase).where(ExceptionCase.entity_id == entity.id)).scalars() if _in(x.created_at, lo, hi)]
    files["exceptions.csv"] = _csv(["id", "type", "status", "amount_minor", "reason", "assignee", "created_at", "resolved_at", "resolution"],
                                   ([x.id, x.type.value, x.status.value, x.amount_minor, x.reason, nm(x.assignee_id), x.created_at.isoformat(), x.resolved_at.isoformat() if x.resolved_at else "",
                                     x.resolution or ""] for x in excs))
    advs = [a for a in db.execute(select(AdvanceApplication).where(AdvanceApplication.entity_id == entity.id)).scalars() if _in(a.created_at, lo, hi)]
    files["advance_applications.csv"] = _csv(["id", "balance_id", "invoice_id", "customer_id", "amount_minor", "basis", "status", "decided_by", "decided_at"],
                                             ([a.id, a.balance_id, a.invoice_id, a.customer_id, a.amount_minor, a.basis, a.status, nm(a.decided_by), a.decided_at.isoformat() if a.decided_at else ""] for a in advs))
    files["users_roles.csv"] = _csv(["email", "name", "role", "active", "entity_scope"],
                                    ([u.email, u.name, u.role.value, int(u.active), ",".join(map(str, u.entity_ids or [])) or "all"] for u in users.values() if not u.entity_ids or entity.id in u.entity_ids))
    models = db.execute(select(ScoringModel).order_by(ScoringModel.id)).scalars().all()
    files["models.csv"] = _csv(["version", "active", "shadow", "created_at", "metrics", "note"], ([m.version, int(m.active), int(m.shadow), m.created_at.isoformat(), json.dumps(m.metrics), m.note] for m in models))

    lock = db.get(PeriodLock, entity.id)
    snap = {"entity": {"id": entity.id, "name": entity.name, "gstin": entity.gstin, "base_currency": entity.base_currency},
            "config": {**DEFAULT_ENTITY_CONFIG, **(entity.config or {})}, "ledgers": entity.ledgers,
            "tolerances": [{"customer_id": t.customer_id, "currency": t.currency, "abs_minor": t.abs_minor, "pct_bp": t.pct_bp}
                           for t in db.execute(select(ToleranceRule).where(ToleranceRule.entity_id == entity.id)).scalars()],
            "approval_limits": [{"kind": p.kind.value, "min_minor": p.min_minor, "max_minor": p.max_minor, "approver_role": p.approver_role.value}
                                for p in db.execute(select(ApprovalPolicy).where(ApprovalPolicy.entity_id == entity.id)).scalars()],
            "period_lock": {"locked_through": lock.locked_through.isoformat(), "note": lock.note} if lock else None}
    files["config_snapshot.json"] = (json.dumps(snap, indent=2, sort_keys=True, default=str).encode(), 1)

    chain = audit.verify_chain(db)
    now = utcnow()
    manifest = {"entity_id": entity.id, "entity": entity.name, "from": start.isoformat(), "to": end.isoformat(), "generated_at": now.isoformat() + "Z", "generated_by": actor.email,
                "audit_chain": chain, "files": {n: {"sha256": hashlib.sha256(b).hexdigest(), "rows": rows} for n, (b, rows) in files.items()},
                "segregation_findings": len(sod)}
    readme = README.format(when=now.strftime("%Y-%m-%d %H:%M"), who=actor.email, entity=entity.name, start=start, end=end).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", readme)
        z.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        for n, (b, _) in files.items():
            z.writestr(n, b)
    return buf.getvalue(), manifest
