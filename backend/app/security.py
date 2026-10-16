"""AuthN (PBKDF2 passwords + JWT) and AuthZ (role -> permission matrix, entity scoping, segregation of duties)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import timedelta
from typing import Callable

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import Role, User, utcnow

ITER = int(os.getenv("PBKDF2_ITERATIONS", "200000"))  # lowered only by the test-suite


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITER)
    return f"pbkdf2${ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def make_token(user: User) -> str:
    payload = {"sub": str(user.id), "role": user.role.value, "exp": utcnow() + timedelta(minutes=settings.jwt_ttl_minutes)}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


# Permission matrix. Segregation of duties: nobody except admin-for-config holds both
# "record/ingest" and "approve" authority, and approvals additionally check requester != approver.
P = {
    "view": {Role.admin, Role.ar_analyst, Role.ar_manager, Role.treasury, Role.finance_head, Role.auditor},
    "view_pii": {Role.treasury, Role.finance_head, Role.auditor, Role.ar_manager},
    "ingest": {Role.ar_analyst, Role.ar_manager, Role.treasury},
    "match": {Role.ar_analyst, Role.ar_manager},
    "request_adjustment": {Role.ar_analyst, Role.ar_manager},
    "approve": {Role.ar_manager, Role.finance_head},
    "build_batch": {Role.ar_manager, Role.treasury},
    "release_batch": {Role.treasury, Role.finance_head},
    "configure": {Role.admin, Role.finance_head},
    "audit": {Role.auditor, Role.finance_head, Role.admin},
    "train": {Role.admin, Role.finance_head},
    "demo": {Role.admin},
}
# Role hierarchy for approval-limit checks (higher rank may approve what lower rank can).
RANK = {Role.ar_analyst: 1, Role.ar_manager: 2, Role.treasury: 2, Role.finance_head: 3, Role.admin: 0, Role.auditor: 0}

bearer = HTTPBearer(auto_error=False)


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        data = jwt.decode(creds.credentials, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    user = db.get(User, int(data["sub"]))
    if not user or not user.active:
        raise HTTPException(401, "User disabled")
    return user


def require(*perms: str) -> Callable:
    def dep(user: User = Depends(current_user)) -> User:
        if not any(user.role in P[p] for p in perms):
            raise HTTPException(403, f"Role '{user.role.value}' lacks permission: {'/'.join(perms)}")
        return user

    return dep


def entity_scope(user: User, entity_id: int) -> None:
    if user.entity_ids and entity_id not in user.entity_ids:
        raise HTTPException(403, "No access to this legal entity")


def can_see_pii(user: User) -> bool:
    return user.role in P["view_pii"]
