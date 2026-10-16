"""Bank-agnostic and ERP-agnostic onboarding: statement profiles, feeds, export/import mappings, mailbox, FX rates."""
from __future__ import annotations

import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, fx
from .. import config
from ..config import EgressBlocked, INDIA_REGIONS, egress_allowed
from ..db import get_db
from ..indian import validate_rules
from ..ingestion import feeds, mailbox, service as ing
from ..ingestion.statements import StatementError, _SYN, suggest_profile
from ..models import BankAccount, ExchangeRate, LegalEntity, RemittanceDocument, User
from ..posting import mapped
from ..security import require
from . import serializers as S
from .deps import entity_for, get_or_404, guard
from .routes_core import _read

router = APIRouter()
PROFILE_KEYS = {"format", "delimiter", "encoding", "skip_rows", "columns", "date_format", "currency", "invert_direction", "narration_rules", "extra_narration_columns"}


def validate_profile(p: dict) -> list[str]:
    errs = []
    if not isinstance(p, dict):
        return ["profile must be an object"]
    errs += [f"unknown key '{k}'" for k in p if k not in PROFILE_KEYS]
    if p.get("format", "auto") not in ("auto", "csv", "xlsx", "mt940", "camt053"):
        errs.append("format must be auto|csv|xlsx|mt940|camt053")
    if p.get("delimiter") and len(p["delimiter"]) != 1:
        errs.append("delimiter must be a single character")
    errs += [f"columns: unknown field '{k}'" for k in (p.get("columns") or {}) if k not in _SYN]
    if p.get("date_format"):
        try:
            datetime(2026, 1, 31).strftime(p["date_format"])
        except Exception:
            errs.append("date_format is not a valid strptime pattern")
    if p.get("currency") and (len(p["currency"]) != 3 or not p["currency"].isalpha()):
        errs.append("currency must be a 3-letter code")
    if p.get("skip_rows") is not None and not (isinstance(p["skip_rows"], int) and 0 <= p["skip_rows"] <= 200):
        errs.append("skip_rows must be 0-200")
    errs += validate_rules(p.get("narration_rules") or [])
    return errs


def _bank(db, user, bid) -> BankAccount:
    b = get_or_404(db, BankAccount, bid)
    entity_for(db, user, b.entity_id)
    return b


# ------------------------------------------------------------------------------------------------ bank profile / feed
@router.get("/bank-accounts/{bid}/setup")
def bank_setup(bid: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    b = _bank(db, user, bid)
    return {"profile": b.profile or {}, "feed": b.feed or {}, "feed_types": [{"type": k, "label": v[0], "status": v[1]} for k, v in feeds.FEED_TYPES.items()],
            "currency": b.currency}


class ProfileIn(BaseModel):
    profile: dict


@router.put("/bank-accounts/{bid}/profile")
def set_profile(bid: int, body: ProfileIn, user: User = Depends(require("configure", "ingest")), db: Session = Depends(get_db)):
    b = _bank(db, user, bid)
    errs = validate_profile(body.profile)
    if errs:
        raise HTTPException(422, "; ".join(errs))
    b.profile = body.profile
    audit.log(db, user.name, "bank.profile_changed", "BankAccount", b.id, b.entity_id, {"keys": sorted(body.profile)}, user.id)
    return {"ok": True}


class FeedIn(BaseModel):
    feed: dict


@router.put("/bank-accounts/{bid}/feed")
def set_feed(bid: int, body: FeedIn, user: User = Depends(require("configure")), db: Session = Depends(get_db)):
    b = _bank(db, user, bid)
    errs = feeds.validate_feed(body.feed)
    if errs:
        raise HTTPException(422, "; ".join(errs))
    if body.feed.get("url") and not egress_allowed(body.feed["url"].split("/")[2]):
        raise HTTPException(422, "Host is not in ALLOWED_EGRESS_HOSTS (data-residency strict mode)")
    b.feed = body.feed
    audit.log(db, user.name, "bank.feed_changed", "BankAccount", b.id, b.entity_id, {"type": body.feed.get("type")}, user.id)
    return {"ok": True}


@router.post("/bank-accounts/{bid}/sync")
def sync(bid: int, user: User = Depends(require("ingest")), db: Session = Depends(get_db)):
    b = _bank(db, user, bid)
    try:
        return feeds.sync_bank(db, b, user.name, user.id)
    except (feeds.FeedError, EgressBlocked, StatementError) as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, f"Feed failed: {e}")


@router.post("/ingest/preview")
async def preview(bank_account_id: int = Form(...), profile_json: str = Form(None), file: UploadFile = File(...),
                  user: User = Depends(require("ingest", "configure")), db: Session = Depends(get_db)):
    """Dry run with an unsaved profile: shows rows, direction, amounts and what the narration parser extracted. Writes nothing."""
    b = _bank(db, user, bank_account_id)
    data = await _read(file)
    prof = None
    if profile_json:
        try:
            prof = json.loads(profile_json)
        except json.JSONDecodeError as e:
            raise HTTPException(422, f"profile is not valid JSON: {e}")
        errs = validate_profile(prof)
        if errs:
            raise HTTPException(422, "; ".join(errs))
    try:
        out = ing.preview_statement(b, file.filename or "", data, prof)
        out["suggested"] = suggest_profile(data, file.filename or "") if (file.filename or "").lower().endswith((".csv", ".txt", ".xlsx", ".xlsm")) else None
        return out
    except StatementError as e:
        sug = None
        try:
            sug = suggest_profile(data, file.filename or "")
        except StatementError:
            pass
        raise HTTPException(422, f"{e}" + (f" — suggested mapping: {json.dumps(sug['profile'])}" if sug and sug.get("profile") else ""))


# ------------------------------------------------------------------------------------------------ ERP + mailbox config
@router.get("/entities/{eid}/integrations")
def get_integrations(eid: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    e = entity_for(db, user, eid)
    cfg = (e.config or {}).get("integrations") or {}
    return {"erp_export": cfg.get("erp_export") or {}, "erp_import": cfg.get("erp_import") or {}, "mailbox": cfg.get("mailbox") or {},
            "connectors": mapped.available(e), "ledgers": e.ledgers, "base_currency": e.base_currency,
            "residency": {"strict": config.settings.residency_strict, "region": config.settings.deploy_region or None,
                          "allowed_egress": list(config.settings.allowed_egress), "indian_region": config.settings.deploy_region in INDIA_REGIONS}}


class IntegrationsIn(BaseModel):
    erp_export: dict | None = None
    erp_import: dict | None = None
    mailbox: dict | None = None
    ledgers: dict[str, str] | None = Field(None, description="logical ledger -> ERP ledger code")


@router.put("/entities/{eid}/integrations")
def set_integrations(eid: int, body: IntegrationsIn, user: User = Depends(require("configure")), db: Session = Depends(get_db)):
    e = entity_for(db, user, eid)
    errs = []
    if body.erp_export is not None:
        errs += mapped.validate_export(body.erp_export)
        if body.erp_export.get("url") and not egress_allowed(body.erp_export["url"].split("/")[2]):
            errs.append("webhook host is not in ALLOWED_EGRESS_HOSTS (data-residency strict mode)")
    if body.mailbox is not None:
        errs += mailbox.validate_mailbox(body.mailbox)
        if body.mailbox.get("host") and not egress_allowed(body.mailbox["host"]):
            errs.append("mailbox host is not in ALLOWED_EGRESS_HOSTS (data-residency strict mode)")
    if body.erp_import is not None and body.erp_import.get("columns") is not None:
        from ..ingestion.service import _COLS

        errs += [f"erp_import.columns: unknown field '{k}'" for k in body.erp_import["columns"] if k not in _COLS]
    if errs:
        raise HTTPException(422, "; ".join(errs))
    cfg = dict((e.config or {}).get("integrations") or {})
    for k in ("erp_export", "erp_import", "mailbox"):
        v = getattr(body, k)
        if v is not None:
            cfg[k] = v
    e.config = {**(e.config or {}), "integrations": cfg}
    if body.ledgers is not None:
        e.ledgers = {**(e.ledgers or {}), **{k: v for k, v in body.ledgers.items() if v}}
    audit.log(db, user.name, "integrations.changed", "LegalEntity", e.id, e.id,
              {"sections": [k for k in ("erp_export", "erp_import", "mailbox") if getattr(body, k) is not None], "ledgers": body.ledgers is not None}, user.id)
    return {"ok": True}


@router.post("/ingest/open-items/preview")
async def open_items_preview(entity_id: int = Form(...), mapping_json: str = Form(None), file: UploadFile = File(...),
                             user: User = Depends(require("ingest", "configure")), db: Session = Depends(get_db)):
    e = entity_for(db, user, entity_id)
    mapping = json.loads(mapping_json) if mapping_json else ((e.config or {}).get("integrations") or {}).get("erp_import")
    try:
        rows = ing.csv_to_open_items(await _read(file), mapping, file.filename or "")
    except (ValueError, json.JSONDecodeError) as ex:
        raise HTTPException(422, str(ex))
    return {"rows": len(rows), "sample": rows[:10]}


# ------------------------------------------------------------------------------------------------ mailbox
@router.post("/entities/{eid}/mailbox/poll")
def poll(eid: int, user: User = Depends(require("ingest")), db: Session = Depends(get_db)):
    e = entity_for(db, user, eid)
    cfg = ((e.config or {}).get("integrations") or {}).get("mailbox") or {}
    if not cfg:
        raise HTTPException(409, "No mailbox configured")
    try:
        return mailbox.poll_mailbox(db, e.id, cfg, user.name, user.id)
    except (mailbox.MailboxError, EgressBlocked) as ex:
        raise HTTPException(409, str(ex))
    except Exception as ex:
        raise HTTPException(502, f"Mailbox poll failed: {ex}")


@router.post("/ingest/email")
async def ingest_email(entity_id: int = Form(...), file: UploadFile = File(...), user: User = Depends(require("ingest")), db: Session = Depends(get_db)):
    """For inbound-mail webhooks / manual drag-and-drop of an .eml. Same security policy as the IMAP poller."""
    e = entity_for(db, user, entity_id)
    cfg = ((e.config or {}).get("integrations") or {}).get("mailbox") or {}
    return mailbox.process_email(db, e.id, await _read(file), cfg, user.name, user.id)


@router.get("/remittances")
def remittances(entity_id: int, status: str = "", user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    q = select(RemittanceDocument).where(RemittanceDocument.entity_id == entity_id)
    if status == "quarantined":
        q = q.where(RemittanceDocument.extractor == "quarantined")
    rows = db.execute(q.order_by(RemittanceDocument.id.desc()).limit(200)).scalars().all()
    return [{"id": r.id, "source": r.source, "subject": r.filename, "sender": r.sender, "auth_result": r.auth_result, "extractor": r.extractor,
             "linked_txn_id": r.linked_txn_id, "lines": len((r.extracted or {}).get("lines", [])), "reasons": (r.extracted or {}).get("reasons"),
             "created_at": S.d(r.created_at), "preview": r.raw_text[:300]} for r in rows]


@router.post("/remittances/{rid}/release")
def release(rid: int, user: User = Depends(require("match")), db: Session = Depends(get_db)):
    r = get_or_404(db, RemittanceDocument, rid)
    guard(user, r)
    try:
        d = mailbox.release_quarantined(db, r, user.name, user.id)
    except mailbox.MailboxError as e:
        raise HTTPException(409, str(e))
    return {"id": d.id, "linked_txn_id": d.linked_txn_id, "lines": len(d.extracted.get("lines", []))}


# ------------------------------------------------------------------------------------------------ FX rates
class RateIn(BaseModel):
    currency: str = Field(..., min_length=3, max_length=3)
    rate_date: date
    rate: str = Field(..., description="base currency per 1 unit, e.g. 83.4521")
    source: str = "manual"


@router.get("/fx-rates")
def list_rates(currency: str = "", limit: int = 100, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    q = select(ExchangeRate).order_by(ExchangeRate.rate_date.desc(), ExchangeRate.currency).limit(min(limit, 500))
    if currency:
        q = q.where(ExchangeRate.currency == currency.upper())
    return [{"id": r.id, "currency": r.currency, "rate_date": r.rate_date.isoformat(), "rate": r.rate_micro / 1e6, "source": r.source} for r in db.execute(q).scalars()]


@router.post("/fx-rates")
def upsert_rates(body: list[RateIn], user: User = Depends(require("fx")), db: Session = Depends(get_db)):
    n = 0
    for r in body:
        try:
            micro = fx.parse_rate(r.rate)
        except Exception:
            raise HTTPException(422, f"Invalid rate '{r.rate}' for {r.currency}")
        if not (1_000 <= micro <= 100_000_000_000):
            raise HTTPException(422, f"Rate {r.rate} for {r.currency} is outside the plausible range")
        cur = r.currency.upper()
        row = db.execute(select(ExchangeRate).where(ExchangeRate.currency == cur, ExchangeRate.rate_date == r.rate_date)).scalar_one_or_none()
        if row:
            row.rate_micro, row.source = micro, r.source
        else:
            db.add(ExchangeRate(currency=cur, rate_date=r.rate_date, rate_micro=micro, source=r.source))
        n += 1
    audit.log(db, user.name, "fx.rates_upserted", "ExchangeRate", "-", None, {"count": n, "currencies": sorted({r.currency.upper() for r in body})}, user.id)
    return {"upserted": n}
