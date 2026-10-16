"""Alembic environment. URL: -x url=..., else a connection injected by app.migrate, else DATABASE_URL via app settings."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool, text

from app import models  # noqa: F401  (registers every table on Base.metadata)
from app.config import settings
from app.db import Base

target_metadata = Base.metadata
config = context.config
PG_LOCK_ID = 727_274_001   # arbitrary app-wide key: serialises concurrent migrators (several pods starting at once)


def _url() -> str:
    return context.get_x_argument(as_dictionary=True).get("url") or settings.database_url


def _configure(**kw):
    context.configure(target_metadata=target_metadata, compare_type=True, render_as_batch=True,   # batch mode = ALTER support on SQLite
                      transaction_per_migration=True, **kw)


def run_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    injected = config.attributes.get("connection")
    if injected is not None:
        _run(injected)
        return
    eng = create_engine(_url(), poolclass=pool.NullPool, future=True)
    with eng.connect() as conn:
        _run(conn)
    eng.dispose()


def _run(conn) -> None:
    pg = conn.dialect.name == "postgresql"
    if pg:
        conn.execute(text("select pg_advisory_lock(:k)"), {"k": PG_LOCK_ID})
    try:
        _configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if pg:
            conn.execute(text("select pg_advisory_unlock(:k)"), {"k": PG_LOCK_ID})
            conn.commit()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
