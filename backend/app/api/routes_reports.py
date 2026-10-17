"""Finance and statutory reports (JSON and CSV)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from .. import audit, reports
from ..db import get_db
from ..models import User
from ..security import require
from .deps import entity_for

router = APIRouter()


def _csv(text: str, name: str) -> Response:
    return Response(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/reports/tds-register")
def tds_register(entity_id: int, fy: str = "", quarter: str = "", customer_id: int | None = None, format: str = "json",
                 user: User = Depends(require("view")), db: Session = Depends(get_db)):
    entity_for(db, user, entity_id)
    if quarter and quarter not in ("Q1", "Q2", "Q3", "Q4"):
        raise HTTPException(400, "quarter must be Q1, Q2, Q3 or Q4")
    data = reports.tds_register(db, entity_id, fy or None, quarter or None, customer_id)
    if format == "csv":
        audit.log(db, user.name, "report.exported", "Report", "tds-register", entity_id, {"fy": fy, "quarter": quarter}, user.id)
        return _csv(reports.csv_tds_register(data), f"tds_register_{fy or 'all'}.csv")
    return data


@router.get("/reports/ageing")
def ageing(entity_id: int, as_of: date | None = None, format: str = "json", user: User = Depends(require("view")), db: Session = Depends(get_db)):
    e = entity_for(db, user, entity_id)
    if as_of and as_of > date.today():
        raise HTTPException(400, "as_of cannot be in the future")
    data = reports.ageing_provision(db, e, as_of)
    if format == "csv":
        audit.log(db, user.name, "report.exported", "Report", "ageing", entity_id, {"as_of": str(data["as_of"])}, user.id)
        return _csv(reports.csv_ageing(data), f"ageing_{data['as_of']}.csv")
    return data
