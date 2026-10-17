"""Per-run matching context: indexes entity data once so each transaction is cheap to evaluate."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..indian import normalise_name, normalise_ref
from ..models import (DEFAULT_ENTITY_CONFIG, Customer, CustomerIdentifier, CustomerProfile, CustomerTrust, Invoice, LegalEntity,
                      MatchProposal, ProposalStatus, ToleranceRule, utcnow)
from .scoring import ensure_model, shadow_model


@dataclass
class Ctx:
    db: Session
    entity: LegalEntity
    cfg: dict
    weights: dict
    model_version: str
    customers: dict[int, Customer]
    cust_norm: dict[int, set[str]]                 # customer -> normalised names (master + learned)
    ident: dict[tuple[str, str], int]              # (kind,value) -> customer_id for learned account/vpa/name
    profiles: dict[int, CustomerProfile]
    open_by_cust: dict[int, list[Invoice]]
    by_norm: dict[str, list[Invoice]]              # normalised invoice number -> invoices (all, incl. closed)
    by_irn: dict[str, Invoice]
    by_po: dict[str, list[Invoice]]
    tolerances: list[ToleranceRule]
    reserved: dict[int, set[int]] = field(default_factory=lambda: defaultdict(set))  # invoice_id -> txn_ids pending
    other_entity_names: dict[int, str] = field(default_factory=dict)
    trust: dict[int, CustomerTrust] = field(default_factory=dict)
    cum_before: dict[int, int] = field(default_factory=dict)     # invoice_id -> taxable value invoiced earlier in the same FY (194Q customers only)
    shadow_weights: dict | None = None
    shadow_version: str | None = None

    def tolerance(self, customer_id: int | None, currency: str, amount: int) -> int:
        """Most specific rule wins: customer > currency > entity default. tol = abs, or max(abs, pct) if pct set."""
        best, rank = None, -1
        for r in self.tolerances:
            if r.customer_id and r.customer_id == customer_id:
                rk = 3
            elif r.customer_id:
                continue
            elif r.currency and r.currency == currency:
                rk = 2
            elif not r.currency:
                rk = 1
            else:
                continue
            if rk > rank:
                best, rank = r, rk
        if not best:
            return 100  # ₹1 default rounding allowance
        pct = abs(amount) * best.pct_bp // 10000
        return max(best.abs_minor, pct) if best.pct_bp else best.abs_minor


def build_ctx(db: Session, entity: LegalEntity) -> Ctx:
    cfg = {**DEFAULT_ENTITY_CONFIG, **(entity.config or {})}
    model = ensure_model(db)
    customers = {c.id: c for c in db.execute(select(Customer).where(Customer.entity_id == entity.id)).scalars()}
    cust_norm: dict[int, set[str]] = defaultdict(set)
    ident: dict[tuple[str, str], int] = {}
    for c in customers.values():
        cust_norm[c.id].add(normalise_name(c.name))
    for i in db.execute(select(CustomerIdentifier).where(CustomerIdentifier.entity_id == entity.id)).scalars():
        if i.kind == "name":
            cust_norm[i.customer_id].add(i.value)
        ident[(i.kind, i.value)] = i.customer_id
    profiles = {p.customer_id: p for p in db.execute(select(CustomerProfile)).scalars() if p.customer_id in customers}
    cutoff = utcnow().date() - timedelta(days=400)
    invs = db.execute(select(Invoice).where(Invoice.entity_id == entity.id)).scalars().all()
    open_by_cust: dict[int, list[Invoice]] = defaultdict(list)
    by_norm: dict[str, list[Invoice]] = defaultdict(list)
    by_irn, by_po = {}, defaultdict(list)
    for inv in invs:
        if inv.open_minor != 0:
            open_by_cust[inv.customer_id].append(inv)
        elif inv.invoice_date < cutoff:
            continue
        by_norm[normalise_ref(inv.number)].append(inv)
        if inv.irn:
            by_irn[inv.irn.upper()] = inv
        if inv.po_ref:
            by_po[normalise_ref(inv.po_ref)].append(inv)
    for lst in open_by_cust.values():
        lst.sort(key=lambda i: (i.due_date, i.invoice_date, i.id))
    ctx = Ctx(db=db, entity=entity, cfg=cfg, weights=model.weights, model_version=model.version, customers=customers,
              cust_norm=cust_norm, ident=ident, profiles=profiles, open_by_cust=open_by_cust, by_norm=by_norm,
              by_irn=by_irn, by_po=by_po,
              tolerances=list(db.execute(select(ToleranceRule).where(ToleranceRule.entity_id == entity.id)).scalars()))
    for p in db.execute(select(MatchProposal).where(MatchProposal.entity_id == entity.id,
                                                    MatchProposal.status == ProposalStatus.pending_review)).scalars():
        for a in p.allocations:
            ctx.reserved[a.invoice_id].add(p.txn_id)
    from collections import defaultdict as _dd

    from ..indian import financial_year

    run: dict[tuple[int, str], int] = _dd(int)
    for inv in sorted((i for i in invs if i.doc_type != "credit_note" and i.total_minor > 0 and customers.get(i.customer_id)
                       and customers[i.customer_id].tds_section == "194Q"), key=lambda i: (i.invoice_date, i.id)):
        k = (inv.customer_id, financial_year(inv.invoice_date)[0])
        ctx.cum_before[inv.id] = run[k]
        run[k] += inv.taxable_minor
    ctx.trust = {t.customer_id: t for t in db.execute(select(CustomerTrust).where(CustomerTrust.entity_id == entity.id)).scalars()}
    sm = shadow_model(db)
    if sm:
        ctx.shadow_weights, ctx.shadow_version = sm.weights, sm.version
    ctx.other_entity_names = {e.id: normalise_name(e.name) for e in db.execute(select(LegalEntity)).scalars() if e.id != entity.id}
    return ctx
