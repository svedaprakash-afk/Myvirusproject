"""Alerts to email, Slack and Microsoft Teams through a durable outbox.

`emit` only writes rows (inside the caller's transaction, so an alert exists exactly when the thing it reports was committed). `dispatch`, run by
the worker, sends them with retry and exponential back-off and records the outcome. Secrets are never stored: the configuration names environment
variables. Every outbound host is checked against the data-residency allow-list. Messages carry business facts (amounts, invoice and customer names
as the app shows them) and no credentials or bank account numbers.

Slack incoming-webhook and SMTP behaviour are standard; the Teams payload is the plain {"text": ...} form and has NOT been verified against a current
Teams tenant (Microsoft has been retiring connector webhooks in favour of Workflows).
"""
from __future__ import annotations

import os
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config
from .models import Notification, LegalEntity, utcnow

EVENTS = {
    "approval.requested": "An approval is waiting for someone with enough authority",
    "exception.sla_breach": "An exception passed its due time",
    "model.alert": "The matching model's health check found a problem",
    "batch.failed": "A posting batch was marked failed",
    "erp.unconfirmed": "A batch was exported but the ERP has not confirmed it",
    "auto.reversed": "A straight-through match was reversed by a person",
    "period.reopened": "A closed period was re-opened",
}
CHANNELS = ("email", "slack", "teams")
MAX_ATTEMPTS = 5


def validate(cfg: dict) -> list[str]:
    errs: list[str] = []
    for ch, c in (cfg or {}).items():
        if ch not in CHANNELS:
            errs.append(f"unknown channel '{ch}'")
            continue
        if not isinstance(c, dict):
            errs.append(f"{ch}: must be an object")
            continue
        for ev in c.get("events") or []:
            if ev not in EVENTS:
                errs.append(f"{ch}: unknown event '{ev}'")
        if ch == "email":
            if not c.get("host") or not c.get("from") or not c.get("to"):
                errs.append("email: host, from and at least one recipient are required")
            elif not config.egress_allowed(c["host"]):
                errs.append("email: host is not in ALLOWED_EGRESS_HOSTS (data-residency strict mode)")
            for k in ("username_env", "password_env"):
                if c.get(k) and not str(c[k]).replace("_", "").isalnum():
                    errs.append(f"email: {k} must be an environment variable name")
        else:
            env = c.get("webhook_env")
            if not env or not str(env).replace("_", "").isalnum():
                errs.append(f"{ch}: webhook_env must name an environment variable that holds the webhook URL")
            elif os.environ.get(env):
                host = urlparse(os.environ[env]).hostname or ""
                if not os.environ[env].startswith("https://"):
                    errs.append(f"{ch}: the webhook URL must be https")
                elif not config.egress_allowed(host):
                    errs.append(f"{ch}: webhook host is not in ALLOWED_EGRESS_HOSTS (data-residency strict mode)")
    return errs


def emit(db: Session, entity_id: int, event: str, subject: str, body: str, dedupe_key: str) -> int:
    """Queue `event` on every channel configured for it. Idempotent per (channel, dedupe_key). Returns rows queued."""
    e = db.get(LegalEntity, entity_id)
    cfg = ((e.config if e else None) or {}).get("notifications") or {}
    n = 0
    for ch in CHANNELS:
        c = cfg.get(ch)
        if not c or (c.get("events") and event not in c["events"]):
            continue
        if db.execute(select(Notification.id).where(Notification.entity_id == entity_id, Notification.channel == ch, Notification.dedupe_key == dedupe_key[:80])).first():
            continue
        db.add(Notification(entity_id=entity_id, event=event, channel=ch, subject=subject[:200], body=body, dedupe_key=dedupe_key[:80]))
        n += 1
    if n:
        db.flush()
    return n


def _send_email(c: dict, n: Notification, smtp_factory) -> None:
    config.check_egress(c["host"])
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = f"[Cash Application] {n.subject}", c["from"], ", ".join(c["to"])
    msg.set_content(n.body)
    user, pw = os.environ.get(c.get("username_env") or ""), os.environ.get(c.get("password_env") or "")
    factory = smtp_factory or (lambda host, port: smtplib.SMTP(host, port, timeout=20))
    with factory(c["host"], int(c.get("port", 587))) as s:
        if c.get("starttls", True):
            s.starttls(context=ssl.create_default_context())
        if user and pw:
            s.login(user, pw)
        s.send_message(msg)


def _send_hook(ch: str, c: dict, n: Notification, http) -> None:
    url = os.environ.get(c.get("webhook_env") or "")
    if not url:
        raise RuntimeError(f"environment variable {c.get('webhook_env')} is not set")
    config.check_egress(url)
    payload = {"text": f"*{n.subject}*\n{n.body}"} if ch == "slack" else {"text": f"**{n.subject}**\n\n{n.body}"}
    if http is None:
        import httpx

        r = httpx.post(url, json=payload, timeout=15)
        r.raise_for_status()
    else:
        http(url, payload)


def dispatch(db: Session, now: datetime | None = None, smtp_factory=None, http=None, limit: int = 50, only_ids: list[int] | None = None) -> dict:
    now = now or utcnow()
    q = select(Notification).where(Notification.status == "pending", Notification.next_attempt_at <= now)
    if only_ids is not None:
        q = q.where(Notification.id.in_(only_ids))
    rows = db.execute(q.order_by(Notification.id).limit(limit)).scalars().all()
    sent = failed = retry = 0
    cache: dict[int, dict] = {}
    for n in rows:
        cfg = cache.setdefault(n.entity_id, ((db.get(LegalEntity, n.entity_id).config or {}).get("notifications") or {}))
        c = cfg.get(n.channel)
        try:
            if not c:
                raise RuntimeError("channel is no longer configured")
            _send_email(c, n, smtp_factory) if n.channel == "email" else _send_hook(n.channel, c, n, http)
            n.status, n.sent_at, n.error = "sent", now, None
            sent += 1
        except Exception as ex:
            n.attempts += 1
            n.error = f"{type(ex).__name__}: {ex}"[:300]
            if n.attempts >= MAX_ATTEMPTS:
                n.status = "failed"
                failed += 1
            else:
                n.next_attempt_at = now + timedelta(minutes=2 ** n.attempts)
                retry += 1
    db.flush()
    return {"sent": sent, "retrying": retry, "failed": failed}


def scan(db: Session, entity: LegalEntity, now: datetime | None = None) -> dict:
    """Condition-based alerts the worker checks every cycle. Each is deduplicated, so a standing problem alerts once."""
    from .matching import monitoring
    from .models import ApprovalRequest, BatchStatus, ExceptionCase, ExceptionStatus, PostingBatch

    now = now or utcnow()
    cfg = {**((entity.config or {}))}
    made = 0
    open_states = [ExceptionStatus.open, ExceptionStatus.in_progress, ExceptionStatus.pending_approval]
    for x in db.execute(select(ExceptionCase).where(ExceptionCase.entity_id == entity.id, ExceptionCase.status.in_(open_states), ExceptionCase.due_at < now)).scalars():
        made += emit(db, entity.id, "exception.sla_breach", f"Exception #{x.id} is past its due time ({x.type.value.replace('_', ' ')})",
                     f"{x.reason}\nAmount: {x.amount_minor / 100:,.2f}. Due {x.due_at:%d %b %H:%M} UTC.", f"sla:{x.id}")
    h = monitoring.health(db, entity.id)
    for a in h["alerts"]:
        if a["level"] in ("high", "medium"):
            made += emit(db, entity.id, "model.alert", f"Matching model: {a['code'].replace('_', ' ')}", a["message"], f"model:{now:%Y%m%d}:{a['code']}")
    hours = int(cfg.get("erp_confirm_hours", 24))
    for b in db.execute(select(PostingBatch).where(PostingBatch.entity_id == entity.id, PostingBatch.status == BatchStatus.exported,
                                                   PostingBatch.exported_at < now - timedelta(hours=hours))).scalars():
        made += emit(db, entity.id, "erp.unconfirmed", f"Batch #{b.id} was exported {hours}+ hours ago and the ERP has not confirmed it",
                     f"Exported {b.exported_at:%d %b %H:%M} UTC, total {b.total_minor / 100:,.2f}. Check the ERP import log and acknowledge or fail the batch.", f"unconfirmed:{b.id}")
    return {"queued": made}
