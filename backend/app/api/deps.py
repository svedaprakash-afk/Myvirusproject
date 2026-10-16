from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import LegalEntity, User
from ..security import current_user, entity_scope


def entity_for(db: Session, user: User, entity_id: int) -> LegalEntity:
    e = db.get(LegalEntity, entity_id)
    if not e:
        raise HTTPException(404, "Legal entity not found")
    entity_scope(user, e.id)
    return e


def page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, 500)), max(0, offset)


def get_or_404(db: Session, model, id_: int, entity_id: int | None = None):
    obj = db.get(model, id_)
    if not obj or (entity_id is not None and getattr(obj, "entity_id", entity_id) != entity_id):
        raise HTTPException(404, f"{model.__name__} not found")
    return obj


def guard(user: User, obj) -> None:
    eid = getattr(obj, "entity_id", None)
    if eid is not None:
        entity_scope(user, eid)
