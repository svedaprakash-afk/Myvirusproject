"""Operational CLI.  python -m app.cli init-db | create-admin EMAIL | seed-demo"""
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
    sub.add_parser("init-db")
    a = sub.add_parser("create-user")
    a.add_argument("email"); a.add_argument("--name", required=True); a.add_argument("--role", choices=[r.value for r in Role], default="admin")
    sub.add_parser("seed-demo")
    args = ap.parse_args(argv)
    assert_safe_for_production()
    Base.metadata.create_all(engine)
    if args.cmd == "init-db":
        print("tables ready"); return 0
    db = SessionLocal()
    try:
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
