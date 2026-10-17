"""OpenID Connect single sign-on: authorization code flow with PKCE, id_token verified against the provider's published keys.

Works with any compliant provider (Microsoft Entra ID, Okta, Google Workspace, Keycloak, Auth0). SAML is NOT implemented; providers that only speak
SAML can usually front an OIDC application. Written to the OIDC Core spec and exercised against a local mock provider, not against a live tenant:
run one real sign-in per provider before relying on it.

Controls: PKCE (S256) + state + nonce bound to a signed, short-lived, HttpOnly cookie; only RS256/ES256 id_tokens are accepted ('none' and HMAC are not);
issuer, audience, expiry and nonce are checked; email must be present and not explicitly unverified; unknown users are refused unless JIT creation is
configured; roles are mapped from a group claim by an operator-written map; every outbound host passes the data-residency allow-list.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

import jwt

from . import config
from .models import Role, User

_cache: dict = {"disco": None, "disco_at": 0.0, "jwks": None, "jwks_at": 0.0}
TTL = 3600
TX_TTL = 600


class SsoError(Exception):
    def __init__(self, msg: str, status: int = 401):
        super().__init__(msg)
        self.status = status


def _http(method: str, url: str, **kw):
    import httpx

    config.check_egress(url)
    r = httpx.request(method, url, timeout=15, follow_redirects=False, **kw)
    r.raise_for_status()
    return r.json()


def enabled() -> bool:
    s = config.settings
    return bool(s.sso_issuer and s.sso_client_id and s.sso_redirect_uri)


def role_map() -> dict[str, str]:
    raw = config.settings.sso_role_map
    if not raw:
        return {}
    try:
        m = json.loads(raw)
    except ValueError:
        raise SsoError("SSO_ROLE_MAP is not valid JSON", 500)
    bad = [v for v in m.values() if v not in {r.value for r in Role}]
    if bad:
        raise SsoError(f"SSO_ROLE_MAP names unknown roles: {bad}", 500)
    return m


def discovery() -> dict:
    if _cache["disco"] and time.time() - _cache["disco_at"] < TTL:
        return _cache["disco"]
    iss = config.settings.sso_issuer
    d = _http("GET", f"{iss}/.well-known/openid-configuration")
    if d.get("issuer", "").rstrip("/") != iss:
        raise SsoError("Provider metadata issuer does not match SSO_ISSUER", 502)
    for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not str(d.get(k, "")).startswith("https://"):
            raise SsoError(f"Provider metadata has no https {k}", 502)
    _cache.update(disco=d, disco_at=time.time())
    return d


def _jwks(force: bool = False) -> dict:
    if not force and _cache["jwks"] and time.time() - _cache["jwks_at"] < TTL:
        return _cache["jwks"]
    j = _http("GET", discovery()["jwks_uri"])
    _cache.update(jwks=j, jwks_at=time.time())
    return j


def reset_cache() -> None:
    _cache.update(disco=None, disco_at=0.0, jwks=None, jwks_at=0.0)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def begin() -> tuple[str, str]:
    """Returns (authorization URL, signed transaction cookie value)."""
    s = config.settings
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    tx = jwt.encode({"state": state, "nonce": nonce, "cv": verifier, "exp": int(time.time()) + TX_TTL}, s.jwt_secret, algorithm="HS256")
    q = {"response_type": "code", "client_id": s.sso_client_id, "redirect_uri": s.sso_redirect_uri, "scope": s.sso_scopes, "state": state, "nonce": nonce,
         "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    return f"{discovery()['authorization_endpoint']}?{urlencode(q)}", tx


def _verify_id_token(tok: str, nonce: str) -> dict:
    s = config.settings
    try:
        head = jwt.get_unverified_header(tok)
    except jwt.PyJWTError:
        raise SsoError("The identity token is malformed")
    if head.get("alg") not in ("RS256", "ES256"):
        raise SsoError("The identity token uses an unsupported signing algorithm")
    key = None
    for force in (False, True):                   # one refresh handles provider key rotation
        for k in _jwks(force).get("keys", []):
            if k.get("kid") == head.get("kid"):
                key = jwt.PyJWK(k).key
                break
        if key:
            break
    if key is None:
        raise SsoError("The identity token was signed with an unknown key")
    try:
        c = jwt.decode(tok, key, algorithms=[head["alg"]], audience=s.sso_client_id, issuer=discovery()["issuer"], leeway=60,
                       options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.PyJWTError as e:
        raise SsoError(f"The identity token was rejected: {e}")
    if not hmac.compare_digest(str(c.get("nonce", "")), nonce):
        raise SsoError("The identity token does not belong to this sign-in (nonce)")
    return c


def complete(code: str, state: str, tx_cookie: str | None) -> dict:
    """Exchange the code and return verified claims. Raises SsoError on any failure."""
    s = config.settings
    if not tx_cookie:
        raise SsoError("The sign-in session expired or cookies are blocked; start again")
    try:
        tx = jwt.decode(tx_cookie, s.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise SsoError("The sign-in session expired; start again")
    if not hmac.compare_digest(tx["state"], state or ""):
        raise SsoError("Sign-in state mismatch; start again")
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": s.sso_redirect_uri, "client_id": s.sso_client_id, "code_verifier": tx["cv"]}
    if s.sso_client_secret:
        form["client_secret"] = s.sso_client_secret
    try:
        tok = _http("POST", discovery()["token_endpoint"], data=form)
    except config.EgressBlocked as e:
        raise SsoError(str(e), 502)
    except Exception as e:
        raise SsoError(f"The identity provider refused the sign-in ({type(e).__name__})")
    if not tok.get("id_token"):
        raise SsoError("The identity provider returned no identity token")
    return _verify_id_token(tok["id_token"], tx["nonce"])


def pick_role(claims: dict) -> str | None:
    m = role_map()
    groups = claims.get(config.settings.sso_group_claim) or []
    if isinstance(groups, str):
        groups = [groups]
    for g, role in m.items():          # operator order is the precedence order
        if g in groups:
            return role
    return None


def email_of(claims: dict) -> str:
    email = str(claims.get("email") or claims.get("preferred_username") or "").lower().strip()
    if "@" not in email:
        raise SsoError("The identity provider did not release an email address")
    if claims.get("email_verified") is False:
        raise SsoError("The email address is not verified at the identity provider")
    dom = config.settings.sso_allowed_domains
    if dom and email.rsplit("@", 1)[1] not in dom:
        raise SsoError("That email domain is not allowed to sign in", 403)
    return email


def new_user(email: str, claims: dict, role: str) -> User:
    from .security import hash_password

    return User(email=email, name=str(claims.get("name") or email.split("@")[0])[:200], role=Role(role), password_hash=hash_password(secrets.token_urlsafe(32)), active=True, entity_ids=[])
