"""Bank feed connectors: HOW statements arrive. Parsing (what is in them) is the statement profile's job, so a new bank needs
configuration, not code. Shipped: folder drop (landing zones, file shares), native SFTP polling (host-to-host, host key pinned) and
generic REST/JSON. Bank-specific APIs are listed as planned and refuse to run. Account Aggregator is not built: it needs a licensed
financial-information user registration that software alone cannot provide."""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import io
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from ..config import check_egress, egress_allowed, settings
from ..money import parse_amount
from ..storage import clean_key
from .statements import RawTxn, StatementError, parse_date


class FeedError(ValueError):
    pass


@dataclass
class FetchedFile:
    name: str
    data: bytes
    on_success: Callable[[], None] | None = None


FEED_TYPES = {
    "folder_drop": ("Folder drop (SFTP landing zone / file share)", "available"),
    "rest_json": ("Generic REST/JSON statement API", "available"),
    "sftp_native": ("SFTP host-to-host polling (pinned host key)", "available"),
    "bank_specific_api": ("Bank-specific corporate APIs (HDFC/ICICI/Axis/...)", "planned"),
}


def validate_feed(cfg: dict) -> list[str]:
    t = cfg.get("type")
    if not t:
        return []
    if t not in FEED_TYPES:
        return [f"unknown feed type '{t}'"]
    if FEED_TYPES[t][1] != "available":
        return [f"feed type '{t}' is planned, not implemented"]
    errs = []
    if t == "folder_drop" and not cfg.get("path"):
        errs.append("folder_drop needs 'path' (a folder under the feed root / a key prefix in the bucket)")
    if t == "sftp_native":
        for k in ("host", "username", "remote_dir", "host_key_sha256"):
            if not cfg.get(k):
                errs.append(f"sftp_native needs '{k}'" + (" (the server's key fingerprint, SHA256:..., from your bank: connections to any other key are refused)" if k == "host_key_sha256" else ""))
        if cfg.get("host_key_sha256") and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", str(cfg["host_key_sha256"])):
            errs.append("host_key_sha256 must look like SHA256:<43 base64 characters> (as printed by ssh-keygen -lf)")
        a = cfg.get("auth") or {}
        if not (a.get("password_env") or a.get("key_env")):
            errs.append("sftp_native needs auth.password_env or auth.key_env (names of environment variables; secrets are never stored in the configuration)")
        for k in ("password_env", "key_env", "key_passphrase_env"):
            if a.get(k) and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(a[k])):
                errs.append(f"auth.{k} must be an environment variable name")
        if cfg.get("host") and not egress_allowed(str(cfg["host"])):
            errs.append("sftp host is not in ALLOWED_EGRESS_HOSTS (data-residency strict mode)")
    if t == "rest_json":
        for k in ("url", "records_path", "map"):
            if not cfg.get(k):
                errs.append(f"rest_json needs '{k}'")
        m = cfg.get("map") or {}
        for k in ("date", "amount", "narration"):
            if k not in m:
                errs.append(f"rest_json map needs '{k}'")
        if cfg.get("url") and not str(cfg["url"]).startswith("https://"):
            errs.append("url must be https://")
    return errs


def fetch_folder_drop(cfg: dict, store=None) -> list[FetchedFile]:
    """Files in <feeds>/<path>/ (direct children only, not processed/). The 'feeds' area is a local directory (FEED_ROOT) or an
    object-storage prefix, depending on STORAGE_BACKEND: a bank's SFTP/H2H landing zone can simply be a bucket."""
    import fnmatch

    from ..storage import StorageError, get_store

    try:
        store = store or get_store("feeds", settings)
        base = clean_key(cfg["path"].strip("/"))
        objs = store.list(base)
    except StorageError as e:
        raise FeedError(("feed path escapes the feed root" if "invalid storage key" in str(e) or "escapes" in str(e) else f"feed storage error: {e}"))
    out = []
    for o in objs:
        rel = o.key[len(base) + 1:]
        if "/" in rel or not fnmatch.fnmatch(rel, cfg.get("pattern", "*")) or o.size > 25 * 1024 * 1024:
            continue
        try:
            data = store.get(o.key)
        except StorageError as e:
            raise FeedError(f"could not read {o.key}: {e}")

        def done(key=o.key, name=rel):
            store.move(key, f"{base}/processed/{datetime.utcnow():%Y%m%dT%H%M%S}_{name}")

        out.append(FetchedFile(rel, data, done if cfg.get("archive", True) else None))
    return out


def _fingerprint(key) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def _load_key(pem: str, pw: str | None):
    import paramiko

    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(pem), password=pw)
        except Exception:
            continue
    raise FeedError("the SFTP private key could not be read")


def _connect(cfg: dict):
    """An authenticated transport. The server's host key must match the pinned fingerprint BEFORE any credential is sent."""
    import paramiko

    check_egress(cfg["host"])
    a = cfg.get("auth") or {}
    try:
        t = paramiko.Transport((cfg["host"], int(cfg.get("port", 22))))
        t.banner_timeout = t.auth_timeout = 20
        t.start_client(timeout=20)
    except Exception as e:
        raise FeedError(f"could not connect to the SFTP server: {type(e).__name__}: {e}")
    try:
        got = _fingerprint(t.get_remote_server_key())
        if not hmac.compare_digest(got.encode(), cfg["host_key_sha256"].encode()):
            raise FeedError(f"SFTP host key mismatch (server presented {got}); refusing to send credentials. If the bank rotated its key, confirm the new "
                            "fingerprint with them and update the configuration.")
        if a.get("key_env"):
            pem = os.environ.get(a["key_env"])
            if not pem:
                raise FeedError(f"environment variable {a['key_env']} is not set")
            t.auth_publickey(cfg["username"], _load_key(pem, os.environ.get(a.get("key_passphrase_env") or "") or None))
        else:
            pw = os.environ.get(a.get("password_env") or "")
            if not pw:
                raise FeedError(f"environment variable {a.get('password_env')} is not set")
            t.auth_password(cfg["username"], pw)
        return t
    except paramiko.AuthenticationException:
        t.close()
        raise FeedError("the SFTP server refused the credentials")
    except Exception:
        t.close()
        raise


def fetch_sftp(cfg: dict) -> list[FetchedFile]:
    """Poll an SFTP directory for statement files (direct children matching the pattern)."""
    import paramiko
    import stat as _st

    t = _connect(cfg)
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        base = cfg["remote_dir"].rstrip("/") or "/"
        out: list[FetchedFile] = []
        for att in sorted(sftp.listdir_attr(base), key=lambda x: x.filename):
            if not _st.S_ISREG(att.st_mode or 0) or not fnmatch.fnmatch(att.filename, cfg.get("pattern", "*")) or (att.st_size or 0) > 25 * 1024 * 1024:
                continue
            path = f"{base}/{att.filename}".replace("//", "/")
            with sftp.open(path, "rb") as fh:
                data = fh.read()
            arch = cfg.get("archive_dir")

            def done(path=path, name=att.filename, arch=arch):
                t2 = _connect(cfg)       # a fresh session: the polling one is closed as soon as the files are read, so a stalled bank cannot hold it open
                try:
                    paramiko.SFTPClient.from_transport(t2).rename(path, f"{arch.rstrip('/')}/{datetime.utcnow():%Y%m%dT%H%M%S}_{name}")
                finally:
                    t2.close()

            out.append(FetchedFile(att.filename, data, done if arch else None))
        return out
    finally:
        t.close()


def _dig(obj, path: str):
    for part in path.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj.get(part) if isinstance(obj, dict) else None
        if obj is None:
            return None
    return obj


def fetch_rest_json(cfg: dict, since: date, transport: Callable | None = None) -> list[RawTxn]:
    """Generic JSON statement API. Credentials come from an environment variable NAMED in the config, never from the config."""
    check_egress(cfg["url"])
    headers = {"Accept": "application/json"}
    auth = cfg.get("auth") or {}
    if auth.get("token_env"):
        tok = os.environ.get(auth["token_env"])
        if not tok:
            raise FeedError(f"environment variable {auth['token_env']} is not set")
        headers[auth.get("header", "Authorization")] = f"{auth.get('scheme', 'Bearer')} {tok}".strip()
    params = {k: str(v).replace("{since}", since.isoformat()).replace("{today}", date.today().isoformat()) for k, v in (cfg.get("params") or {}).items()}
    if transport:
        payload = transport(cfg["url"], headers, params)
    else:
        import httpx

        r = httpx.get(cfg["url"], headers=headers, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()
    records = _dig(payload, cfg["records_path"])
    if not isinstance(records, list):
        raise FeedError(f"records_path '{cfg['records_path']}' did not resolve to a list")
    m, out = cfg["map"], []
    credit_vals = {str(v).upper() for v in cfg.get("credit_values", ["CR", "CREDIT", "C"])}
    for rec in records:
        d = parse_date(str(_dig(rec, m["date"]) or ""), cfg.get("date_format"))
        raw_amt = _dig(rec, m["amount"])
        amt = parse_amount(str(raw_amt)) if raw_amt is not None else None
        if d is None or not amt:
            continue
        if m.get("direction"):
            direction = "credit" if str(_dig(rec, m["direction"]) or "").upper() in credit_vals else "debit"
        else:
            direction = "credit" if amt > 0 else "debit"
        bal = _dig(rec, m["balance"]) if m.get("balance") else None
        out.append(RawTxn(txn_date=d, amount_minor=abs(amt), direction=direction, narration=str(_dig(rec, m["narration"]) or ""),
                          bank_ref=str(_dig(rec, m["ref"])) if m.get("ref") and _dig(rec, m["ref"]) else None,
                          balance_minor=parse_amount(str(bal)) if bal is not None else None, currency=str(cfg.get("currency", "INR")).upper()))
    return out


def sync_bank(db, bank, actor: str, actor_id: int | None, transport: Callable | None = None) -> dict:
    from . import service as ing

    cfg = bank.feed or {}
    t = cfg.get("type")
    if not t:
        raise FeedError("No feed configured for this bank account")
    if FEED_TYPES.get(t, ("", ""))[1] != "available":
        raise FeedError(f"Feed type '{t}' is planned, not implemented")
    results = []
    if t == "folder_drop":
        for f in fetch_folder_drop(cfg):
            try:
                results.append({"file": f.name, **ing.ingest_statement(db, bank, f.name, f.data, actor, actor_id, gateway=bank.is_gateway)})
            except StatementError as e:
                results.append({"file": f.name, "error": str(e)})   # leave the file in place so someone can fix the profile
                continue
            if f.on_success:
                f.on_success()
    elif t == "sftp_native":
        for f in fetch_sftp(cfg):
            try:
                results.append({"file": f.name, **ing.ingest_statement(db, bank, f.name, f.data, actor, actor_id, gateway=bank.is_gateway)})
            except StatementError as e:
                results.append({"file": f.name, "error": str(e)})
                continue
            if f.on_success:
                try:
                    f.on_success()
                except Exception as e:           # the file is already ingested and deduplicated; failing to archive must not lose that fact
                    results[-1]["archive_warning"] = f"could not move to the archive folder: {e}"
    elif t == "rest_json":
        since = date.today() - timedelta(days=int(cfg.get("lookback_days", 7)))
        raws = fetch_rest_json(cfg, since, transport)
        res = ing.persist_raw(db, bank, raws, "rest_json", f"api:{cfg['url'][:60]}", f"api{date.today():%Y%m%d}", actor, actor_id, bank.is_gateway, bank.profile)
        results.append({"file": "api", **res})
    return {"feed": t, "results": results, "inserted": sum(r.get("inserted", 0) for r in results), "errors": [r for r in results if "error" in r]}
