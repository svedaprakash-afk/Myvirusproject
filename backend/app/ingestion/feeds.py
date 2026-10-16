"""Bank feed connectors: HOW statements arrive. Parsing (what is in them) is the statement profile's job, so a new bank needs
configuration, not code. Shipped: folder drop (SFTP/host-to-host landing zones, file shares) and generic REST/JSON.
Bank-specific APIs and native SFTP polling are listed as planned and refuse to run."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from ..config import check_egress, settings
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
    "sftp_native": ("Native SFTP polling", "planned"),
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
    elif t == "rest_json":
        since = date.today() - timedelta(days=int(cfg.get("lookback_days", 7)))
        raws = fetch_rest_json(cfg, since, transport)
        res = ing.persist_raw(db, bank, raws, "rest_json", f"api:{cfg['url'][:60]}", f"api{date.today():%Y%m%d}", actor, actor_id, bank.is_gateway, bank.profile)
        results.append({"file": "api", **res})
    return {"feed": t, "results": results, "inserted": sum(r.get("inserted", 0) for r in results), "errors": [r for r in results if "error" in r]}
