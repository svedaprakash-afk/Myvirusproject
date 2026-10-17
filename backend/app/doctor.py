"""Production readiness checks (`python -m app.cli doctor`). Exit status is non-zero when anything FAILS; warnings do not fail it.

It checks what software can check: configuration, the database, the schema revision, storage, users. It cannot check the things around the software
(firewall, DNS, TLS certificate, backups leaving the machine, patching): the runbook has those.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select, text

from . import config, migrate
from .db import SessionLocal, engine
from .models import Role, User

KNOWN_BAD_SECRETS = ("dev-only", "change-me", "changeme", "secret", "password")


@dataclass
class Check:
    name: str
    status: str          # ok | warn | fail
    detail: str


def run() -> list[Check]:
    s = config.settings
    out: list[Check] = []
    add = lambda n, st, d: out.append(Check(n, st, d))

    add("environment", "ok" if s.env == "prod" else "fail", "APP_ENV=prod" if s.env == "prod" else f"APP_ENV is '{s.env}', not 'prod'")
    weak = len(s.jwt_secret) < 32 or any(b in s.jwt_secret.lower() for b in KNOWN_BAD_SECRETS)
    add("signing secret", "fail" if weak else "ok", "JWT_SECRET is short or a placeholder: use 48+ random characters" if weak else "JWT_SECRET is set and long")
    add("demo data", "fail" if s.allow_demo else "ok", "ALLOW_DEMO must be 0: the demo seeder wipes data" if s.allow_demo else "demo seeding is off")
    local = all(o.startswith(("http://localhost", "http://127.0.0.1")) for o in s.cors_origins)
    add("CORS", "warn" if local else "ok", "CORS_ORIGINS still points at localhost" if local else "CORS origins: " + ", ".join(s.cors_origins))
    add("data residency", "ok" if s.residency_strict and s.deploy_region in config.INDIA_REGIONS else "warn",
        f"strict mode on, region {s.deploy_region}" if s.residency_strict and s.deploy_region in config.INDIA_REGIONS else "DATA_RESIDENCY_STRICT is off or DEPLOY_REGION is not an Indian region label")
    add("sign-in policy", "warn" if (s.sso_enforce is False and not s.sso_issuer) else "ok",
        "local passwords only (no MFA): fine for a pilot, a gap for real financial data" if (s.sso_enforce is False and not s.sso_issuer) else "single sign-on configured")

    try:
        with engine.connect() as c:
            c.execute(text("select 1"))
        add("database", "ok", f"reachable ({engine.dialect.name})")
        if engine.dialect.name == "sqlite":
            add("database engine", "warn", "SQLite is for development: use PostgreSQL in production")
        cur, head = migrate.current(engine), migrate.head()
        add("schema", "ok" if cur == head else "fail", f"at revision {cur}" if cur == head else f"at {cur}, release expects {head}: run `migrate`")
        drift = migrate.drift(engine) if cur else []
        add("schema drift", "ok" if not drift else "fail", "matches the models" if not drift else f"{len(drift)} difference(s) from the models")
    except Exception as e:
        add("database", "fail", f"cannot connect: {type(e).__name__}")
        return out

    try:
        from . import storage

        errs = storage.validate_settings(s)
        if errs:
            add("storage", "fail", "; ".join(errs))
        else:
            res = {a: storage.probe(storage.get_store(a)) for a in ("exports", "feeds")}
            add("storage", "ok" if all(r["ok"] for r in res.values()) else "fail", f"{s.storage_backend}: write, read-back and delete verified")
    except Exception as e:
        add("storage", "fail", f"{type(e).__name__}: {e}"[:200])

    try:
        _users(out)
    except Exception as e:
        add("users", "fail", f"cannot read users ({type(e).__name__}): is the schema migrated?")
    return out


def _users(out: list[Check]) -> None:
    add = lambda n, st, d: out.append(Check(n, st, d))
    with SessionLocal() as db:
        admins = db.execute(select(User).where(User.role == Role.admin, User.active.is_(True))).scalars().all()
        add("administrator", "ok" if admins else "fail", f"{len(admins)} active administrator(s)" if admins else "no active administrator: create one with `create-user`")
        unchanged = [a.email for a in admins if a.password_changed_at is None]
        if unchanged:
            add("initial password", "warn", f"{', '.join(unchanged)} still use the password they were created with: change it after first sign-in")
        demo = db.scalar(select(func.count()).select_from(User).where(User.email.like("%@meridian.example")))
        add("demo accounts", "fail" if demo else "ok", f"{demo} demo account(s) with a published password exist: remove them" if demo else "no demo accounts")


def render(checks: list[Check]) -> str:
    icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    return "\n".join(f"[{icon[c.status]}] {c.name}: {c.detail}" for c in checks)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == "fail" for c in checks) else 0
