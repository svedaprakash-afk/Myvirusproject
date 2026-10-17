import os
import subprocess
import sys

import pytest
from sqlalchemy import inspect, text

from app import migrate
from app.db import Base, make_engine
from app.models import Role, User

BACKEND = os.path.join(os.path.dirname(__file__), "..")
PG = os.environ.get("TEST_PG_URL")        # e.g. postgresql+psycopg://postgres@localhost:5433/mig_test (a scratch database)


@pytest.fixture()
def eng(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path}/m.db")
    yield e
    e.dispose()


def tables(e):
    return set(inspect(e).get_table_names())


def test_fresh_database_upgrades_to_exactly_the_models(eng):
    assert migrate.upgrade(eng) == migrate.head() == "0008"
    assert tables(eng) == set(Base.metadata.tables) | {"alembic_version"}
    assert migrate.drift(eng) == [], "CI GUARD: a model changed without a migration. Run `python -m app.cli create-migration \"msg\"` and review it."


def test_upgrade_is_idempotent_and_preserves_data(eng):
    migrate.upgrade(eng)
    with eng.begin() as c:
        c.execute(User.__table__.insert().values(email="a@x.com", name="A", role=Role.admin, password_hash="x", active=True, entity_ids=[], created_at=__import__("datetime").datetime.utcnow()))
    assert migrate.upgrade(eng) == migrate.head()
    with eng.connect() as c:
        assert c.execute(text("select count(*) from users")).scalar() == 1


def test_downgrade_to_base_removes_everything_but_the_version_table(eng):
    migrate.upgrade(eng)
    migrate.downgrade(eng, "base")
    assert tables(eng) == {"alembic_version"}
    assert migrate.upgrade(eng) == migrate.head()


def test_database_created_by_the_old_create_all_is_adopted_without_data_loss(eng):
    Base.metadata.create_all(eng)               # what every pre-Alembic install looks like
    with eng.begin() as c:
        c.execute(User.__table__.insert().values(email="old@x.com", name="Old", role=Role.auditor, password_hash="x", active=True, entity_ids=[], created_at=__import__("datetime").datetime.utcnow()))
    assert migrate.current(eng) is None
    assert migrate.upgrade(eng) == migrate.head()
    with eng.connect() as c:
        assert c.execute(text("select email from users")).scalar() == "old@x.com"


def test_0001_to_0002_upgrade_preserves_existing_batches_and_downgrade_removes_the_column(eng):
    migrate.upgrade(eng, "0001")
    assert "export_key" not in {c["name"] for c in inspect(eng).get_columns("posting_batches")}
    with eng.begin() as c:
        c.execute(text("insert into legal_entities (id, name, base_currency, config, ledgers) values (1, 'E', 'INR', '{}', '{}')"))
        c.execute(text("insert into posting_batches (id, entity_id, status, idempotency_key, created_at, total_minor) values (7, 1, 'draft', 'k', '2026-10-01 00:00:00', 5)"))
    assert migrate.upgrade(eng, "0002") == "0002"
    with eng.connect() as c:
        assert c.execute(text("select total_minor, export_key from posting_batches where id = 7")).one() == (5, None)
    migrate.downgrade(eng, "0001")
    assert "export_key" not in {c["name"] for c in inspect(eng).get_columns("posting_batches")}
    with eng.connect() as c:
        assert c.execute(text("select total_minor from posting_batches where id = 7")).scalar() == 5


def test_legacy_database_from_an_older_release_is_stamped_at_its_own_revision_then_upgraded(eng):
    """A create_all() database from before export_key existed: it is at 0001, so adoption stamps 0001 and the upgrade ADDS the column
    (stamping head would silently leave it without the column; stamping 0001 blindly would break databases that already have it)."""
    Base.metadata.create_all(eng)
    _strip_0003(eng)
    with eng.begin() as c:
        c.execute(text("alter table posting_batches drop column export_key"))
    assert migrate.adoptable_revision(eng) == ("0001", [])
    assert migrate.upgrade(eng) == HEAD
    assert "export_key" in {c["name"] for c in inspect(eng).get_columns("posting_batches")}
    assert "customer_trust" in tables(eng)


HEAD = "0008"


def _strip_0003(eng):
    """Make a create_all() database look like it predates 0003 (drop everything 0003 to 0008 added)."""
    with eng.begin() as c:
        c.execute(text("drop table notifications"))
        c.execute(text("drop table period_locks"))
        c.execute(text("drop table gst_documents"))
        c.execute(text("drop table form26as_entries"))
        c.execute(text("drop index ix_customers_tan"))
        c.execute(text("alter table customers drop column tan"))
        c.execute(text("alter table tds_entries drop column kind"))
        for t in ("advance_applications", "on_account_balances", "customer_trust"):
            c.execute(text(f"drop table {t}"))
        c.execute(text("alter table match_proposals drop column shadow_confidence"))
        c.execute(text("alter table match_proposals drop column shadow_model_version"))
        c.execute(text("alter table scoring_models drop column shadow"))


def test_legacy_database_at_0002_is_stamped_there_and_upgraded_to_head(eng):
    Base.metadata.create_all(eng)
    _strip_0003(eng)
    assert migrate.adoptable_revision(eng) == ("0002", [])
    assert migrate.upgrade(eng) == HEAD


def test_0004_and_0005_add_their_objects_and_downgrade_cleanly(eng):
    migrate.upgrade(eng, "0003")
    assert "kind" not in {c["name"] for c in inspect(eng).get_columns("tds_entries")}
    migrate.upgrade(eng, "0004")
    migrate.upgrade(eng)
    assert "form26as_entries" in tables(eng) and "tan" in {c["name"] for c in inspect(eng).get_columns("customers")}
    migrate.downgrade(eng, "0004")
    assert "form26as_entries" not in tables(eng) and "tan" not in {c["name"] for c in inspect(eng).get_columns("customers")}
    migrate.upgrade(eng)
    col = next(c for c in inspect(eng).get_columns("tds_entries") if c["name"] == "kind")
    assert "income_tax" in str(col["default"]) and col["nullable"] is False, "existing rows must read as income-tax TDS"
    migrate.downgrade(eng, "0003")
    assert "kind" not in {c["name"] for c in inspect(eng).get_columns("tds_entries")}


def test_0002_to_0003_keeps_existing_models_and_defaults_shadow_off_and_downgrades(eng):
    migrate.upgrade(eng, "0002")
    with eng.begin() as c:
        c.execute(text("insert into scoring_models (id, version, weights, active, metrics, note, created_at) values (1, 'v1', '{}', 1, '{}', '', '2026-10-01 00:00:00')"))
    migrate.upgrade(eng)
    with eng.connect() as c:
        assert c.execute(text("select shadow from scoring_models where id = 1")).scalar() in (0, False)
    migrate.downgrade(eng, "0002")
    assert "customer_trust" not in tables(eng) and "shadow" not in {c["name"] for c in inspect(eng).get_columns("scoring_models")}


def test_legacy_database_that_already_has_the_latest_columns_is_stamped_at_head(eng):
    Base.metadata.create_all(eng)
    assert migrate.adoptable_revision(eng) == (HEAD, [])


def test_legacy_database_that_does_not_match_the_baseline_is_refused_untouched(eng):
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text("alter table invoices drop column booking_rate_micro"))     # an old install from before multi-currency
    with pytest.raises(migrate.SchemaError, match="booking_rate_micro"):
        migrate.upgrade(eng)
    assert "alembic_version" not in tables(eng), "nothing may be stamped or changed when the schema is unknown"


def test_drift_guard_detects_schema_that_differs_from_models(eng):
    migrate.upgrade(eng)
    with eng.begin() as c:
        c.execute(text("alter table users add column rogue text"))
    assert migrate.drift(eng), "a column that exists in the DB but not in the models must be reported"


def test_ensure_schema_modes(eng):
    with pytest.raises(migrate.SchemaError, match="migrate"):
        migrate.ensure_schema(eng, auto=False)       # production: never silently run new code on an old/empty schema
    assert migrate.ensure_schema(eng, auto=True) == migrate.head()
    assert migrate.ensure_schema(eng, auto=False) == migrate.head()


def run(*args, env=None, **kw):
    return subprocess.run([sys.executable, *args], cwd=BACKEND, capture_output=True, text=True, env={**os.environ, **(env or {})}, **kw)


def test_cli_migrate_status_and_downgrade_safety(tmp_path):
    env = {"DATABASE_URL": f"sqlite:///{tmp_path}/cli.db"}
    assert run("-m", "app.cli", "status", env=env).returncode == 1                        # no schema yet
    r = run("-m", "app.cli", "migrate", env=env)
    assert r.returncode == 0 and f"revision {migrate.head()}" in r.stdout
    s = run("-m", "app.cli", "status", env=env)
    assert s.returncode == 0 and "up_to_date=True drift=0" in s.stdout
    assert run("-m", "app.cli", "downgrade", "base", env=env).returncode == 2             # refuses without --yes
    assert run("-m", "app.cli", "downgrade", "base", "--yes", env=env).returncode == 0


def test_production_refuses_to_start_on_a_stale_schema_and_dev_upgrades(tmp_path):
    code = ("import asyncio; from app.main import app\n"
            "async def go():\n    async with app.router.lifespan_context(app): print('STARTED')\n"
            "asyncio.run(go())")
    prod = {"DATABASE_URL": f"sqlite:///{tmp_path}/p.db", "APP_ENV": "prod", "ALLOW_DEMO": "0", "JWT_SECRET": "x" * 40}
    r = run("-c", code, env=prod)
    assert r.returncode != 0 and "app.cli migrate" in r.stderr and "STARTED" not in r.stdout
    assert run("-m", "app.cli", "migrate", env=prod).returncode == 0
    assert "STARTED" in run("-c", code, env=prod).stdout
    dev = {"DATABASE_URL": f"sqlite:///{tmp_path}/d.db", "APP_ENV": "dev"}
    assert "STARTED" in run("-c", code, env=dev).stdout and migrate.current(make_engine(dev["DATABASE_URL"])) == migrate.head()


# ---------------------------------------------------------------------------------------------- PostgreSQL (real server)
pg = pytest.mark.skipif(not PG, reason="set TEST_PG_URL to a scratch PostgreSQL database to run these")


@pytest.fixture()
def pgeng():
    e = make_engine(PG)
    with e.begin() as c:
        c.execute(text("drop schema public cascade"))
        c.execute(text("create schema public"))
    yield e
    e.dispose()


@pg
def test_postgres_upgrade_has_one_copy_of_each_enum_and_no_drift(pgeng):
    assert migrate.upgrade(pgeng) == migrate.head()
    assert migrate.drift(pgeng) == []
    with pgeng.connect() as c:
        enums = [r[0] for r in c.execute(text("select typname from pg_type where typtype='e'"))]
    assert sorted(enums) == sorted({"approvalkind", "approvalstatus", "batchstatus", "exceptionstatus", "exceptiontype", "proposalstatus", "role", "txnstatus"})


@pg
def test_postgres_downgrade_drops_tables_and_enum_types(pgeng):
    migrate.upgrade(pgeng)
    migrate.downgrade(pgeng, "base")
    with pgeng.connect() as c:
        assert c.execute(text("select count(*) from pg_type where typtype='e'")).scalar() == 0
    assert tables(pgeng) == {"alembic_version"}
    assert migrate.upgrade(pgeng) == migrate.head()  # and it can be re-applied


@pg
def test_postgres_legacy_create_all_database_is_adopted(pgeng):
    Base.metadata.create_all(pgeng)
    assert migrate.upgrade(pgeng) == migrate.head()
    assert migrate.drift(pgeng) == []


@pg
def test_postgres_concurrent_migrators_are_serialised(pgeng):
    """Four separate OS processes ('pods') migrate the same empty database at the same instant. (Alembic's context is process-global, so
    threads in one process are not a valid simulation.)"""
    env = {**os.environ, "DATABASE_URL": PG}
    ps = [subprocess.Popen([sys.executable, "-m", "app.cli", "migrate"], cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    res = [(p.wait(timeout=120), p.stdout.read(), p.stderr.read()) for p in ps]
    assert [r[0] for r in res] == [0, 0, 0, 0], res
    assert migrate.current(pgeng) == migrate.head() and migrate.drift(pgeng) == []
