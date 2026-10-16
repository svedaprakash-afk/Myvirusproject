"""Auth, master data, ingestion."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..ingestion import service as ing
from ..ingestion.statements import StatementError
from ..models import (BankAccount, Customer, CustomerIdentifier, CustomerProfile, DEFAULT_ENTITY_CONFIG, Invoice, LegalEntity, ToleranceRule, User)
from ..posting.connectors import REGISTRY
from ..security import can_see_pii, current_user, make_token, require, verify_password
from . import serializers as S
from .deps import entity_for, get_or_404, guard, page

router = APIRouter()


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    u = db.execute(select(User).where(User.email == body.email.lower().strip())).scalar_one_or_none()
    if not u or not u.active or not verify_password(body.password, u.password_hash):
        audit.log(db, body.email[:100], "auth.failed", "User", u.id if u else "-", None, {})
        db.commit()
        raise HTTPException(401, "Invalid credentials")
    audit.log(db, u.name, "auth.login", "User", u.id, None, {}, u.id)
    return {"token": make_token(u), "user": {"id": u.id, "name": u.name, "email": u.email, "role": u.role.value}}


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role.value, "entity_ids": user.entity_ids}


@router.get("/connectors")
def connectors(entity_id: int | None = None, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    from ..posting import mapped

    e = entity_for(db, user, entity_id) if entity_id else None
    return mapped.available(e) if e else [{"name": c.name, "label": c.label, "status": c.status} for c in REGISTRY.values()]


@router.get("/entities")
def entities(user: User = Depends(require("view")), db: Session = Depends(get_db)):
    rows = db.execute(select(LegalEntity).order_by(LegalEntity.id)).scalars().all()
    return [{"id": e.id, "name": e.name, "gstin": e.gstin, "pan": e.pan, "base_currency": e.base_currency}
            for e in rows if not user.entity_ids or e.id in user.entity_ids]


@router.get("/entities/{entity_id}/config")
def get_config(entity_id: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    e = entity_for(db, user, entity_id)
    tol = db.execute(select(ToleranceRule).where(ToleranceRule.entity_id == e.id)).scalars().all()
    return {"config": {**DEFAULT_ENTITY_CONFIG, **(e.config or {})}, "ledgers": e.ledgers,
            "tolerances": [{"id": t.id, "customer_id": t.customer_id, "currency": t.currency, "abs_minor": t.abs_minor, "pct_bp": t.pct_bp} for t in tol]}


class ConfigIn(BaseModel):
    auto_approve_threshold: float | None = Field(None, ge=0.5, le=0.999)
    review_threshold: float | None = Field(None, ge=0.05, le=0.9)
    auto_approve_max_minor: int | None = Field(None, ge=0)
    auto_approve_enabled: bool | None = None
    auto_approve_amount_only: bool | None = None
    auto_approve_cross_currency: bool | None = None
    fx_spread_bp: int | None = Field(None, ge=10, le=1000)
    foreign_bank_charge_max_base_minor: int | None = Field(None, ge=0, le=5_000_000)
    straight_through_release: bool | None = None
    sla_hours: int | None = Field(None, ge=1, le=720)
    max_subset_size: int | None = Field(None, ge=2, le=12)
    default_tolerance_minor: int | None = Field(None, ge=0, le=100_000)


@router.put("/entities/{entity_id}/config")
def set_config(entity_id: int, body: ConfigIn, user: User = Depends(require("configure")), db: Session = Depends(get_db)):
    e = entity_for(db, user, entity_id)
    changes = body.model_dump(exclude_none=True)
    tol = changes.pop("default_tolerance_minor", None)
    before = {**DEFAULT_ENTITY_CONFIG, **(e.config or {})}
    e.config = {**before, **changes}
    if tol is not None:
        r = db.execute(select(ToleranceRule).where(ToleranceRule.entity_id == e.id, ToleranceRule.customer_id.is_(None), ToleranceRule.currency.is_(None))).scalar_one_or_none()
        if r:
            r.abs_minor = tol
        else:
            db.add(ToleranceRule(entity_id=e.id, abs_minor=tol))
        changes["default_tolerance_minor"] = tol
    audit.log(db, user.name, "config.changed", "LegalEntity", e.id, e.id, {"changes": changes, "before": {k: before.get(k) for k in changes}}, user.id)
    return {"ok": True, "config": e.config}


@router.get("/entities/{entity_id}/bank-accounts")
def bank_accounts(entity_id: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    e = entity_for(db, user, entity_id)
    return [S.bank(b, can_see_pii(user)) for b in db.execute(select(BankAccount).where(BankAccount.entity_id == e.id)).scalars()]


# ---------------------------------------------------------------------------------------------- customers & invoices
@router.get("/customers")
def customers(entity_id: int, q: str = "", limit: int = 50, offset: int = 0, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    limit, offset = page(limit, offset)
    base = select(Customer).where(Customer.entity_id == entity_id)
    if q:
        base = base.where(or_(Customer.name.ilike(f"%{q}%"), Customer.code.ilike(f"%{q}%"), Customer.gstin.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    rows = db.execute(base.order_by(Customer.name).limit(limit).offset(offset)).scalars().all()
    opens = dict(db.execute(select(Invoice.customer_id, func.sum(Invoice.open_minor)).where(Invoice.entity_id == entity_id).group_by(Invoice.customer_id)).all())
    return {"total": total, "items": [{**S.customer(c, brief=False), "open_minor": int(opens.get(c.id) or 0)} for c in rows]}


@router.get("/customers/{cid}")
def customer_detail(cid: int, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    c = get_or_404(db, Customer, cid)
    guard(user, c)
    prof = db.get(CustomerProfile, c.id)
    idents = db.execute(select(CustomerIdentifier).where(CustomerIdentifier.customer_id == c.id).order_by(CustomerIdentifier.hits.desc())).scalars().all()
    invs = db.execute(select(Invoice).where(Invoice.customer_id == c.id, Invoice.open_minor != 0).order_by(Invoice.due_date)).scalars().all()
    return {**S.customer(c, brief=False),
            "profile": None if not prof else {"payments": prof.payments, "multi_invoice_payments": prof.multi_invoice_payments, "tds_payments": prof.tds_payments,
                                              "avg_days_to_pay": round(prof.avg_days_to_pay, 1), "avg_days_late": round(prof.avg_days_late, 1),
                                              "last_paid": S.d(prof.last_paid)},
            "identifiers": [{"kind": i.kind, "value": i.value, "hits": i.hits, "source": i.source} for i in idents],
            "open_invoices": [S.invoice(i) for i in invs]}


class CustomerPatch(BaseModel):
    tier: str | None = Field(None, pattern="^[ABC]$")
    tds_rate_bp: int | None = Field(None, ge=0, le=3000)
    tds_section: str | None = None
    tds_base: str | None = Field(None, pattern="^(taxable|gross)$")
    virtual_account_no: str | None = None
    payment_terms_days: int | None = Field(None, ge=0, le=365)


@router.patch("/customers/{cid}")
def patch_customer(cid: int, body: CustomerPatch, user: User = Depends(require("configure", "match")), db: Session = Depends(get_db)):
    c = get_or_404(db, Customer, cid)
    guard(user, c)
    ch = body.model_dump(exclude_none=True)
    before = {k: getattr(c, k) for k in ch}
    for k, v in ch.items():
        setattr(c, k, v or None if k in ("tds_rate_bp", "virtual_account_no", "tds_section") else v)
    audit.log(db, user.name, "customer.updated", "Customer", c.id, c.entity_id, {"before": before, "after": ch}, user.id)
    return S.customer(c, brief=False)


@router.get("/invoices")
def invoices(entity_id: int, status: str = "", customer_id: int | None = None, q: str = "", limit: int = 100, offset: int = 0,
             user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    limit, offset = page(limit, offset)
    base = select(Invoice).where(Invoice.entity_id == entity_id)
    if customer_id:
        base = base.where(Invoice.customer_id == customer_id)
    if q:
        base = base.where(Invoice.number.ilike(f"%{q}%"))
    if status == "open":
        base = base.where(Invoice.open_minor != 0)
    elif status == "paid":
        base = base.where(Invoice.open_minor == 0)
    elif status == "disputed":
        base = base.where(Invoice.disputed.is_(True))
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    rows = db.execute(base.order_by(Invoice.due_date, Invoice.id).limit(limit).offset(offset)).scalars().all()
    return {"total": total, "items": [S.invoice(i) for i in rows]}


# ---------------------------------------------------------------------------------------------- ingestion
MAX_UPLOAD = 25 * 1024 * 1024


async def _read(f: UploadFile) -> bytes:
    data = await f.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large (25 MB limit)")
    if not data:
        raise HTTPException(400, "File is empty")
    return data


@router.post("/ingest/bank-statement")
async def ingest_statement(bank_account_id: int = Form(...), gateway: bool = Form(False), file: UploadFile = File(...),
                           user: User = Depends(require("ingest")), db: Session = Depends(get_db)):
    bank = get_or_404(db, BankAccount, bank_account_id)
    entity_for(db, user, bank.entity_id)
    data = await _read(file)
    try:
        return ing.ingest_statement(db, bank, file.filename or "statement", data, user.name, user.id, gateway=gateway or bank.is_gateway)
    except StatementError as e:
        raise HTTPException(422, str(e))


@router.post("/ingest/open-items")
async def ingest_open_items(entity_id: int = Form(...), full_snapshot: bool = Form(False), file: UploadFile = File(None), rows_json: str = Form(None),
                            user: User = Depends(require("ingest")), db: Session = Depends(get_db)):
    e = entity_for(db, user, entity_id)
    try:
        if file is not None:
            rows = ing.csv_to_open_items(await _read(file), ((e.config or {}).get("integrations") or {}).get("erp_import"), file.filename or "")
        elif rows_json:
            rows = json.loads(rows_json)
        else:
            raise HTTPException(400, "Provide a CSV file or rows_json")
    except (ValueError, json.JSONDecodeError) as ex:
        raise HTTPException(422, str(ex))
    return ing.import_open_items(db, e, rows, user.name, user.id, full_snapshot=full_snapshot)


@router.post("/ingest/remittance")
async def ingest_remittance(entity_id: int = Form(...), text: str = Form(None), source: str = Form("manual"), file: UploadFile = File(None),
                            user: User = Depends(require("ingest")), db: Session = Depends(get_db)):
    from ..ingestion.remittance import text_from_upload

    entity_for(db, user, entity_id)
    fname = None
    if file is not None:
        fname = file.filename
        try:
            text = text_from_upload(fname or "", await _read(file))
        except Exception as ex:
            raise HTTPException(422, f"Could not read file: {ex}")
    if not text or not text.strip():
        raise HTTPException(400, "Provide remittance text or a file")
    doc = ing.ingest_remittance(db, entity_id, text, fname, source, user.name, user.id)
    return {"id": doc.id, "extractor": doc.extractor, "extracted": doc.extracted, "linked_txn_id": doc.linked_txn_id}
