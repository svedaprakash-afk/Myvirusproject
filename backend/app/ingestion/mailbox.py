"""Remittance inbox connector (IMAP) + raw .eml intake.

Security model for inbound mail (it is attacker-controlled input that can influence matching):
  * sender domain allow-list (optional) and SPF/DKIM verdict from the receiving server's Authentication-Results header;
  * mail failing the authentication policy is QUARANTINED, not extracted, until a human releases it;
  * attachments are size/type/count limited; PDFs/XLSX are only text-extracted, never executed;
  * extracted values are only *evidence* for the scorer and every reference must resolve to a real open item;
  * polling is idempotent on Message-ID.
Credentials are read from an environment variable NAMED in the config, never stored.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import os
import re
from email import policy
from email.utils import parseaddr
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import check_egress
from ..models import RemittanceDocument
from . import service as ing
from .remittance import get_extractor, text_from_upload

ALLOWED_ATTACH = (".pdf", ".xlsx", ".xlsm", ".csv", ".txt", ".eml")
MAX_ATTACH, MAX_BYTES = 5, 10 * 1024 * 1024


class MailboxError(ValueError):
    pass


def validate_mailbox(cfg: dict) -> list[str]:
    if not cfg:
        return []
    errs = [f"'{k}' is required" for k in ("host", "username", "password_env") if not cfg.get(k)]
    if cfg.get("port") and not isinstance(cfg["port"], int):
        errs.append("port must be an integer")
    return errs


def auth_verdict(msg) -> str:
    """Summarise the receiving MTA's SPF/DKIM/DMARC verdict. We trust only what OUR server stamped, never the sender's claims."""
    hdr = " ".join(str(h) for h in msg.get_all("Authentication-Results", []))
    v = {k: (m.group(1).lower() if (m := re.search(rf"\b{k}=(\w+)", hdr, re.I)) else "none") for k in ("dkim", "spf", "dmarc")}
    return f"dkim={v['dkim']};spf={v['spf']};dmarc={v['dmarc']}"


def _auth_ok(verdict: str) -> bool:
    return "dmarc=pass" in verdict or "dkim=pass" in verdict


def _domain_ok(addr: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    d = addr.rsplit("@", 1)[-1].lower()
    return any(d == a.lower() or d.endswith("." + a.lower()) for a in allowed)


def message_text(msg) -> str:
    parts = [f"From: {msg.get('From', '')}", f"Subject: {msg.get('Subject', '')}", f"Date: {msg.get('Date', '')}"]
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    ptxt = plain.get_content() if plain is not None else ""
    htxt = ""
    if html is not None:
        t = html.get_content()
        t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
        t = re.sub(r"</(p|div|tr|br|li)>|<br\s*/?>", "\n", t, flags=re.I)
        t = re.sub(r"</t[dh]>", "  ", t, flags=re.I)
        htxt = re.sub(r"[ \t]+", " ", re.sub(r"<[^>]+>", " ", t))
    # Some senders ship a stub plain part ("view in browser") with the real remittance table only in the HTML.
    digits = lambda x: sum(c.isdigit() for c in x)
    parts.append(htxt if not ptxt.strip() or digits(htxt) > 2 * digits(ptxt) + 5 else ptxt)
    n = 0
    for att in msg.iter_attachments():
        name = att.get_filename() or ""
        if not name.lower().endswith(ALLOWED_ATTACH) or n >= MAX_ATTACH:
            continue
        data = att.get_payload(decode=True) or b""
        if len(data) > MAX_BYTES:
            continue
        try:
            parts.append(f"--- attachment: {name} ---\n" + text_from_upload(name, data))
            n += 1
        except Exception:
            parts.append(f"--- attachment: {name} (unreadable) ---")
    return "\n".join(parts)


def process_email(db: Session, entity_id: int, raw: bytes, cfg: dict | None, actor: str, actor_id: int | None = None, force: bool = False) -> dict:
    cfg = cfg or {}
    msg = email.message_from_bytes(raw, policy=policy.default)
    mid = (msg.get("Message-ID") or "").strip() or "sha:" + hashlib.sha256(raw).hexdigest()[:32]
    if len(mid) > 200:
        mid = "sha:" + hashlib.sha256(mid.encode()).hexdigest()
    if db.execute(select(RemittanceDocument.id).where(RemittanceDocument.entity_id == entity_id, RemittanceDocument.source_ref == mid)).first():
        return {"status": "duplicate", "message_id": mid}
    sender = parseaddr(msg.get("From", ""))[1].lower()
    verdict = auth_verdict(msg)
    reasons = []
    if not _domain_ok(sender, cfg.get("allowed_domains") or []):
        reasons.append(f"sender domain not in allow-list")
    if cfg.get("require_auth_pass", True) and not _auth_ok(verdict):
        reasons.append(f"sender not authenticated ({verdict})")
    text = message_text(msg)
    if reasons and not force:
        doc = RemittanceDocument(entity_id=entity_id, source="email", filename=(msg.get("Subject") or "")[:200], raw_text=text[:20000],
                                 extracted={"quarantined": True, "reasons": reasons}, extractor="quarantined", source_ref=mid, sender=sender, auth_result=verdict)
        db.add(doc)
        db.flush()
        audit.log(db, actor, "remittance.quarantined", "RemittanceDocument", doc.id, entity_id, {"sender": sender, "reasons": reasons}, actor_id)
        return {"status": "quarantined", "id": doc.id, "reasons": reasons, "message_id": mid}
    doc = ing.ingest_remittance(db, entity_id, text, (msg.get("Subject") or "")[:200], "email", actor, actor_id)
    doc.source_ref, doc.sender, doc.auth_result = mid, sender, verdict
    db.flush()
    return {"status": "ingested", "id": doc.id, "linked_txn_id": doc.linked_txn_id, "lines": len(doc.extracted.get("lines", [])), "message_id": mid}


def release_quarantined(db: Session, doc: RemittanceDocument, actor: str, actor_id: int | None) -> RemittanceDocument:
    if doc.extractor != "quarantined":
        raise MailboxError("Not quarantined")
    ex = get_extractor()
    doc.extracted, doc.extractor = ex.extract(doc.raw_text), ex.name
    ing.link_remittance(db, doc)
    audit.log(db, actor, "remittance.released", "RemittanceDocument", doc.id, doc.entity_id, {"sender": doc.sender}, actor_id)
    return doc


def poll_mailbox(db: Session, entity_id: int, cfg: dict, actor: str, actor_id: int | None = None, imap_factory: Callable | None = None) -> dict:
    errs = validate_mailbox(cfg)
    if errs:
        raise MailboxError("; ".join(errs))
    check_egress(cfg["host"])
    pw = os.environ.get(cfg["password_env"])
    if not pw:
        raise MailboxError(f"environment variable {cfg['password_env']} is not set")
    M = (imap_factory or (lambda h, p: imaplib.IMAP4_SSL(h, p)))(cfg["host"], int(cfg.get("port", 993)))
    out = {"ingested": 0, "quarantined": 0, "duplicates": 0, "errors": []}
    try:
        M.login(cfg["username"], pw)
        M.select(cfg.get("folder", "INBOX"))
        typ, data = M.search(None, "UNSEEN")
        ids = (data[0].split() if data and data[0] else [])[: int(cfg.get("max_per_poll", 50))]
        for i in ids:
            try:
                typ, parts = M.fetch(i, "(BODY.PEEK[])")
                raw = next(p[1] for p in parts if isinstance(p, tuple))
                res = process_email(db, entity_id, raw, cfg, actor, actor_id)
                out[{"ingested": "ingested", "quarantined": "quarantined", "duplicate": "duplicates"}[res["status"]]] += 1
                M.store(i, "+FLAGS", "\\Seen")           # only after it is safely stored
                if cfg.get("processed_folder"):
                    M.copy(i, cfg["processed_folder"])
            except Exception as e:  # one bad mail must not stop the poll; it stays UNSEEN for a retry / human look
                out["errors"].append(f"message {i.decode() if isinstance(i, bytes) else i}: {e}")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    audit.log(db, actor, "mailbox.polled", "LegalEntity", entity_id, entity_id, {k: (v if k != "errors" else len(v)) for k, v in out.items()}, actor_id)
    return out
