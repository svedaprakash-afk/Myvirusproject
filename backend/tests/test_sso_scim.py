"""OIDC sign-in against a local mock provider, and SCIM provisioning."""
import base64
import dataclasses
import hashlib
import json
import time
from urllib.parse import parse_qs, unquote, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app import config, sso
from app.models import AuditLog, Role, User
from app.security import hash_password
from tests.conftest import login

ISS = "https://idp.example.in/tenant"
CLIENT = "cash-app"
CALLBACK = "http://testserver/api/v1/auth/sso/callback"


class Idp:
    """Just enough of an OpenID provider: discovery, JWKS, authorization-request capture, PKCE-checking token endpoint."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.claims: dict = {}
        self.calls: list = []
        self.auth: dict = {}
        self.code_challenge = None
        self.override_token = None

    def jwk(self, kid=None):
        d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {**d, "kid": kid or self.kid, "use": "sig", "alg": "RS256"}

    def id_token(self, **over):
        now = int(time.time())
        c = {"iss": ISS, "aud": CLIENT, "sub": "u-1", "iat": now, "exp": now + 300, "nonce": self.auth.get("nonce"), "email": "ana@example.in", "name": "Ana Analyst",
             "groups": ["g-analysts"], **self.claims, **over}
        c = {k: v for k, v in c.items() if v is not None}
        return jwt.encode(c, self.key, algorithm="RS256", headers={"kid": over.get("_kid", self.kid)}) if "_kid" not in over else jwt.encode(
            {k: v for k, v in c.items() if k != "_kid"}, self.key, algorithm="RS256", headers={"kid": over["_kid"]})

    def http(self, method, url, **kw):
        self.calls.append((method, url))
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": ISS, "authorization_endpoint": f"{ISS}/authorize", "token_endpoint": f"{ISS}/token", "jwks_uri": f"{ISS}/keys"}
        if url.endswith("/keys"):
            return {"keys": [self.jwk()]}
        if url.endswith("/token"):
            f = kw["data"]
            verifier = f["code_verifier"]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if challenge != self.code_challenge:
                raise RuntimeError("PKCE verification failed at the provider")
            assert f["grant_type"] == "authorization_code" and f["client_secret"] == "s3cret" and f["redirect_uri"] == CALLBACK
            return {"id_token": self.override_token or self.id_token()}
        raise AssertionError(url)


@pytest.fixture()
def idp(monkeypatch):
    i = Idp()
    sso.reset_cache()
    monkeypatch.setattr(sso, "_http", i.http)
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, sso_issuer=ISS, sso_client_id=CLIENT, sso_client_secret="s3cret", sso_redirect_uri=CALLBACK, sso_frontend_url="/app",
        sso_role_map=json.dumps({"g-managers": "ar_manager", "g-analysts": "ar_analyst"})))
    yield i
    sso.reset_cache()


def start(client, idp):
    r = client.get("/api/v1/auth/sso/login", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == [CLIENT] and q["scope"] == ["openid email profile"] and r.headers["location"].startswith(f"{ISS}/authorize")
    idp.auth = {"nonce": q["nonce"][0], "state": q["state"][0]}
    idp.code_challenge = q["code_challenge"][0]
    return q["state"][0]


def finish(client, state, code="abc"):
    r = client.get("/api/v1/auth/sso/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    frag = loc.split("#", 1)[1]
    k, v = frag.split("=", 1)
    return k, unquote(v)


def add_user(w, email="ana@example.in", role=Role.ar_manager, active=True):
    u = User(email=email, name="Ana", role=role, password_hash=hash_password("x"), active=active)
    w.db.add(u)
    w.db.flush()
    return u


def test_config_endpoint_reflects_whether_sso_is_configured(client, idp):
    assert client.get("/api/v1/auth/sso/config").json()["enabled"] is True


def test_sign_in_for_an_existing_user_issues_an_app_token_and_syncs_the_role_from_groups(client, idp, w):
    u = add_user(w, role=Role.treasury)
    k, tok = finish(client, start(client, idp))
    assert k == "sso_token"
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()
    assert me["email"] == "ana@example.in" and me["role"] == "ar_analyst", "role follows the group map"
    actions = [a.action for a in w.db.execute(select(AuditLog)).scalars()]
    assert "user.role_synced" in actions and "auth.sso_login" in actions
    assert any(m == "POST" for m, _ in idp.calls), "the code was exchanged at the token endpoint (PKCE verified by the provider mock)"


@pytest.mark.parametrize("claims,reason", [
    ({"nonce": "someone-else"}, "nonce"),
    ({"aud": "other-app"}, "rejected"),
    ({"iss": "https://evil.example/t"}, "rejected"),
    ({"exp": int(time.time()) - 3600}, "rejected"),
    ({"email_verified": False}, "not verified"),
    ({"email": None, "preferred_username": None}, "email address"),
])
def test_bad_identity_tokens_are_refused_with_a_reason_and_audited(client, idp, w, claims, reason):
    add_user(w)
    state = start(client, idp)
    idp.override_token = idp.id_token(**claims)
    k, msg = finish(client, state)
    assert k == "sso_error" and reason in msg
    assert w.db.execute(select(AuditLog).where(AuditLog.action == "auth.sso_failed")).scalars().first() is not None


def test_tokens_with_dangerous_algorithms_or_unknown_keys_are_refused(client, idp, w):
    add_user(w)
    for forged in (jwt.encode({"iss": ISS, "aud": CLIENT, "sub": "x", "iat": int(time.time()), "exp": int(time.time()) + 99, "nonce": "n", "email": "ana@example.in"}, "s3cret", algorithm="HS256"),
                   jwt.encode({"iss": ISS, "aud": CLIENT, "sub": "x"}, None, algorithm="none")):
        state = start(client, idp)
        idp.override_token = forged
        k, msg = finish(client, state)
        assert k == "sso_error" and "unsupported signing algorithm" in msg
    state = start(client, idp)
    idp.override_token = idp.id_token(_kid="rotated-away")
    assert "unknown key" in finish(client, state)[1]


def test_key_rotation_is_handled_by_one_refresh_of_the_provider_keys(client, idp, w, monkeypatch):
    add_user(w)
    finish(client, start(client, idp))           # warms the JWKS cache with kid k1
    idp.kid = "k2"                                # the provider rotates: new kid, same private key for the mock
    k, tok = finish(client, start(client, idp))
    assert k == "sso_token"


def test_state_and_cookie_are_enforced(client, idp, w):
    add_user(w)
    start(client, idp)
    assert "state mismatch" in finish(client, "forged-state")[1]
    client.cookies.clear()
    assert "expired or cookies are blocked" in finish(client, idp.auth["state"])[1]


def test_unknown_users_are_refused_unless_jit_is_configured(client, idp, w, monkeypatch):
    k, msg = finish(client, start(client, idp))
    assert k == "sso_error" and "has not been set up" in msg and w.db.execute(select(User).where(User.email == "ana@example.in")).first() is None
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, sso_jit_role="auditor"))
    k, tok = finish(client, start(client, idp))
    u = w.db.execute(select(User).where(User.email == "ana@example.in")).scalar_one()
    assert k == "sso_token" and u.role == Role.ar_analyst, "the mapped role beats the JIT default"
    assert client.post("/api/v1/auth/login", json={"email": "ana@example.in", "password": "anything"}).status_code == 401, "JIT users have no usable password"


def test_disabled_users_and_disallowed_domains_cannot_sign_in(client, idp, w, monkeypatch):
    add_user(w, active=False)
    assert "disabled" in finish(client, start(client, idp))[1]
    w.users  # keep fixture alive
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, sso_allowed_domains=("corp.example",)))
    assert "domain is not allowed" in finish(client, start(client, idp))[1]


def test_provider_error_parameter_is_reported(client, idp):
    r = client.get("/api/v1/auth/sso/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert "access_denied" in unquote(r.headers["location"])


def test_enforcement_turns_off_password_sign_in_except_for_the_admin_break_glass(client, idp, monkeypatch, w):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, sso_enforce=True))
    assert client.get("/api/v1/auth/sso/config").json()["enforce"] is True
    r = client.post("/api/v1/auth/login", json={"email": "ar_analyst@t.example", "password": "pw-ar_analyst"})
    assert r.status_code == 403 and "single sign-on" in r.json()["detail"]
    assert client.post("/api/v1/auth/login", json={"email": "admin@t.example", "password": "pw-admin"}).status_code == 200
    assert w.db.execute(select(AuditLog).where(AuditLog.action == "auth.password_refused")).scalars().first() is not None


def test_sso_endpoints_are_404_when_not_configured(client):
    assert client.get("/api/v1/auth/sso/config").json() == {"enabled": False, "enforce": False, "label": "Sign in with your company account"}
    assert client.get("/api/v1/auth/sso/login", follow_redirects=False).status_code == 404


# ------------------------------------------------------------------------------------------------ SCIM
TOKEN = "scim-token-0123456789-abcdefghij"


@pytest.fixture()
def scim(client, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, scim_token=TOKEN))
    h = {"Authorization": f"Bearer {TOKEN}"}
    return client, h


def test_scim_requires_the_bearer_token_and_is_off_without_one(client, monkeypatch):
    assert client.get("/scim/v2/Users").status_code == 404
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, scim_token=TOKEN))
    assert client.get("/scim/v2/Users").status_code == 401
    assert client.get("/scim/v2/Users", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/scim/v2/Users", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/scim+json")


def test_scim_create_defaults_to_the_least_privileged_role_and_refuses_roles_outside_the_allow_list(scim, w):
    c, h = scim
    r = c.post("/scim/v2/Users", json={"userName": "New.Person@Example.in", "displayName": "New Person", "active": True}, headers=h)
    assert r.status_code == 201 and r.json()["userName"] == "new.person@example.in" and r.json()["roles"][0]["value"] == "auditor"
    ok = c.post("/scim/v2/Users", json={"userName": "mgr@example.in", "roles": [{"value": "ar_manager"}]}, headers=h)
    assert ok.status_code == 201 and ok.json()["roles"][0]["value"] == "ar_manager"
    bad = c.post("/scim/v2/Users", json={"userName": "cfo@example.in", "roles": [{"value": "finance_head"}]}, headers=h)
    assert bad.status_code == 403 and bad.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"] and "cannot be assigned through SCIM" in bad.json()["detail"]
    assert c.post("/scim/v2/Users", json={"userName": "x@example.in", "roles": [{"value": "wizard"}]}, headers=h).json()["scimType"] == "invalidValue"
    assert c.post("/scim/v2/Users", json={"userName": "mgr@example.in"}, headers=h).status_code == 409
    assert c.post("/scim/v2/Users", json={"userName": "not-an-email"}, headers=h).status_code == 400


def test_scim_filter_get_put_and_entra_style_patch(scim, w):
    c, h = scim
    uid = c.post("/scim/v2/Users", json={"userName": "p@example.in", "displayName": "P", "roles": [{"value": "ar_analyst"}]}, headers=h).json()["id"]
    f = c.get('/scim/v2/Users?filter=userName eq "P@example.in"', headers=h).json()
    assert f["totalResults"] == 1 and f["Resources"][0]["id"] == uid
    assert c.get('/scim/v2/Users?filter=displayName co "x"', headers=h).status_code == 400
    r = c.patch(f"/scim/v2/Users/{uid}", json={"Operations": [{"op": "Replace", "path": "active", "value": "False"}]}, headers=h)
    assert r.json()["active"] is False, "Entra ID sends booleans as strings"
    r = c.patch(f"/scim/v2/Users/{uid}", json={"Operations": [{"op": "Replace", "value": {"active": True, "displayName": "Pat"}}]}, headers=h)
    assert r.json()["active"] is True and r.json()["displayName"] == "Pat"
    assert c.put(f"/scim/v2/Users/{uid}", json={"userName": "p@example.in", "roles": [{"value": "treasury"}], "active": True}, headers=h).json()["roles"][0]["value"] == "treasury"
    assert c.put(f"/scim/v2/Users/{uid}", json={"userName": "other@example.in"}, headers=h).status_code == 400
    assert c.get("/scim/v2/Users/9999", headers=h).status_code == 404


def test_scim_delete_deactivates_and_a_live_token_stops_working_immediately(scim, w):
    c, h = scim
    u = add_user(w, "leaver@example.in", Role.ar_analyst)
    w.db.flush()
    from app.security import make_token

    tok = {"Authorization": f"Bearer {make_token(u)}"}
    assert c.get("/api/v1/auth/me", headers=tok).status_code == 200
    assert c.delete(f"/scim/v2/Users/{u.id}", headers=h).status_code == 204
    assert c.get("/api/v1/auth/me", headers=tok).status_code == 401
    assert w.db.get(User, u.id) is not None, "the account row stays so audit entries still resolve"
    acts = [a.action for a in w.db.execute(select(AuditLog)).scalars()]
    assert "user.deprovisioned" in acts
