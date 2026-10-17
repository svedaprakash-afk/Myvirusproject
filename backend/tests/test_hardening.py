"""Password policy, lockout, password change and session revocation, production docs lockdown, doctor, CLI user management."""
import os
import subprocess
import sys
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import doctor, passwords
from app.models import AuditLog, Role, User, utcnow
from app.security import hash_password, make_token, verify_password
from tests.conftest import login

BACKEND = os.path.join(os.path.dirname(__file__), "..")


@pytest.mark.parametrize("pw,fragment", [
    ("short1!", "at least 12"), ("aaaaaaaaaaaa", "few different"), ("Password12345", "too common"), ("letmeinletmein", "too common"), ("abcabcabcabc", "common or repetitive"),
    ("ana.kumar-2026-ok", "your email name"), ("x" * 200, "at most 128")])
def test_weak_passwords_are_refused_with_a_reason(pw, fragment):
    assert any(fragment in e for e in passwords.check(pw, "ana.kumar@example.in", "Ana Kumar")), passwords.check(pw, "ana.kumar@example.in", "Ana Kumar")


def test_a_long_unremarkable_passphrase_is_accepted_without_character_class_rules():
    assert passwords.check("correct horse battery staple", "ana.kumar@example.in", "Ana Kumar") == []
    assert passwords.check("river-lantern-oxygen-42", "x@example.in") == []
    assert any("your name" in e for e in passwords.check("kumarkumar-lantern-oxygen", "x@example.in", "Ana Kumar"))


def fails(client, email, n, pw="wrong-password"):
    return [client.post("/api/v1/auth/login", json={"email": email, "password": pw}).status_code for _ in range(n)]


def test_five_failures_lock_the_account_even_for_the_right_password_then_it_clears(client, w, db):
    email = "ar_analyst@t.example"
    assert fails(client, email, 5) == [401] * 5
    r = client.post("/api/v1/auth/login", json={"email": email, "password": "pw-ar_analyst"})
    assert r.status_code == 429 and "Too many failed attempts" in r.json()["detail"] and int(r.headers["Retry-After"]) > 800
    assert db.execute(select(AuditLog).where(AuditLog.action == "auth.locked")).scalars().first() is not None
    # a lock does not extend itself: failures older than the window stop counting
    for row in db.execute(select(AuditLog).where(AuditLog.action == "auth.failed")).scalars():
        row.ts = utcnow() - timedelta(minutes=16)
    db.flush()
    assert client.post("/api/v1/auth/login", json={"email": email, "password": "pw-ar_analyst"}).status_code == 200


def test_lockout_is_per_account_and_unknown_emails_cannot_be_told_apart(client, w):
    assert fails(client, "nobody@t.example", 5) == [401] * 5
    assert client.post("/api/v1/auth/login", json={"email": "nobody@t.example", "password": "x"}).status_code == 429, "same behaviour as a real account"
    assert client.post("/api/v1/auth/login", json={"email": "ar_manager@t.example", "password": "pw-ar_manager"}).status_code == 200, "other accounts unaffected"
    assert fails(client, "AR_Analyst@T.example ", 4) == [401] * 4, "case and whitespace do not give extra tries"
    assert fails(client, "ar_analyst@t.example", 1) == [401]
    assert fails(client, "ar_analyst@t.example", 1) == [429]


def test_change_password_needs_the_current_one_enforces_policy_and_ends_other_sessions(client, w, db):
    u = w.users["ar_analyst"]
    old = {"Authorization": "Bearer " + make_token(u)}
    time.sleep(1.1)       # token iat has one-second resolution
    url = "/api/v1/auth/change-password"
    assert client.post(url, json={"current_password": "nope", "new_password": "river-lantern-oxygen-42"}, headers=old).status_code == 403
    assert client.post(url, json={"current_password": "pw-ar_analyst", "new_password": "short"}, headers=old).status_code == 422
    assert client.post(url, json={"current_password": "pw-ar_analyst", "new_password": "pw-ar_analyst"}, headers=old).status_code == 422
    r = client.post(url, json={"current_password": "pw-ar_analyst", "new_password": "river-lantern-oxygen-42"}, headers=old)
    assert r.status_code == 200 and verify_password("river-lantern-oxygen-42", u.password_hash) and u.password_changed_at is not None
    assert client.get("/api/v1/auth/me", headers=old).status_code == 401, "the session that existed before the change is signed out"
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + r.json()["token"]}).status_code == 200, "the new token returned to this browser works"
    assert client.post("/api/v1/auth/login", json={"email": u.email, "password": "river-lantern-oxygen-42"}).status_code == 200
    assert db.execute(select(AuditLog).where(AuditLog.action == "auth.password_changed")).scalars().first() is not None


def test_guessing_through_change_password_counts_toward_the_lockout(client, w):
    h = login(client, "ar_analyst@t.example", "pw-ar_analyst")
    for _ in range(5):
        assert client.post("/api/v1/auth/change-password", json={"current_password": "guess", "new_password": "river-lantern-oxygen-42"}, headers=h).status_code == 403
    assert client.post("/api/v1/auth/login", json={"email": "ar_analyst@t.example", "password": "pw-ar_analyst"}).status_code == 429


def test_api_documentation_is_not_published_in_production_but_is_in_development():
    code = "from app.main import app; print(app.docs_url, app.openapi_url)"
    env = {**os.environ, "DATABASE_URL": "sqlite://", "JWT_SECRET": "x" * 40}
    prod = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env={**env, "APP_ENV": "prod"}, capture_output=True, text=True)
    dev = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env={**env, "APP_ENV": "dev"}, capture_output=True, text=True)
    assert prod.stdout.strip() == "None None" and dev.stdout.strip() == "/docs /openapi.json", (prod.stderr[-300:], dev.stderr[-300:])


# ------------------------------------------------------------------------------------------------ doctor
@pytest.fixture()
def prod_like(db, monkeypatch):
    import dataclasses

    from sqlalchemy.orm import sessionmaker

    from app import config, migrate

    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, env="prod", jwt_secret="a-strong-random-key-" + "x" * 30, allow_demo=False,
                                                                  cors_origins=("https://cash.example.in",), residency_strict=True, deploy_region="ap-south-1"))
    monkeypatch.setattr(doctor, "engine", db.get_bind())
    monkeypatch.setattr(doctor, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    monkeypatch.setattr(migrate, "current", lambda e: migrate.head())
    monkeypatch.setattr(migrate, "drift", lambda e: [])
    return db


def db_is_sqlite(db):
    return db.get_bind().dialect.name == "sqlite"


def by_name(checks):
    return {c.name: c for c in checks}


def test_doctor_passes_a_sound_deployment_and_warns_about_what_is_only_adequate(prod_like, w, tmp_path, monkeypatch):
    import dataclasses

    from app import config

    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, export_dir=str(tmp_path / "e"), feed_root=str(tmp_path / "f")))
    prod_like.add(User(email="boss@corp.example", active=True, name="Boss", role=Role.admin, password_hash=hash_password("x")))
    prod_like.commit()
    for u in prod_like.execute(select(User)).scalars():
        if u.role == Role.admin:
            u.password_changed_at = utcnow()
    prod_like.commit()
    c = by_name(doctor.run())
    assert c["environment"].status == c["signing secret"].status == c["demo data"].status == c["schema"].status == "ok"
    assert c["legal entity"].status == "ok", "the World fixture has one"
    assert c["storage"].status == "ok" and c["administrator"].status == "ok" and c["demo accounts"].status == "ok"
    assert c["sign-in policy"].status == "warn" and "no MFA" in c["sign-in policy"].detail
    if db_is_sqlite(prod_like):
        assert c["database engine"].status == "warn"
    else:
        assert "database engine" not in c
    assert doctor.exit_code(doctor.run()) == 0


def test_doctor_fails_loudly_on_the_things_that_must_not_ship(prod_like, w, monkeypatch):
    import dataclasses

    from app import config

    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, env="dev", jwt_secret="dev-only-change-me", allow_demo=True))
    c = by_name(doctor.run())
    assert [c[k].status for k in ("environment", "signing secret", "demo data")] == ["fail", "fail", "fail"]
    assert c["demo accounts"].status == "ok" and c["administrator"].status == "fail" or c["administrator"].status == "ok"
    assert doctor.exit_code(doctor.run()) == 1
    prod_like.add(User(email="x@meridian.example", active=True, name="Demo", role=Role.admin, password_hash=hash_password("demo1234")))
    prod_like.commit()
    c = by_name(doctor.run())
    assert c["demo accounts"].status == "fail" and c["initial password"].status == "warn"


def test_doctor_reports_an_unmigrated_database_instead_of_crashing(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    eng = create_engine(f"sqlite:///{tmp_path}/empty.db")
    monkeypatch.setattr(doctor, "engine", eng)
    monkeypatch.setattr(doctor, "SessionLocal", sessionmaker(bind=eng))
    c = by_name(doctor.run())
    assert c["database"].status == "ok" and c["schema"].status == "fail"
    assert doctor.exit_code(doctor.run()) == 1


# ------------------------------------------------------------------------------------------------ CLI
def cli(tmp_path, *args, **env):
    e = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path}/cli.db", "APP_ENV": "dev", "JWT_SECRET": "y" * 40, "PBKDF2_ITERATIONS": "1000", **env}
    return subprocess.run([sys.executable, "-m", "app.cli", *args], cwd=BACKEND, env=e, capture_output=True, text=True)


def test_cli_creates_and_resets_users_non_interactively_with_the_policy_applied(tmp_path):
    assert cli(tmp_path, "migrate").returncode == 0
    r = cli(tmp_path, "create-user", "ana@corp.example", "--name", "Ana Kumar", "--password-env", "PW", PW="short")
    assert r.returncode == 1 and "at least 12" in r.stderr
    assert cli(tmp_path, "create-user", "ana@corp.example", "--name", "Ana Kumar", "--password-env", "PW", PW="river-lantern-oxygen-42").returncode == 0
    r = cli(tmp_path, "create-user", "ana@corp.example", "--name", "Ana Kumar", "--password-env", "PW", PW="river-lantern-oxygen-43")
    assert r.returncode == 1 and "already exists" in r.stderr
    assert cli(tmp_path, "reset-password", "ana@corp.example", "--password-env", "PW", PW="another-long-passphrase-9").returncode == 0
    assert cli(tmp_path, "reset-password", "ghost@corp.example", "--password-env", "PW", PW="another-long-passphrase-9").returncode == 1
    r = cli(tmp_path, "create-user", "b@corp.example", "--name", "B", "--password-env", "MISSING")
    assert r.returncode == 1 and "MISSING is not set" in r.stderr
    assert cli(tmp_path, "doctor").returncode == 1, "dev settings: the doctor refuses to bless them"


def test_create_entity_sets_up_a_usable_suggest_only_tenant(db):
    from app import onboard
    from app.models import ApprovalPolicy, BankAccount, ToleranceRule

    e = onboard.create_entity(db, "Acme Traders Pvt Ltd", "27AABCM4421F1ZU", None, ["HDFC Bank|50200012345678|HDFC0000123|1100-HDFC", "USD a/c|9988|||USD"])
    assert e.pan == "AABCM4421F" and e.config["auto_approve_enabled"] is False and e.ledgers["ar_control"]
    banks = db.execute(select(BankAccount).where(BankAccount.entity_id == e.id).order_by(BankAccount.id)).scalars().all()
    assert [(b.ifsc, b.currency, b.ledger_code) for b in banks] == [("HDFC0000123", "INR", "1100-HDFC"), (None, "USD", "1100-BANK")]
    assert db.query(ToleranceRule).filter_by(entity_id=e.id).count() == 1 and db.query(ApprovalPolicy).filter_by(entity_id=e.id).count() == 7


@pytest.mark.parametrize("kw,msg", [({"name": " "}, "name is required"), ({"name": "X", "gstin": "27AABCM4421F1ZZ"}, "GSTIN"), ({"name": "X", "pan": "123"}, "PAN"),
                                    ({"name": "X", "banks": ["only|two"]}, "use 'Bank name"), ({"name": "X", "banks": ["B|1|BAD|L"]}, "IFSC"), ({"name": "X", "banks": ["B|1||L|US"]}, "3-letter")])
def test_create_entity_rejects_bad_input_without_writing_anything(db, kw, msg):
    from app import onboard
    from app.models import LegalEntity

    with pytest.raises(onboard.OnboardError, match=msg):
        onboard.create_entity(db, **kw)
    assert db.query(LegalEntity).count() == 0


def test_create_entity_refuses_a_duplicate_name(db):
    from app import onboard

    onboard.create_entity(db, "Acme")
    with pytest.raises(onboard.OnboardError, match="already exists"):
        onboard.create_entity(db, "Acme")


def test_cli_create_entity(tmp_path):
    assert cli(tmp_path, "migrate").returncode == 0
    r = cli(tmp_path, "create-entity", "--name", "Acme", "--gstin", "27AABCM4421F1ZU", "--bank", "HDFC|123|HDFC0000123|1100-HDFC")
    assert r.returncode == 0 and "created entity 1" in r.stdout and "1 bank" in r.stdout
    assert cli(tmp_path, "create-entity", "--name", "Acme").returncode == 1
