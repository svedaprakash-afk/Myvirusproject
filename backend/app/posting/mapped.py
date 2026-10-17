"""Configuration-driven ERP connectors. Any ERP that can import a flat file or accept an HTTPS call can be integrated by
configuration (column mapping / webhook), with no code change. The canonical journal underneath never changes."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
from typing import Callable

from ..config import check_egress
from ..models import LegalEntity, PostingBatch
from .connectors import Rendered

SOURCES = {"voucher_no", "date", "line_type", "account", "party_code", "invoice_ref", "debit", "credit", "amount", "signed_amount", "drcr", "currency",
           "exchange_rate", "base_debit", "base_credit", "base_amount", "narration", "batch_id", "entity_name", "entity_gstin"}


def validate_export(cfg: dict) -> list[str]:
    t = cfg.get("type")
    if not t:
        return []
    errs: list[str] = []
    if t == "mapped_csv":
        cols = cfg.get("columns")
        if not isinstance(cols, list) or not cols:
            return ["mapped_csv needs a non-empty 'columns' list"]
        for i, c in enumerate(cols):
            if not c.get("header"):
                errs.append(f"column {i + 1}: 'header' required")
            if c.get("source") not in SOURCES and "const" not in c:
                errs.append(f"column {i + 1}: 'source' must be one of {sorted(SOURCES)} or give a 'const'")
        if cfg.get("delimiter") and len(cfg["delimiter"]) != 1:
            errs.append("delimiter must be one character")
    elif t == "webhook":
        if not str(cfg.get("url", "")).startswith("https://"):
            errs.append("webhook url must be https://")
        if not cfg.get("secret_env"):
            errs.append("webhook needs 'secret_env' (name of the environment variable holding the HMAC secret)")
    else:
        errs.append(f"unknown export type '{t}' (use mapped_csv or webhook)")
    return errs


def _fmt_date(d, f: str) -> str:
    return d.strftime(f)


def _num(minor: int, dec: str) -> str:
    s = f"{minor / 100:.2f}"
    return s.replace(".", dec) if dec != "." else s


class MappedCsvConnector:
    name, label, status = "mapped_csv", "Configured CSV (column-mapped)", "available"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def render(self, batch: PostingBatch, entity: LegalEntity) -> Rendered:
        c = self.cfg
        dec, dfmt = c.get("decimal", "."), c.get("date_format", "%d-%m-%Y")
        base = c.get("amounts", "document")            # document | base
        amap = c.get("account_map") or {}
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=c.get("delimiter", ","))
        if c.get("header", True):
            w.writerow([col["header"] for col in c["columns"]])
        for ln in batch.lines:
            dr, cr = (ln.base_debit_minor, ln.base_credit_minor) if base == "base" else (ln.debit_minor, ln.credit_minor)
            vals = {
                "voucher_no": ln.voucher_no, "date": _fmt_date(ln.posting_date, dfmt), "line_type": ln.line_type, "account": amap.get(ln.account, ln.account),
                "party_code": getattr(ln, "_party", "") or "", "invoice_ref": getattr(ln, "_inv", "") or "", "debit": _num(dr, dec) if dr else "",
                "credit": _num(cr, dec) if cr else "", "amount": _num(dr or cr, dec), "signed_amount": _num(dr - cr, dec), "drcr": "Dr" if dr else "Cr",
                "currency": ln.currency if base != "base" else entity.base_currency, "exchange_rate": f"{ln.rate_micro / 1e6:.6f}".replace(".", dec) if ln.rate_micro else "",
                "base_debit": _num(ln.base_debit_minor, dec) if ln.base_debit_minor else "", "base_credit": _num(ln.base_credit_minor, dec) if ln.base_credit_minor else "",
                "base_amount": _num(ln.base_debit_minor or ln.base_credit_minor, dec), "narration": ln.narration, "batch_id": batch.id,
                "entity_name": entity.name, "entity_gstin": entity.gstin or "",
            }
            w.writerow([col["const"] if "const" in col else vals[col["source"]] for col in c["columns"]])
        return Rendered(f"cashapp_batch_{batch.id}.csv", buf.getvalue().encode("utf-8"), "text/csv")


class WebhookConnector:
    """POSTs the canonical journal (HMAC-SHA256 signed, idempotency-keyed) to the ERP/middleware. A 2xx JSON reply of
    {"documents": {voucher_no: erp_doc_no}} acknowledges the batch automatically."""

    name, label, status = "webhook", "Signed webhook / REST", "available"

    def __init__(self, cfg: dict, transport: Callable | None = None):
        self.cfg, self.transport = cfg, transport

    def render(self, batch: PostingBatch, entity: LegalEntity) -> Rendered:
        from .connectors import CanonicalJsonConnector

        return CanonicalJsonConnector().render(batch, entity)

    def deliver(self, batch: PostingBatch, entity: LegalEntity) -> dict[str, str] | None:
        check_egress(self.cfg["url"])
        secret = os.environ.get(self.cfg["secret_env"])
        if not secret:
            raise ValueError(f"environment variable {self.cfg['secret_env']} is not set")
        body = self.render(batch, entity).content
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Signature": f"sha256={sig}", "Idempotency-Key": batch.idempotency_key, **(self.cfg.get("headers") or {})}
        if self.transport:
            status, payload = self.transport(self.cfg["url"], body, headers)
        else:
            import httpx

            r = httpx.post(self.cfg["url"], content=body, headers=headers, timeout=float(self.cfg.get("timeout", 20)))
            status, payload = r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})
        if not 200 <= status < 300:
            raise ValueError(f"ERP endpoint returned HTTP {status}")
        docs = (payload or {}).get("documents")
        return {str(k): str(v) for k, v in docs.items()} if isinstance(docs, dict) else None


def resolve(entity: LegalEntity, name: str, transport: Callable | None = None):
    from .connectors import REGISTRY

    cfg = ((entity.config or {}).get("integrations") or {}).get("erp_export") or {}
    if name == "mapped_csv" and cfg.get("type") == "mapped_csv" and not validate_export(cfg):
        return MappedCsvConnector(cfg)
    if name == "webhook" and cfg.get("type") == "webhook" and not validate_export(cfg):
        return WebhookConnector(cfg, transport)
    c = REGISTRY.get(name)
    if c is not None and hasattr(c, "with_options"):
        return c.with_options(((entity.config or {}).get("integrations") or {}).get("erp_options", {}).get(name))
    return c


def available(entity: LegalEntity) -> list[dict]:
    from .connectors import REGISTRY

    opts = ((entity.config or {}).get("integrations") or {}).get("erp_options") or {}
    out = [{"name": c.name, "label": c.label, "status": ("available" if not hasattr(c, "required") or not c.validate(opts.get(c.name) or {}) else "needs options"),
            **({"unverified": True} if getattr(c, "unverified", False) else {})} if c.status == "available" else {"name": c.name, "label": c.label, "status": c.status}
           for c in REGISTRY.values()]
    cfg = ((entity.config or {}).get("integrations") or {}).get("erp_export") or {}
    for t, label in (("mapped_csv", MappedCsvConnector.label), ("webhook", WebhookConnector.label)):
        out.append({"name": t, "label": label, "status": "available" if cfg.get("type") == t and not validate_export(cfg) else "not configured"})
    return out
