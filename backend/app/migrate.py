"""Schema migrations (Alembic) with safe adoption of pre-Alembic databases and a start-up guard.

  * dev/test: the app upgrades its own schema on start (AUTO_MIGRATE=1)
  * production: migrations run as a separate locked step (`python -m app.cli migrate`); the app only VERIFIES and refuses to start
    on a stale schema, so a deploy can never run new code against old tables.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from .db import Base

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "0001"


class SchemaError(RuntimeError):
    pass


def _cfg(conn=None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    if conn is not None:
        cfg.attributes["connection"] = conn
    return cfg


def head() -> str:
    return ScriptDirectory.from_config(_cfg()).get_current_head()


def current(engine: Engine) -> str | None:
    with engine.connect() as c:
        return MigrationContext.configure(c).get_current_revision()


def drift(engine: Engine) -> list:
    """Differences between the live schema and the models (empty = in sync). Used as a CI guard against model changes without a migration."""
    with engine.connect() as c:
        return compare_metadata(MigrationContext.configure(c, opts={"compare_type": True}), Base.metadata)


def _columns(engine: Engine) -> dict[str, set[str]]:
    insp = inspect(engine)
    return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names() if t != "alembic_version"}


def revision_schemas() -> list[tuple[str, dict[str, set[str]]]]:
    """(revision, {table: columns}) for every revision, oldest first, computed by actually applying each migration in a scratch SQLite database."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    scratch = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    revs = [r.revision for r in reversed(list(ScriptDirectory.from_config(_cfg()).walk_revisions()))]
    out = []
    for r in revs:
        with scratch.connect() as conn:
            command.upgrade(_cfg(conn), r)
            conn.commit()
        out.append((r, _columns(scratch)))
    scratch.dispose()
    return out


def adoptable_revision(engine: Engine) -> tuple[str | None, list[str]]:
    """For a database created by the old create_all(): the NEWEST revision whose tables/columns it already contains (it may predate or postdate
    later revisions). Returns (revision, []) or (None, what is missing relative to the baseline). Constraint/index names are ignored — they
    predate the naming convention. Extra tables/columns in the live DB are tolerated."""
    live, best = _columns(engine), None
    schemas = revision_schemas()
    for rev, cols in schemas:
        if all(t in live and cs <= live[t] for t, cs in cols.items()):
            best = rev
    if best is None:
        base = schemas[0][1]
        return None, [f"missing table {t}" if t not in live else f"missing column {t}.{c}" for t, cs in base.items() for c in sorted(cs) if t not in live or c not in live[t]]
    return best, []


def upgrade(engine: Engine, revision: str = "head") -> str | None:
    """Upgrade to `revision`. A database with app tables but no alembic_version is adopted (stamped at the baseline) only if its columns match;
    otherwise this raises instead of guessing."""
    has_version = "alembic_version" in inspect(engine).get_table_names()
    has_tables = "users" in inspect(engine).get_table_names()
    with engine.begin() as conn:
        if has_tables and not has_version:
            rev, probs = adoptable_revision(engine)
            if rev is None:
                raise SchemaError("Existing database predates migrations and does not match the baseline schema: " + "; ".join(probs[:6])
                                  + ". Fix it manually (or restore from a backup) before upgrading; nothing was changed.")
            command.stamp(_cfg(conn), rev)
    with engine.connect() as conn:       # alembic manages its own transactions (per migration); a PG advisory lock serialises replicas
        command.upgrade(_cfg(conn), revision)
        conn.commit()
    return current(engine)


def downgrade(engine: Engine, revision: str) -> None:
    with engine.connect() as conn:
        command.downgrade(_cfg(conn), revision)
        conn.commit()


def ensure_schema(engine: Engine, auto: bool) -> str | None:
    cur, h = current(engine), head()
    if cur == h:
        return cur
    if auto:
        return upgrade(engine)
    raise SchemaError(f"Database schema is at revision {cur or 'none'} but this release needs {h}. Run `python -m app.cli migrate` (as a Job/init step) and restart.")
