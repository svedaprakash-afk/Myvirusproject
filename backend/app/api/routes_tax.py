"""Tax compliance: Form 26AS / AIS import and reconciliation, GST filing cross-checks."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..ingestion import form26as, gst
from ..models import Form26asEntry, User
from ..security import require
from . import serializers as S
from .deps import entity_for, page

router = APIRouter()
MAX_UPLOAD = 15 * 1024 * 1024


async def _read(f: UploadFile) -> bytes:
    data = await f.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large (15 MB limit)")
    if not data:
        raise HTTPException(400, "File is empty")
    return data


@router.post("/tax/26as/import")
async def import_26as(entity_id: int = Form(...), source: str = Form("26AS"), file: UploadFile = File(...),
                      user: User = Depends(require("ingest")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    try:
        rows = form26as.parse_upload(file.filename or "", await _read(file))
        return form26as.import_entries(db, entity_id, rows, source, user.name, user.id)
    except form26as.Form26asError as e:
        raise HTTPException(422, str(e))


@router.get("/tax/26as/entries")
def entries_26as(entity_id: int, fy: str = "", unmapped: bool = False, limit: int = 200, offset: int = 0,
                 user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    limit, offset = page(limit, offset)
    q = select(Form26asEntry).where(Form26asEntry.entity_id == entity_id)
    if fy:
        q = q.where(Form26asEntry.fy == fy)
    if unmapped:
        q = q.where(Form26asEntry.customer_id.is_(None))
    rows = db.execute(q.order_by(Form26asEntry.txn_date.desc(), Form26asEntry.id).limit(limit).offset(offset)).scalars().all()
    return [{"id": e.id, "source": e.source, "fy": e.fy, "quarter": e.quarter, "tan": e.deductor_tan, "deductor": e.deductor_name, "section": e.section,
             "txn_date": S.d(e.txn_date), "booking_date": S.d(e.booking_date), "status": e.booking_status, "paid_minor": e.amount_paid_minor,
             "deducted_minor": e.tax_deducted_minor, "deposited_minor": e.tds_deposited_minor, "customer_id": e.customer_id} for e in rows]


class MapIn(BaseModel):
    entity_id: int
    tan: str = Field(..., min_length=10, max_length=10)
    customer_id: int


@router.post("/tax/26as/map")
def map_26as(body: MapIn, user: User = Depends(require("match", "approve")), db: Session = Depends(get_db)):
    entity_for(db, user, body.entity_id)
    try:
        return {"entries_linked": form26as.map_deductor(db, body.entity_id, body.tan, body.customer_id, user.name, user.id)}
    except form26as.Form26asError as e:
        raise HTTPException(422, str(e))


@router.get("/tax/26as/reconciliation")
def reconciliation(entity_id: int, fy: str, source: str = "26AS", user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    return form26as.reconcile(db, entity_id, fy, source.upper())


@router.post("/tax/26as/reconciliation/apply")
def apply_reconciliation(entity_id: int, fy: str, source: str = "26AS", user: User = Depends(require("match", "approve")), db: Session = Depends(get_db)):
    """Mark our TDS entries that the statement confirms. Only ever sets the flag; it never clears one a person set."""
    entity_for(db, user, entity_id)
    from .. import audit

    r = form26as.reconcile(db, entity_id, fy, source.upper(), apply_flags=True)
    audit.log(db, user.name, "tax.26as_reconciled", "LegalEntity", entity_id, entity_id, {"fy": fy, "flagged": r["flagged"], "summary": r["summary"]}, user.id)
    return r


# ---------------------------------------------------------------------------------------------- GST cross-checks
@router.post("/tax/gst/import")
async def import_gst(entity_id: int = Form(...), source: str = Form(...), file: UploadFile = File(...),
                     user: User = Depends(require("ingest")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    source = source.upper()
    data = await _read(file)
    try:
        if source == "GSTR1":
            docs, fp = gst.parse_gstr1(data)
        elif source == "IRP":
            from ..ingestion.remittance import extract_upload

            if (file.filename or "").lower().endswith(".json"):
                raise gst.GstError("IRP JSON is not supported: export the e-invoice register as CSV or XLSX")
            docs, fp = gst.parse_irp(extract_upload(file.filename or "", data).text), None
        else:
            raise gst.GstError("source must be GSTR1 or IRP")
        out = gst.import_documents(db, entity_id, docs, source, user.name, user.id)
        return {**out, "period": fp}
    except gst.GstError as e:
        raise HTTPException(422, str(e))


@router.get("/tax/gst/crosscheck")
def gst_crosscheck(entity_id: int, include_closed: bool = False, user: User = Depends(require("view")), db: Session = Depends(get_db)):
    return gst.crosscheck(db, entity_for(db, user, entity_id), only_open=not include_closed)
