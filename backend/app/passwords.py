"""Password policy and sign-in throttling.

Policy follows current guidance: length matters more than character classes, and a password must not be a well-known one or contain the person's own
name. Throttling is per account (counted from the audit trail, so it works across replicas and survives restarts): five failed attempts lock the
account for fifteen minutes. The trade-off is explicit: an attacker who knows an email can keep that person locked out; the lock is short, is
audited, and an administrator can see it. There is no per-IP limit here: put one at the reverse proxy.
"""
from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog, utcnow

MIN_LEN, MAX_LEN = 12, 128
LOCK_AFTER, WINDOW = 5, timedelta(minutes=15)
COMMON = {
    "password", "passw0rd", "qwerty", "qwertyuiop", "letmein", "welcome", "admin", "administrator", "iloveyou", "monkey", "dragon", "football", "baseball",
    "abc123", "123456", "12345678", "123456789", "1234567890", "111111", "000000", "changeme", "change-me", "secret", "master", "login", "princess", "sunshine",
    "india", "india123", "mumbai", "delhi", "bangalore", "cricket", "namaste", "cashapp", "cashapplication", "receivables", "meridian", "demo1234",
}


def check(pw: str, email: str = "", name: str = "") -> list[str]:
    """Reasons the password is not acceptable (empty list = fine)."""
    errs: list[str] = []
    if len(pw) < MIN_LEN:
        errs.append(f"must be at least {MIN_LEN} characters")
    if len(pw) > MAX_LEN:
        errs.append(f"must be at most {MAX_LEN} characters")
    low = pw.lower()
    stripped = re.sub(r"[^a-z]", "", low)
    if len(set(low)) < 5:
        errs.append("uses too few different characters")
    if any(c in stripped for c in COMMON if len(c) >= 6) or low in COMMON or re.fullmatch(r"(.{1,4})\1{2,}", low):
        errs.append("is too common or repetitive")
    local = email.split("@")[0].lower()
    if len(local) >= 4 and local in low:
        errs.append("must not contain your email name")
    for part in re.split(r"\W+", name.lower()):
        if len(part) >= 4 and part in low:
            errs.append("must not contain your name")
            break
    return errs


def locked_for(db: Session, email: str, now=None) -> int | None:
    """Seconds until sign-in is allowed again, or None if the account is not locked."""
    now = now or utcnow()
    rows = db.execute(select(AuditLog.ts).where(AuditLog.action == "auth.failed", AuditLog.actor == email, AuditLog.ts >= now - WINDOW)
                      .order_by(AuditLog.ts.desc()).limit(LOCK_AFTER)).scalars().all()
    if len(rows) < LOCK_AFTER:
        return None
    until = rows[-1] + WINDOW      # the 5th most recent failure ages out first
    return max(int((until - now).total_seconds()) + 1, 1) if until > now else None
