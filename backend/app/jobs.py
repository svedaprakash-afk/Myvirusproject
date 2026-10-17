"""Scheduled work: FX reference-rate fetch, remittance-inbox polling, bank-feed sync, then matching.

Runs in a separate worker process (`python -m app.cli worker`) so the API can scale independently. Each unit of work claims a
(job, time-slot) row first; the unique constraint is the lock, so any number of worker replicas can run safely and a slot is
executed once. Failures are recorded, never raised into the loop."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import fxfeed, notify
from .ingestion import feeds, mailbox
from .matching import engine
from .models import BankAccount, JobRun, LegalEntity, utcnow

log = logging.getLogger("cashapp.worker")
IST = timedelta(hours=5, minutes=30)
FX_AFTER_IST_HOUR = 14        # FBIL publishes ~1 pm IST; first attempt at 2 pm, then every cycle until it succeeds
FX_MAX_ATTEMPTS = 8


def _claim(db: Session, name: str, slot: str) -> JobRun | None:
    row = JobRun(name=name, slot=slot)
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return None
    db.commit()
    return row


def _finish(db: Session, row: JobRun, ok: bool, detail: dict) -> None:
    row.status, row.finished_at, row.detail = ("ok" if ok else "failed"), utcnow(), detail
    db.commit()


def run_once(Session_: sessionmaker, now: datetime | None = None, fx_transport=None, imap_factory=None, feed_transport=None, smtp_factory=None,
             notify_http=None) -> list[dict]:
    """One scheduler tick. `now` is UTC (injectable for tests). Returns what ran."""
    now = now or utcnow()
    ist = now + IST
    ran: list[dict] = []
    db = Session_()
    try:
        # ---- FX: weekdays after 14:00 IST, once per day, retried each tick until success --------------------------------
        if ist.weekday() < 5 and ist.hour >= FX_AFTER_IST_HOUR and fxfeed.settings.fx_feed_url:
            done = db.execute(select(JobRun).where(JobRun.name == "fx_fetch", JobRun.slot == f"{ist.date()}", JobRun.status == "ok")).first()
            if not done:
                attempts = db.execute(select(JobRun).where(JobRun.name == "fx_fetch", JobRun.slot.like(f"{ist.date()}#%"))).scalars().all()
                if len(attempts) < FX_MAX_ATTEMPTS:
                    row = _claim(db, "fx_fetch", f"{ist.date()}#{len(attempts) + 1}")
                    if row:
                        try:
                            res = fxfeed.run_feed(db, "worker", transport=fx_transport)
                            _finish(db, row, True, res)
                            db.add(JobRun(name="fx_fetch", slot=f"{ist.date()}", status="ok", detail=res, finished_at=utcnow()))
                            db.commit()
                        except Exception as e:
                            db.rollback()
                            _finish(db, row, False, {"error": str(e)[:500]})
                        ran.append({"job": "fx_fetch", "status": row.status})
        # ---- per entity: inbox poll, bank sync, matching ------------------------------------------------------------
        for e in db.execute(select(LegalEntity)).scalars().all():
            integ = (e.config or {}).get("integrations") or {}
            new_work = False
            mb = integ.get("mailbox") or {}
            if mb.get("host"):
                iv = max(int(mb.get("poll_minutes", 10)), 1)
                row = _claim(db, f"mailbox:{e.id}", f"{int(now.timestamp()) // (iv * 60)}")
                if row:
                    try:
                        res = mailbox.poll_mailbox(db, e.id, mb, "worker", imap_factory=imap_factory)
                        db.commit()
                        _finish(db, row, not res["errors"], res)
                        new_work |= bool(res["ingested"])
                    except Exception as ex:
                        db.rollback()
                        _finish(db, row, False, {"error": str(ex)[:500]})
                    ran.append({"job": f"mailbox:{e.id}", "status": row.status})
            for b in db.execute(select(BankAccount).where(BankAccount.entity_id == e.id)).scalars().all():
                if (b.feed or {}).get("type") in feeds.FEED_TYPES and feeds.FEED_TYPES[b.feed["type"]][1] == "available":
                    iv = max(int(b.feed.get("interval_minutes", 60)), 1)
                    row = _claim(db, f"bank_sync:{b.id}", f"{int(now.timestamp()) // (iv * 60)}")
                    if row:
                        try:
                            res = feeds.sync_bank(db, b, "worker", None, feed_transport)
                            db.commit()
                            _finish(db, row, not res["errors"], {k: res[k] for k in ("feed", "inserted")} | {"errors": len(res["errors"])})
                            new_work |= bool(res["inserted"])
                        except Exception as ex:
                            db.rollback()
                            _finish(db, row, False, {"error": str(ex)[:500]})
                        ran.append({"job": f"bank_sync:{b.id}", "status": row.status})
            if (e.config or {}).get("notifications"):
                row = _claim(db, f"notify_scan:{e.id}", f"{int(now.timestamp()) // 900}")
                if row:
                    try:
                        res = notify.scan(db, e, now)
                        db.commit()
                        _finish(db, row, True, res)
                    except Exception as ex:
                        db.rollback()
                        _finish(db, row, False, {"error": str(ex)[:500]})
                    ran.append({"job": f"notify_scan:{e.id}", "status": row.status})
            if new_work:
                row = _claim(db, f"match:{e.id}", f"{int(now.timestamp())}")
                if row:
                    try:
                        res = engine.run_matching(db, e, actor="worker")
                        db.commit()
                        _finish(db, row, True, res)
                    except Exception as ex:
                        db.rollback()
                        _finish(db, row, False, {"error": str(ex)[:500]})
                    ran.append({"job": f"match:{e.id}", "status": row.status})
        row = _claim(db, "notify_send", f"{int(now.timestamp()) // 60}")
        if row:
            res: dict = {}
            try:
                res = notify.dispatch(db, now, smtp_factory=smtp_factory, http=notify_http)
                db.commit()
                _finish(db, row, not res["failed"], res)
            except Exception as ex:
                db.rollback()
                _finish(db, row, False, {"error": str(ex)[:500]})
            if res.get("sent") or res.get("failed") or res.get("retrying"):
                ran.append({"job": "notify_send", "status": row.status})
    finally:
        db.close()
    return ran


def loop(Session_: sessionmaker, interval: int = 60) -> None:  # pragma: no cover - thin wrapper
    log.info("worker started")
    while True:
        try:
            for r in run_once(Session_):
                log.info("job %s -> %s", r["job"], r["status"])
        except Exception:
            log.exception("worker tick failed")
        time.sleep(interval)
