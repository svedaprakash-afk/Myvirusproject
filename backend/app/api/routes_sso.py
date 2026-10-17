"""SSO endpoints (OIDC) and SCIM 2.0 user provisioning."""
from __future__ import annotations

import hmac
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, config, sso
from ..db import get_db
from ..models import Role, User
from ..security import make_token

router = APIRouter()
scim = APIRouter(prefix="/scim/v2")
COOKIE = "sso_tx"


@router.get("/auth/sso/config")
def sso_config():
    s = config.settings
    return {"enabled": sso.enabled(), "enforce": bool(sso.enabled() and s.sso_enforce), "label": "Sign in with your company account"}


@router.get("/auth/sso/login")
def sso_login():
    if not sso.enabled():
        raise HTTPException(404, "Single sign-on is not configured")
    try:
        url, tx = sso.begin()
    except (sso.SsoError, Exception) as e:
        raise HTTPException(502, f"Could not reach the identity provider: {e}")
    r = RedirectResponse(url, status_code=302)
    r.set_cookie(COOKIE, tx, max_age=sso.TX_TTL, httponly=True, samesite="lax", secure=config.settings.sso_redirect_uri.startswith("https://"), path="/api/v1/auth/sso")
    return r


def _back(frag: str) -> RedirectResponse:
    r = RedirectResponse(f"{config.settings.sso_frontend_url}#{frag}", status_code=302)
    r.delete_cookie(COOKIE, path="/api/v1/auth/sso")
    return r


@router.get("/auth/sso/callback")
def sso_callback(request: Request, code: str = "", state: str = "", error: str = "", db: Session = Depends(get_db)):
    if not sso.enabled():
        raise HTTPException(404, "Single sign-on is not configured")
    try:
        if error:
            raise sso.SsoError(f"The identity provider reported: {error}")
        claims = sso.complete(code, state, request.cookies.get(COOKIE))
        email = sso.email_of(claims)
        role = sso.pick_role(claims)
        u = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if u is None:
            jit = config.settings.sso_jit_role
            if not jit:
                raise sso.SsoError("Your account has not been set up in this application. Ask an administrator.", 403)
            u = sso.new_user(email, claims, role or jit)
            db.add(u)
            db.flush()
            audit.log(db, "sso", "user.created_jit", "User", u.id, None, {"email": email, "role": u.role.value}, None)
        elif not u.active:
            raise sso.SsoError("Your account is disabled.", 403)
        elif role and role != u.role.value:
            audit.log(db, "sso", "user.role_synced", "User", u.id, None, {"from": u.role.value, "to": role}, None)
            u.role = Role(role)
        audit.log(db, u.name, "auth.sso_login", "User", u.id, None, {}, u.id)
        db.commit()
        return _back(f"sso_token={quote(make_token(u))}")
    except sso.SsoError as e:
        audit.log(db, "sso", "auth.sso_failed", "User", "-", None, {"reason": str(e)[:200]}, None)
        db.commit()
        return _back(f"sso_error={quote(str(e))}")


# ================================================================================================ SCIM 2.0
SCIM_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_ERR = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
MEDIA = "application/scim+json"


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None):
        self.status, self.detail, self.scim_type = status, detail, scim_type


def _reply(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=MEDIA)


def scim_auth(authorization: str = Header("")):
    tok = config.settings.scim_token
    if len(tok) < 24:
        raise ScimError(404, "SCIM is not enabled (SCIM_TOKEN must be set to at least 24 characters)")
    given = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(given.encode(), tok.encode()):
        raise ScimError(401, "Invalid bearer token")


def _resource(u: User) -> dict:
    return {"schemas": [SCIM_USER], "id": str(u.id), "userName": u.email, "displayName": u.name, "name": {"formatted": u.name}, "active": bool(u.active),
            "emails": [{"value": u.email, "primary": True}], "roles": [{"value": u.role.value, "primary": True}], "meta": {"resourceType": "User"}}


def _truthy(v) -> bool:
    return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes")


def _role_from(body: dict, default: str | None) -> str | None:
    roles = body.get("roles") or []
    val = (roles[0].get("value") if roles and isinstance(roles[0], dict) else None) or default
    if val is None:
        return None
    if val not in {r.value for r in Role}:
        raise ScimError(400, f"Unknown role '{val}'", "invalidValue")
    if val not in config.settings.scim_allowed_roles:
        raise ScimError(403, f"Role '{val}' cannot be assigned through SCIM (allowed: {', '.join(config.settings.scim_allowed_roles)})")
    return val


def _name(body: dict, fallback: str) -> str:
    n = body.get("displayName") or (body.get("name") or {}).get("formatted")
    if not n and body.get("name"):
        n = " ".join(filter(None, [body["name"].get("givenName"), body["name"].get("familyName")]))
    return (n or fallback)[:200]


def _email(body: dict) -> str:
    e = (body.get("userName") or "").lower().strip()
    if "@" not in e:
        raise ScimError(400, "userName must be an email address", "invalidValue")
    return e


@scim.get("/ServiceProviderConfig")
def sp_config(_=Depends(scim_auth)):
    return _reply({"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"], "patch": {"supported": True}, "bulk": {"supported": False},
                   "filter": {"supported": True, "maxResults": 200}, "changePassword": {"supported": False}, "sort": {"supported": False}, "etag": {"supported": False},
                   "authenticationSchemes": [{"type": "oauthbearertoken", "name": "Bearer token", "description": "Static bearer token"}]})


@scim.get("/Users")
def users_list(filter: str = "", startIndex: int = 1, count: int = 100, db: Session = Depends(get_db), _=Depends(scim_auth)):
    q = select(User).order_by(User.id)
    if filter:
        m = re.fullmatch(r'\s*userName\s+eq\s+"([^"]+)"\s*', filter, re.I)
        if not m:
            raise ScimError(400, 'Only the filter userName eq "value" is supported', "invalidFilter")
        q = q.where(User.email == m.group(1).lower())
    rows = db.execute(q).scalars().all()
    start, count = max(startIndex, 1), max(0, min(count, 200))
    page = rows[start - 1: start - 1 + count]
    return _reply({"schemas": [SCIM_LIST], "totalResults": len(rows), "startIndex": start, "itemsPerPage": len(page), "Resources": [_resource(u) for u in page]})


@scim.post("/Users")
def user_create(body: dict, db: Session = Depends(get_db), _=Depends(scim_auth)):
    email = _email(body)
    if db.execute(select(User).where(User.email == email)).first():
        raise ScimError(409, f"{email} already exists", "uniqueness")
    role = _role_from(body, config.settings.scim_default_role)
    u = sso.new_user(email, {"name": _name(body, email.split("@")[0])}, role)
    u.active = _truthy(body.get("active", True))
    db.add(u)
    db.flush()
    audit.log(db, "scim", "user.provisioned", "User", u.id, None, {"email": email, "role": role, "active": u.active}, None)
    return _reply(_resource(u), 201)


def _get(db: Session, uid: str) -> User:
    u = db.get(User, int(uid)) if uid.isdigit() else None
    if not u:
        raise ScimError(404, "User not found")
    return u


@scim.get("/Users/{uid}")
def user_get(uid: str, db: Session = Depends(get_db), _=Depends(scim_auth)):
    return _reply(_resource(_get(db, uid)))


def _apply(db: Session, u: User, body: dict, replace_all: bool) -> None:
    before = {"role": u.role.value, "active": u.active, "name": u.name}
    if "userName" in body and _email(body) != u.email:
        raise ScimError(400, "userName cannot be changed", "mutability")
    if "active" in body:
        u.active = _truthy(body["active"])
    if "roles" in body or replace_all:
        r = _role_from(body, None)
        if r:
            u.role = Role(r)
    if "displayName" in body or "name" in body:
        u.name = _name(body, u.name)
    audit.log(db, "scim", "user.updated", "User", u.id, None, {"before": before, "after": {"role": u.role.value, "active": u.active, "name": u.name}}, None)


@scim.put("/Users/{uid}")
def user_put(uid: str, body: dict, db: Session = Depends(get_db), _=Depends(scim_auth)):
    u = _get(db, uid)
    _apply(db, u, body, replace_all=True)
    return _reply(_resource(u))


@scim.patch("/Users/{uid}")
def user_patch(uid: str, body: dict, db: Session = Depends(get_db), _=Depends(scim_auth)):
    u = _get(db, uid)
    flat: dict = {}
    for op in body.get("Operations") or []:
        verb = str(op.get("op", "")).lower()
        if verb not in ("add", "replace", "remove"):
            raise ScimError(400, f"Unsupported operation '{verb}'", "invalidSyntax")
        path, val = op.get("path"), op.get("value")
        if verb == "remove":
            if path == "active":
                flat["active"] = False
            continue
        if path is None and isinstance(val, dict):       # Entra ID style: value carries the attributes
            flat.update(val)
        elif path == "active":
            flat["active"] = val
        elif path in ("displayName", "name.formatted"):
            flat["displayName"] = val
        elif path and path.startswith("roles"):
            flat["roles"] = val if isinstance(val, list) else [{"value": val}]
    _apply(db, u, flat, replace_all=False)
    return _reply(_resource(u))


@scim.delete("/Users/{uid}")
def user_delete(uid: str, db: Session = Depends(get_db), _=Depends(scim_auth)):
    """Deprovisioning disables the account (audit records keep pointing at it); sign-in and any live token stop working at the next request."""
    u = _get(db, uid)
    u.active = False
    audit.log(db, "scim", "user.deprovisioned", "User", u.id, None, {"email": u.email}, None)
    return JSONResponse(None, status_code=204)
