"""ERP connector layer. The platform speaks ONE canonical journal; connectors only render it for a target ERP.

Writing a new connector = implement `render()` (and optionally `parse_open_items`). Nothing in matching, workflow or
posting changes. Shipped here: generic line-level CSV and canonical JSON.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Protocol

from ..models import LegalEntity, PostingBatch
from .types import Rendered  # noqa: F401  (re-exported for existing importers)


class ERPConnector(Protocol):
    name: str
    label: str
    status: str  # available | planned

    def render(self, batch: PostingBatch, entity: LegalEntity) -> Rendered: ...


def _vouchers(batch: PostingBatch) -> dict[str, list]:
    out: dict[str, list] = {}
    for ln in batch.lines:
        out.setdefault(ln.voucher_no, []).append(ln)
    return out


class GenericCsvConnector:
    name, label, status = "generic_csv", "Generic line-level CSV", "available"

    def render(self, batch: PostingBatch, entity: LegalEntity) -> Rendered:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["voucher_no", "posting_date", "line_type", "account", "party_code", "invoice_ref", "debit", "credit", "currency", "exchange_rate", "base_debit", "base_credit", "narration"])
        for ln in batch.lines:
            w.writerow([ln.voucher_no, ln.posting_date.isoformat(), ln.line_type, ln.account, getattr(ln, "_party", "") or "",
                        getattr(ln, "_inv", "") or "", f"{ln.debit_minor / 100:.2f}" if ln.debit_minor else "",
                        f"{ln.credit_minor / 100:.2f}" if ln.credit_minor else "", ln.currency,
                        f"{ln.rate_micro / 1e6:.6f}" if ln.rate_micro else "", f"{ln.base_debit_minor / 100:.2f}" if ln.base_debit_minor else "",
                        f"{ln.base_credit_minor / 100:.2f}" if ln.base_credit_minor else "", ln.narration])
        return Rendered(f"cashapp_batch_{batch.id}.csv", buf.getvalue().encode(), "text/csv")


class CanonicalJsonConnector:
    name, label, status = "canonical_json", "Canonical JSON (voucher + settlement references)", "available"

    def render(self, batch: PostingBatch, entity: LegalEntity) -> Rendered:
        vouchers = []
        for vno, lines in _vouchers(batch).items():
            dr = sum(x.base_debit_minor for x in lines)
            cr = sum(x.base_credit_minor for x in lines)
            vouchers.append({
                "voucher_no": vno, "date": lines[0].posting_date.isoformat(), "base_currency": entity.base_currency, "total_debit": dr / 100,
                "total_credit": cr / 100, "balanced": dr == cr,
                "lines": [{"type": x.line_type, "account": x.account, "party": getattr(x, "_party", None), "invoice": getattr(x, "_inv", None),
                           "currency": x.currency, "exchange_rate": (x.rate_micro / 1e6) if x.rate_micro else None, "debit": x.debit_minor / 100,
                           "credit": x.credit_minor / 100, "base_debit": x.base_debit_minor / 100, "base_credit": x.base_credit_minor / 100, "narration": x.narration} for x in lines],
            })
        doc = {"schema": "cashapp.journal/1", "batch_id": batch.id, "idempotency_key": batch.idempotency_key, "legal_entity": entity.name,
               "gstin": entity.gstin, "vouchers": vouchers}
        return Rendered(f"cashapp_batch_{batch.id}.json", json.dumps(doc, indent=2).encode(), "application/json")


class PlannedConnector:
    status = "planned"

    def __init__(self, name: str, label: str):
        self.name, self.label = name, label

    def render(self, batch, entity):  # pragma: no cover - guarded by the API
        raise NotImplementedError(f"The {self.label} connector is planned but not implemented yet")


REGISTRY: dict[str, ERPConnector] = {c.name: c for c in [
    GenericCsvConnector(), CanonicalJsonConnector(),
    PlannedConnector("zoho_books", "Zoho Books (REST)"),
]}


def _register_native() -> None:
    from .erp_native import NATIVE

    REGISTRY.update(NATIVE)


_register_native()
