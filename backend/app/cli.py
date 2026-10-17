"""Operational CLI.  migrate | status | doctor | create-migration MSG | downgrade REV --yes | create-user | reset-password | seed-demo | fetch-fx | worker"""
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
    a.add_argument("--password-env", help="read the password from this environment variable instead of prompting (for scripted installs)")
    r = sub.add_parser("reset-password", help="set a new password for a user (signs out their existing sessions)")
    r.add_argument("email"); r.add_argument("--password-env")
    sub.add_parser("doctor", help="production readiness checks (non-zero exit if any fail)")
    sub.add_parser("seed-demo")
    sub.add_parser("fetch-fx", help="fetch the FBIL/RBI reference rates once (for cron)")
    sub.add_parser("worker", help="run scheduled jobs: FX fetch, inbox polling, bank-feed sync, matching")
    args = ap.parse_args(argv)
    assert_safe_for_production()
    from . import migrate

    if args.cmd == "doctor":
        from . import doctor

        checks = doctor.run()
        print(doctor.render(checks))
        return doctor.exit_code(checks)
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
        if args.cmd in ("create-user", "reset-password"):
            import os

            from . import passwords
            from sqlalchemy import select

            email = args.email.lower().strip()
            pw = os.environ.get(args.password_env or "") if args.password_env else getpass.getpass("Password (min 12 chars): ")
            if args.password_env and not pw:
                print(f"environment variable {args.password_env} is not set", file=sys.stderr); return 1
            existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
            errs = passwords.check(pw or "", email, getattr(args, "name", "") or (existing.name if existing else ""))
            if errs:
                print("password rejected: it " + "; it ".join(errs), file=sys.stderr); return 1
            if args.cmd == "create-user":
                if existing:
                    print(f"{email} already exists (use reset-password)", file=sys.stderr); return 1
                db.add(User(email=email, name=args.name, role=Role(args.role), password_hash=hash_password(pw)))
            else:
                if not existing:
                    print(f"no such user {email}", file=sys.stderr); return 1
                from .models import utcnow

                existing.password_hash, existing.password_changed_at = hash_password(pw), utcnow()
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
