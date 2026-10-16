"""Operational CLI.  migrate | status | create-migration MSG | downgrade REV --yes | create-user | seed-demo | fetch-fx | worker"""
from __future__ import annotations

import argparse
import getpass
import sys

from .config import assert_safe_for_production, settings
from .db import Base, SessionLocal, engine
from .models import Role, User
from .security import hash_password


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="upgrade the database schema to the latest revision (locked; safe to run from several pods)")
    sub.add_parser("init-db", help="alias of migrate (kept for existing deployment manifests)")
    sub.add_parser("status", help="show current vs latest schema revision and any drift from the models")
    cm = sub.add_parser("create-migration", help="developer: autogenerate a new revision from model changes")
    cm.add_argument("message")
    dg = sub.add_parser("downgrade", help="DANGEROUS: roll the schema back (data in dropped columns/tables is lost)")
    dg.add_argument("revision"); dg.add_argument("--yes", action="store_true")
    a = sub.add_parser("create-user")
    a.add_argument("email"); a.add_argument("--name", required=True); a.add_argument("--role", choices=[r.value for r in Role], default="admin")
    sub.add_parser("seed-demo")
    sub.add_parser("fetch-fx", help="fetch the FBIL/RBI reference rates once (for cron)")
    sub.add_parser("worker", help="run scheduled jobs: FX fetch, inbox polling, bank-feed sync, matching")
    args = ap.parse_args(argv)
    assert_safe_for_production()
    from . import migrate

    if args.cmd in ("migrate", "init-db"):
        try:
            print("schema at revision", migrate.upgrade(engine))
        except migrate.SchemaError as e:
            print(f"ERROR: {e}", file=sys.stderr); return 2
        return 0
    if args.cmd == "status":
        cur, h = migrate.current(engine), migrate.head()
        d = migrate.drift(engine) if cur else []
        print(f"current={cur} head={h} up_to_date={cur == h} drift={len(d)}")
        for x in d[:10]:
            print("  drift:", x)
        return 0 if cur == h and not d else 1
    if args.cmd == "create-migration":
        from alembic import command

        command.revision(migrate._cfg(), message=args.message, autogenerate=True)
        print("review the generated file before committing it"); return 0
    if args.cmd == "downgrade":
        if not args.yes:
            print("refusing: pass --yes to confirm data loss", file=sys.stderr); return 2
        migrate.downgrade(engine, args.revision); print("downgraded to", args.revision); return 0
    try:
        migrate.ensure_schema(engine, settings.auto_migrate)
    except migrate.SchemaError as e:
        print(f"ERROR: {e}", file=sys.stderr); return 2
    if args.cmd == "worker":
        import logging

        from . import jobs

        logging.basicConfig(level=logging.INFO)
        jobs.loop(SessionLocal)
        return 0
    db = SessionLocal()
    try:
        if args.cmd == "fetch-fx":
            from . import fxfeed

            print(fxfeed.run_feed(db, "cli"))
        if args.cmd == "create-user":
            pw = getpass.getpass("Password (min 12 chars): ")
            if len(pw) < 12:
                print("password too short", file=sys.stderr); return 1
            db.add(User(email=args.email.lower(), name=args.name, role=Role(args.role), password_hash=hash_password(pw)))
        elif args.cmd == "seed-demo":
            if not settings.allow_demo:
                print("ALLOW_DEMO=0", file=sys.stderr); return 1
            from .seed.demo import seed_demo
            print(seed_demo(db)["matching"])
        db.commit()
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
