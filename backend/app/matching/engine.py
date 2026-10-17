"""Matching engine: customer identification -> evidence gathering -> candidate allocations -> scoring -> guardrails.

Design rules that make this auditable:
  * Confidence is a transparent sum of named contributions (see scoring.py).
  * Auto-approval needs BOTH a high score AND every hard guardrail to pass; guardrails are plain code, not model output.
  * The engine never posts anything. It proposes; `actions.py` applies; `posting` exports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from math import log10

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..indian import normalise_name
from ..models import (Customer, ExceptionCase, ExceptionStatus, ExceptionType, Invoice, LegalEntity, MatchAllocation,
                      MatchProposal, PaymentTransaction, ProposalStatus, RemittanceDocument, TxnStatus, utcnow)
from ..money import from_minor
from . import actions
from .context import Ctx, build_ctx
from .identify import Guess, identify, is_ambiguous, name_similarity
from .references import find_references, strip_ids
from . import advances, trust
from .scoring import contributions, predict
from .subset import find_subsets
from .tds import TdsScenario, scenarios_for, tds_on as _tds_on

REF_STRONG = {"exact", "irn", "remittance"}


@dataclass
class Alloc:
    invoice: Invoice
    applied: int
    tds: int = 0
    writeoff: int = 0
    tds_bp: int | None = None
    tds_section: str | None = None
    hit: str | None = None


@dataclass
class Cand:
    customer_id: int | None
    allocs: list[Alloc]
    strategy: str
    feats: dict
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unapplied: int = 0
    short: int = 0
    kind: str = "exact"
    prob: float = 0.0
    n_competitors: int = 0
    tds_scenario: TdsScenario | None = None
    writeoff_kind: str = "tolerance"          # tolerance | bank_charge
    settle_currency: str | None = None        # set when invoices are in a different currency than the receipt
    settle_rate_micro: int | None = None
    fx_estimated: bool = False                # foreign amount inferred from the rate table rather than stated by the payer

    @property
    def inv_ids(self) -> frozenset[int]:
        return frozenset(a.invoice.id for a in self.allocs)

    @property
    def tds(self) -> int:
        return sum(a.tds for a in self.allocs)

    @property
    def writeoff(self) -> int:
        return sum(a.writeoff for a in self.allocs)

    @property
    def applied(self) -> int:
        return sum(a.applied for a in self.allocs)


_SYM = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "CN¥", "HKD": "HK$", "AUD": "A$", "CAD": "C$", "SGD": "S$"}


def inr(m: int, ccy: str = "INR") -> str:
    """Format minor units with the right symbol (name kept for existing callers; pass the currency for anything that is not base)."""
    return f"{'−' if m < 0 else ''}{_SYM.get(ccy, ccy + ' ')}{abs(from_minor(m)):,.2f}"


# ---------------------------------------------------------------------------------------------- allocation
def allocate_set(ctx: Ctx, cust: Customer | None, invs: list[Invoice], pay: int, tol: int, hits: dict[int, str], max_charge: int = 0) -> Cand:
    """Decide how `pay` clears this set of invoices: exact, within tolerance, TDS-explained, partial or over."""
    invs = sorted(invs, key=lambda i: (i.due_date, i.id))
    total_open = sum(i.open_minor for i in invs)
    diff = pay - total_open
    allocs = [Alloc(i, i.open_minor, hit=hits.get(i.id)) for i in invs]
    cur = invs[0].currency if invs else ctx.entity.base_currency
    domestic = all(i.currency == ctx.entity.base_currency for i in invs)   # Indian TDS is only withheld on base-currency (domestic) invoices
    c = Cand(cust.id if cust else None, allocs, "", {})

    def settle_residual(resid: int, note_kind: str):
        # resid = pay - expected; fold into write-off on the largest invoice (negative = rounding gain)
        tgt = max(allocs, key=lambda a: abs(a.invoice.open_minor))
        tgt.writeoff = -resid
        tgt.applied -= tgt.writeoff
        c.kind = note_kind

    if abs(diff) <= tol:
        c.kind = "exact"
        if diff:
            settle_residual(diff, "tolerance")
            c.evidence.append(f"Difference of {inr(abs(diff), cur)} is within the {inr(tol, cur)} tolerance and will be written off")
        else:
            c.evidence.append(f"Amount {inr(pay, cur)} equals the open balance exactly")
    elif diff < 0:
        best = None
        if cust and domestic:
            for sc in scenarios_for(cust, ctx.cfg):
                tds_list = [tds_on(ctx, a.invoice, sc) for a in allocs]
                T = sum(tds_list)
                if T <= 0:
                    continue
                resid = pay - (total_open - T)
                # TDS is often rounded to whole rupees by the payer: allow ~₹0.50 per invoice, never the full business
                # tolerance — otherwise tolerance x scenarios x subsets produces coincidental "explanations".
                if abs(resid) <= min(tol, 50 * len(allocs) + 50):
                    key = (not sc.usual, abs(resid))
                    if best is None or key < best[0]:
                        best = (key, sc, tds_list, resid)
        if best:
            _, sc, tds_list, resid = best
            for a, t in zip(allocs, tds_list):
                a.tds, a.tds_bp, a.tds_section = t, sc.rate_bp, sc.section
                a.applied = a.invoice.open_minor - t
            if resid:
                settle_residual(resid, "tds")
            c.kind, c.tds_scenario = "tds", sc
            c.evidence.append(
                f"Shortfall of {inr(-diff, cur)} equals {sc.rate_bp / 100:g}% TDS on {'taxable value' if sc.base == 'taxable' else 'invoice total'}"
                f" ({sc.section or 'TDS'}){' — customer\'s usual rate' if sc.usual else ' — NOT this customer\'s usual rate'}")
        elif 0 < -diff <= max_charge:
            # foreign receipts routinely arrive net of intermediary/correspondent bank charges; book them as charges, not write-offs
            settle_residual(diff, "charge")
            c.writeoff_kind = "bank_charge"
            c.evidence.append(f"Shortfall of {inr(-diff, cur)} looks like intermediary bank charges on a foreign receipt (limit {inr(max_charge, cur)})")
        else:
            remaining = pay
            for a in allocs:
                a.applied = max(min(remaining, a.invoice.open_minor), 0) if a.invoice.open_minor > 0 else a.invoice.open_minor
                remaining -= a.applied
            c.kind, c.short = "partial", total_open - pay
            c.warnings.append(f"Short by {inr(-diff, cur)} and not explained by TDS or tolerance")
    else:
        c.kind, c.unapplied = "over", diff
        c.warnings.append(f"Payment exceeds the open balance of these invoices by {inr(diff, cur)}")
    return c


def balance_cash(c: Cand, pay: int) -> None:
    """Invariant: sum(applied) + unapplied == cash received. Any residue (<= tolerance) becomes a write-off on the largest
    invoice so each invoice's cleared amount is unchanged and the journal always balances."""
    diff = pay - (c.applied + c.unapplied)
    if diff and c.allocs:
        tgt = max(c.allocs, key=lambda a: abs(a.invoice.open_minor))
        tgt.applied += diff
        tgt.writeoff -= diff


def charge_allowance(ctx: Ctx, txn) -> int:
    """Intermediary/correspondent charges are a roughly constant amount in the BASE currency (default ₹5,000 ≈ $60); express that limit in the
    receipt's currency via the rate table so ¥ and $ are treated alike. No rate => no allowance (never guess)."""
    from ..fx import from_base, rate_on

    base = ctx.entity.base_currency
    if txn.currency == base:
        return 0
    r = rate_on(ctx.db, txn.currency, txn.txn_date, base)
    return from_base(int(ctx.cfg.get("foreign_bank_charge_max_base_minor", 500_000)), r) if r else 0


def base_feats(ctx: Ctx, txn: PaymentTransaction, cust: Customer | None, guess: Guess | None, c: Cand) -> dict:
    pay = txn.receivable_minor
    n = max(len(c.allocs), 1)
    hits = [a.hit for a in c.allocs]
    f = {
        "customer_conf": guess.score if guess else 0.0,
        "name_agree": guess.name_sim if guess else 0.0,
        "ref_full": sum(h in ("exact", "irn") for h in hits) / n,
        "ref_serial": sum(h in ("serial", "po") for h in hits) / n,
        "remit": sum(h == "remittance" for h in hits) / n,
        "amount_exact": 1.0 if c.kind == "exact" or (c.kind == "tds" and c.writeoff == 0) else 0.0,
        "amount_tol": 1.0 if c.kind in ("tolerance", "charge") or (c.kind == "tds" and c.writeoff) else 0.0,
        "tds_explained": 1.0 if c.kind == "tds" else 0.0,
        "tds_usual": 1.0 if c.tds_scenario and c.tds_scenario.usual else 0.0,
        # unapplied cash is only suspicious when nothing independent (a reference) anchors the allocation
        "unexplained": (min(1.0, c.unapplied / pay) if pay else 0.0) * (1 - max(sum(h in ("exact", "irn") for h in hits) / n, sum(h == "remittance" for h in hits) / n)),
        "bundle_size": min(1.0, (n - 1) / 5),
        "fx_estimated": 1.0 if c.fx_estimated else 0.0,
        "partial_pay": 1.0 if c.kind == "partial" else 0.0,
    }
    if cust and c.allocs:
        pool = ctx.open_by_cust.get(cust.id, [])
        first_k = {i.id for i in pool[: len(c.allocs)]}
        f["fifo"] = sum(a.invoice.id in first_k for a in c.allocs) / n
        prof = ctx.profiles.get(cust.id)
        if prof and prof.payments >= 3:
            multi = len(c.allocs) > 1
            rate = prof.multi_invoice_payments / prof.payments
            f["history"] = (rate if multi else 1 - rate) * 2 - 1
            if c.kind == "tds":
                f["history"] = (f["history"] + (prof.tds_payments / prof.payments * 2 - 1)) / 2
        earliest = min(a.invoice.invoice_date for a in c.allocs)
        f["date_anomaly"] = 1.0 if txn.txn_date < earliest else 0.0
    return f


def tds_on(ctx: Ctx, inv: Invoice, sc: TdsScenario) -> int:
    return _tds_on(inv, sc, cum_before=ctx.cum_before.get(inv.id, 0))


def finish(ctx: Ctx, c: Cand) -> Cand:
    c.feats["ambiguity"] = min(1.0, 0.5 * c.n_competitors)
    c.prob = predict(ctx.weights, c.feats)
    return c


# ---------------------------------------------------------------------------------------------- candidate builders
def ref_candidate(ctx: Ctx, txn, cust: Customer, guess: Guess, hits: dict[int, str]) -> Cand | None:
    invs = [i for i in ctx.open_by_cust.get(cust.id, []) if i.id in hits and not i.disputed and i.currency == txn.currency]
    disputed = [i for i in ctx.open_by_cust.get(cust.id, []) if i.id in hits and i.disputed and i.currency == txn.currency]
    if not invs and not disputed:
        return None
    use = invs + disputed  # disputed invoices are included so the guardrail can block auto-approval and flag it
    tol = ctx.tolerance(cust.id, txn.currency, txn.receivable_minor)
    c = allocate_set(ctx, cust, use, txn.receivable_minor, tol, hits, charge_allowance(ctx, txn))
    c.strategy = "reference"
    kinds = sorted({hits[i.id] for i in use})
    c.evidence.insert(0, f"{', '.join(i.number for i in use)} referenced in payment text ({'/'.join(kinds)})")
    if disputed:
        c.warnings.append(f"Invoice {', '.join(i.number for i in disputed)} is under dispute")
    c.feats = base_feats(ctx, txn, cust, guess, c)
    return c


def remittance_candidate(ctx: Ctx, txn, cust: Customer | None, guess: Guess | None, doc: RemittanceDocument) -> Cand | None:
    lines = (doc.extracted or {}).get("lines") or []
    if not lines:
        return None
    from ..indian import normalise_ref

    resolved: list[tuple[dict, Invoice]] = []
    for ln in lines:
        key = normalise_ref(ln["ref"])
        pool = [i for i in ctx.by_norm.get(key, []) if i.open_minor != 0 and i.currency == txn.currency]
        if cust:
            pool = [i for i in pool if i.customer_id == cust.id]
        if len(pool) == 1:
            resolved.append((ln, pool[0]))
    if not resolved:
        return None
    if cust is None:
        cust = ctx.customers[resolved[0][1].customer_id]
    resolved = [(ln, i) for ln, i in resolved if i.customer_id == cust.id]
    hits = {i.id: "remittance" for _, i in resolved}
    tol = ctx.tolerance(cust.id, txn.currency, txn.receivable_minor)
    c = allocate_set(ctx, cust, [i for _, i in resolved], txn.receivable_minor, tol, hits)
    # If the customer itself stated per-invoice paid amounts and TDS, trust that split when it sums to the payment.
    stated = [(ln.get("amount_minor"), ln.get("tds_minor") or 0, i) for ln, i in resolved]
    if all(a is not None for a, _, _ in stated) and abs(sum(a for a, _, _ in stated) - txn.receivable_minor) <= tol:
        c.allocs = []
        for a, t, inv in stated:
            if inv.open_minor > 0 and a + t > inv.open_minor + tol:
                continue
            al = Alloc(inv, a, tds=t, hit="remittance")
            if t:
                al.tds_bp = int(round(t * 10000 / (inv.taxable_minor or inv.total_minor)))
            c.allocs.append(al)
            short = inv.open_minor - a - t
            if abs(short) > tol and short > 0:
                c.short += short
        if len(c.allocs) == len(stated):
            balance_cash(c, txn.receivable_minor)
            c.kind = "exact" if not c.short else "partial"
            c.evidence.insert(0, "Customer's remittance advice states the per-invoice split, which sums to the bank credit")
            if c.tds:
                usual = cust.tds_rate_bp
                c.tds_scenario = TdsScenario(0, "taxable", None, bool(usual and abs(c.tds * 10000 // max(sum(a.invoice.taxable_minor for a in c.allocs), 1) - usual) <= 5))
    else:
        c.evidence.insert(0, f"{len(resolved)} invoice(s) named in the remittance advice")
    c.strategy = "remittance_advice"
    c.feats = base_feats(ctx, txn, cust, guess, c)
    return c


def amount_candidates(ctx: Ctx, txn, cust: Customer, guess: Guess) -> list[Cand]:
    pool = [i for i in ctx.open_by_cust.get(cust.id, []) if i.open_minor > 0 and not i.disputed and i.currency == txn.currency]
    if not pool:
        return []
    pay = txn.receivable_minor
    tol = ctx.tolerance(cust.id, txn.currency, pay)
    out: list[Cand] = []
    seen: set[frozenset[int]] = set()
    cfg_max = int(ctx.cfg.get("max_subset_size", 8))
    pool = pool[:40]
    for sc in [None, *scenarios_for(cust, ctx.cfg)]:
        vals = [(i.id, i.open_minor - (tds_on(ctx, i, sc) if sc else 0)) for i in pool]
        size = cfg_max if (sc is None or sc.usual) else min(cfg_max, 3)   # exotic TDS rates only for small sets
        t_tol = tol if sc is None else min(tol, 50 * size + 50)
        for keys, _ in find_subsets(vals, pay, t_tol, max_size=size):
            fs = frozenset(keys)
            if fs in seen:
                continue
            seen.add(fs)
            chosen = [i for i in pool if i.id in fs]
            c = allocate_set(ctx, cust, chosen, pay, tol, {}, charge_allowance(ctx, txn))
            if c.kind in ("exact", "tolerance", "tds", "charge"):
                c.strategy = "amount_combination"
                c.evidence.insert(0, f"Combination of {len(chosen)} open invoice(s) ({', '.join(i.number for i in chosen)}) sums to the payment")
                c.feats = base_feats(ctx, txn, cust, guess, c)
                out.append(c)
        if len(out) >= 8:
            break
    return out


def cross_candidates(ctx: Ctx, txn, cust: Customer, guess: Guess, hits: dict[int, str]) -> list[Cand]:
    """Receipt in base currency (INR) for invoice(s) in a foreign currency. The payer's stated foreign amount (from the bank's
    inward-remittance narration) is exact evidence; otherwise it is estimated from the rate table, which is weaker evidence."""
    from ..fx import from_base, rate_on

    base = ctx.entity.base_currency
    if txn.currency != base:
        return []
    foreign = [i for i in ctx.open_by_cust.get(cust.id, []) if i.open_minor > 0 and i.currency != base and not i.disputed]
    out: list[Cand] = []
    pay = txn.receivable_minor
    spread = int(ctx.cfg.get("fx_spread_bp", 150))
    for ccy in sorted({i.currency for i in foreign}):
        pool = [i for i in foreign if i.currency == ccy]
        exact = txn.fx_minor if txn.fx_ccy == ccy and txn.fx_minor else None
        rate = rate_on(ctx.db, ccy, txn.txn_date, base)
        if exact is None and rate is None:
            continue
        target = exact if exact is not None else from_base(pay, rate)
        tol = ctx.tolerance(cust.id, ccy, target)
        if exact is None:
            tol = max(tol, target * spread // 10000)
        sets: list[list[Invoice]] = []
        refd = [i for i in pool if i.id in hits]
        if refd:
            sets.append(refd)
        for keys, _ in find_subsets([(i.id, i.open_minor) for i in pool], target, tol, max_size=int(ctx.cfg.get("max_subset_size", 8))):
            chosen = [i for i in pool if i.id in set(keys)]
            if all({x.id for x in chosen} != {y.id for y in s} for s in sets):
                sets.append(chosen)
        for chosen in sets[:4]:
            total = sum(i.open_minor for i in chosen)
            if abs(target - total) > tol:
                continue
            c = Cand(cust.id, [Alloc(i, i.open_minor, hit=hits.get(i.id)) for i in sorted(chosen, key=lambda i: (i.due_date, i.id))], "cross_currency", {})
            c.kind = "exact" if exact is not None and total == exact else "tolerance"
            c.settle_currency, c.settle_rate_micro, c.fx_estimated = ccy, int(pay * 1_000_000 / total), exact is None
            c.evidence.append((f"Payer's remittance states {ccy} {exact / 100:,.2f}" if exact is not None else
                               f"{ccy} {total / 100:,.2f} at the {txn.txn_date} rate ({rate / 1e6:.4f}) ≈ the {inr(pay)} credited, within {spread / 100:g}%")
                              + f"; implied rate {c.settle_rate_micro / 1e6:.4f}")
            if rate and abs(c.settle_rate_micro - rate) * 10000 > rate * spread:
                c.warnings.append(f"Implied rate {c.settle_rate_micro / 1e6:.4f} is {abs(c.settle_rate_micro - rate) * 100 / rate:.1f}% away from the rate table ({rate / 1e6:.4f})")
            c.feats = base_feats(ctx, txn, cust, guess, c)
            c.feats["amount_exact"], c.feats["amount_tol"] = (1.0, 0.0) if c.kind == "exact" else (0.0, 1.0)
            out.append(c)
    return out


def fifo_candidate(ctx: Ctx, txn, cust: Customer, guess: Guess) -> Cand | None:
    pool = [i for i in ctx.open_by_cust.get(cust.id, []) if i.open_minor > 0 and not i.disputed and i.currency == txn.currency]
    if not pool:
        return None
    pay, take, run = txn.receivable_minor, [], 0
    for i in pool:
        if run >= pay:
            break
        take.append(i)
        run += i.open_minor
    c = allocate_set(ctx, cust, take, pay, ctx.tolerance(cust.id, txn.currency, pay), {})
    c.strategy = "fifo_guess"
    c.evidence.insert(0, "No reference or exact amount found — applying to the oldest open invoices (a guess for a human to confirm)")
    skipped = [i.number for i in ctx.open_by_cust.get(cust.id, []) if i.open_minor > 0 and i.disputed and i.currency == txn.currency]
    if skipped:
        c.evidence.append(f"Disputed invoice(s) skipped so cash is not applied against something the customer is contesting: {', '.join(skipped)}")
    c.feats = base_feats(ctx, txn, cust, guess, c)
    return c


# ---------------------------------------------------------------------------------------------- guardrails
def guardrails(ctx: Ctx, txn: PaymentTransaction, c: Cand, guess: Guess | None, ambiguous: bool, dup_suspect: bool) -> list[str]:
    """Hard controls independent of the model. Any entry blocks straight-through processing."""
    g: list[str] = []
    cfg = ctx.cfg
    if c.prob < cfg["auto_approve_threshold"]:
        g.append(f"Confidence {c.prob:.0%} below auto-approve threshold {cfg['auto_approve_threshold']:.0%}")
    if not guess or guess.score < 0.85:
        g.append("Payer not identified with high certainty")
    if ambiguous:
        g.append("More than one customer is a plausible payer")
    if guess and txn.payer_name and guess.name_sim < 0.6 and guess.score < 0.97:
        g.append(f"Payer name '{txn.payer_name}' does not resemble the customer — possible third-party payment")
    if c.n_competitors:
        g.append("More than one invoice combination fits this payment")
    if txn.receivable_minor > cfg["auto_approve_max_minor"]:
        g.append(f"Amount above the {inr(cfg['auto_approve_max_minor'])} auto-approval ceiling")
    if dup_suspect:
        g.append("Possible duplicate payment")
    if any(a.invoice.disputed for a in c.allocs):
        g.append("Includes a disputed invoice")
    if c.unapplied or c.short:
        g.append("Leaves an unapplied or short-paid residual that needs a decision")
    if c.strategy == "fifo_guess":
        g.append("Oldest-first guess, not evidence-based")
    if c.strategy == "cross_currency":
        if c.fx_estimated:
            g.append("Foreign amount estimated from the rate table, not stated by the payer")
        elif not cfg.get("auto_approve_cross_currency", False):
            g.append("Cross-currency settlement (realised FX) requires confirmation")
    elif any(a.invoice.currency != txn.currency for a in c.allocs):
        g.append("Currency differs from invoice currency")
    if any(a.invoice.open_minor < 0 and a.hit not in REF_STRONG for a in c.allocs):
        g.append("Credit note applied without being referenced")
    if c.kind == "tds" and not (c.tds_scenario and c.tds_scenario.usual):
        g.append("TDS rate differs from this customer's usual rate")
    if any(len(ctx.reserved.get(a.invoice.id, set()) - {txn.id}) for a in c.allocs):
        g.append("An invoice is already proposed for another receipt")
    if txn.channel == "CASH":
        g.append("Cash deposit requires manual confirmation")
    if not cfg.get("auto_approve_enabled", True):
        g.append("Straight-through approval is switched off for this entity (suggest-only mode)")
    if c.strategy in ("amount_combination", "amount_inference") and not cfg.get("auto_approve_amount_only", False):
        g.append("Matched on amount alone — no invoice reference; amount-only auto-approval is disabled")
    g.extend(trust.gate(cfg, ctx.trust.get(c.customer_id) if c.customer_id else None, c.prob))
    return g


# ---------------------------------------------------------------------------------------------- orchestration
def sla_due(ctx: Ctx):
    return utcnow() + timedelta(hours=ctx.cfg.get("sla_hours", 48))


def priority(ctx: Ctx, amount_minor: int, cust: Customer | None, age_days: int, bonus: float = 0.0) -> float:
    tier = {"A": 15, "B": 8, "C": 0}.get(cust.tier if cust else "B", 5)
    return round(10 * log10(max(abs(amount_minor) / 100, 1)) + 1.5 * age_days + tier + bonus, 2)


def open_exception(ctx: Ctx, txn: PaymentTransaction | None, etype: ExceptionType, amount: int, reason: str,
                   cust: Customer | None = None, suggestions: list | None = None, proposal: MatchProposal | None = None,
                   invoice: Invoice | None = None, bonus: float = 0.0) -> ExceptionCase:
    q = select(ExceptionCase).where(ExceptionCase.entity_id == ctx.entity.id, ExceptionCase.type == etype,
                                    ExceptionCase.status.in_([ExceptionStatus.open, ExceptionStatus.in_progress, ExceptionStatus.pending_approval]))
    if txn:
        q = q.where(ExceptionCase.txn_id == txn.id)
    if invoice:
        q = q.where(ExceptionCase.invoice_id == invoice.id)
    existing = ctx.db.execute(q).scalars().first()
    age = (utcnow().date() - txn.txn_date).days if txn else 0
    if existing:
        existing.amount_minor, existing.reason, existing.suggestions = amount, reason, suggestions or []
        existing.proposal_id = proposal.id if proposal else existing.proposal_id
        return existing
    ex = ExceptionCase(entity_id=ctx.entity.id, type=etype, txn_id=txn.id if txn else None, invoice_id=invoice.id if invoice else None,
                       proposal_id=proposal.id if proposal else None, customer_id=cust.id if cust else None, amount_minor=amount,
                       reason=reason, suggestions=suggestions or [], priority=priority(ctx, amount, cust, age, bonus), due_at=sla_due(ctx))
    ctx.db.add(ex)
    ctx.db.flush()
    audit.log(ctx.db, "engine", "exception.opened", "ExceptionCase", ex.id, ctx.entity.id, {"type": etype.value, "amount_minor": amount, "txn": txn.id if txn else None})
    return ex


def _supersede_open_proposals(db: Session, txn: PaymentTransaction):
    for p in db.execute(select(MatchProposal).where(MatchProposal.txn_id == txn.id, MatchProposal.status == ProposalStatus.pending_review)).scalars():
        p.status = ProposalStatus.superseded


def process_transaction(ctx: Ctx, txn: PaymentTransaction) -> dict:
    db = ctx.db
    kind = (txn.extra or {}).get("kind", "customer")
    pay = txn.receivable_minor

    # --- non-receivable credits -----------------------------------------------------------------
    if kind in ("interest", "charges", "reversal"):
        txn.status = TxnStatus.exception
        open_exception(ctx, txn, ExceptionType.non_ar_credit, pay,
                       f"Bank credit looks like {kind} rather than a customer receipt: '{txn.narration[:80]}'",
                       suggestions=[{"action": "classify_non_ar", "label": f"Post to {'interest income' if kind == 'interest' else 'bank charges reversal / other income'}"}])
        return {"outcome": "exception", "type": "non_ar_credit"}

    # --- intercompany ------------------------------------------------------------------------------
    pn = normalise_name(txn.payer_name)
    if pn:
        for eid, en in ctx.other_entity_names.items():
            if en and name_similarity(pn, en) >= 0.92:
                txn.status = TxnStatus.exception
                open_exception(ctx, txn, ExceptionType.intercompany, pay, f"Payer '{txn.payer_name}' matches group entity (id {eid}); not a customer receipt",
                               suggestions=[{"action": "classify_interco", "label": "Post to intercompany ledger"}])
                return {"outcome": "exception", "type": "intercompany"}

    # --- duplicate by identical UTR ------------------------------------------------------------------
    dup_suspect = False
    if txn.utr and txn.channel != "GATEWAY":
        twin = db.execute(select(PaymentTransaction).where(
            PaymentTransaction.entity_id == txn.entity_id, PaymentTransaction.utr == txn.utr, PaymentTransaction.id != txn.id,
            PaymentTransaction.direction == "credit", PaymentTransaction.id < txn.id)).scalars().first()
        if twin:  # only the *later* line is the duplicate; the original must stay matchable
            txn.status = TxnStatus.exception
            open_exception(ctx, txn, ExceptionType.duplicate_payment, pay,
                           f"Same UTR {txn.utr} already appears on receipt #{twin.id} ({inr(twin.amount_minor, twin.currency)} on {twin.txn_date}); likely a duplicate statement line",
                           suggestions=[{"action": "mark_duplicate", "label": "Mark as duplicate statement line (no posting)"}])
            return {"outcome": "exception", "type": "duplicate_payment"}

    doc: RemittanceDocument | None = db.get(RemittanceDocument, txn.remittance_id) if txn.remittance_id else None
    remit_text = doc.raw_text if doc else ""
    text = strip_ids(f"{txn.narration} {remit_text}", txn.utr, txn.payer_vpa)

    exact_hits = find_references(ctx, text)
    for ln in ((doc.extracted or {}).get("lines") or []) if doc else []:
        from ..indian import normalise_ref

        for inv in ctx.by_norm.get(normalise_ref(ln["ref"]), []):
            exact_hits[inv.id] = "remittance" if exact_hits.get(inv.id) in (None, "exact") else exact_hits[inv.id]
    all_invs = {i.id: i for lst in ctx.by_norm.values() for i in lst}
    ref_customers: dict[int, str] = {}
    for iid, h in exact_hits.items():
        if h in ("exact", "irn", "remittance"):
            ref_customers[all_invs[iid].customer_id] = "exact"

    guesses = identify(ctx, txn, remit_text, (doc.extracted or {}).get("payer_name") if doc else None, ref_customers,
                       doc.sender if doc else None)
    plausible = [g for g in guesses if g.score >= 0.5][:3]
    serial_hits = find_references(ctx, text, [g.customer_id for g in plausible]) if plausible else {}
    hits = {**serial_hits, **exact_hits}
    ambiguous = is_ambiguous(guesses)

    # --- duplicate by already-settled invoice -----------------------------------------------------------
    referenced = [all_invs[i] for i, h in hits.items() if h in ("exact", "irn", "remittance") and i in all_invs]
    if referenced and all(i.open_minor == 0 for i in referenced) and _amount_repeats_settled(ctx, txn, referenced):
        txn.status = TxnStatus.exception
        c0 = ctx.customers.get(referenced[0].customer_id)
        open_exception(ctx, txn, ExceptionType.duplicate_payment, pay,
                       f"Payment references {', '.join(i.number for i in referenced)} which is already fully settled — possible duplicate payment",
                       cust=c0, suggestions=[{"action": "refund", "label": "Refund customer"}, {"action": "on_account", "label": "Hold on account for future invoices"}],
                       bonus=10)
        return {"outcome": "exception", "type": "duplicate_payment"}

    # --- candidate generation ---------------------------------------------------------------------------
    cands: list[Cand] = []
    for g in plausible[:2] if ambiguous else plausible[:1]:
        cust = ctx.customers[g.customer_id]
        cust_hits = {iid: h for iid, h in hits.items() if iid in all_invs and all_invs[iid].customer_id == cust.id}
        if doc:
            rc = remittance_candidate(ctx, txn, cust, g, doc)
            if rc:
                cands.append(rc)
        rf = ref_candidate(ctx, txn, cust, g, cust_hits) if cust_hits else None
        if rf:
            cands.append(rf)
        if not rf or rf.kind not in ("exact", "tolerance", "tds", "charge"):
            cands += amount_candidates(ctx, txn, cust, g)
        cands += cross_candidates(ctx, txn, cust, g, cust_hits)
        if not cands:
            fc = fifo_candidate(ctx, txn, cust, g)
            if fc:
                cands.append(fc)

    if not plausible:
        cands += _global_amount_inference(ctx, txn)

    # --- scoring, ambiguity, selection ------------------------------------------------------------------
    for c in cands:
        c.feats["unique"] = 1.0
        c.n_competitors = 0
        finish(ctx, c)
    cands.sort(key=lambda c: (-c.prob, len(c.allocs)))
    if cands:
        cands = _resolve_competition(ctx, cands)

    best = cands[0] if cands else None
    guess_for_best = next((g for g in guesses if best and g.customer_id == best.customer_id), None)

    if best is None or best.prob < ctx.cfg["review_threshold"]:
        return _raise_unmatched(ctx, txn, guesses, best, hits)

    return _propose(ctx, txn, best, guess_for_best, ambiguous and len(plausible) > 1, dup_suspect, cands)


def _amount_repeats_settled(ctx: Ctx, txn: PaymentTransaction, settled: list[Invoice]) -> bool:
    """A reference to a settled invoice is a duplicate only if the amount also repeats that invoice (optionally net of TDS).
    Otherwise it is more likely a mistyped reference, which the normal candidate flow handles (with a warning)."""
    pay = txn.receivable_minor
    if any(i.currency != txn.currency for i in settled):
        return False   # cannot judge an amount repeat across currencies; fall through to normal candidates
    cust = ctx.customers.get(settled[0].customer_id)
    tol = ctx.tolerance(cust.id if cust else None, txn.currency, pay)
    total = sum(i.total_minor for i in settled)
    if abs(total - pay) <= tol:
        return True
    from ..money import pct_of

    for sc in scenarios_for(cust):
        t = sum(pct_of(i.taxable_minor if sc.base == "taxable" else i.total_minor, sc.rate_bp) for i in settled if i.total_minor > 0)
        if t and abs(total - t - pay) <= tol:
            return True
    return False


EXACT_KINDS = ("exact", "tolerance", "tds", "charge")


def _resolve_competition(ctx: Ctx, cands: list[Cand]) -> list[Cand]:
    """Penalise the leader when comparable alternatives exist, and rank by evidence strength when evidence conflicts.

    * Rivals are alternatives with different invoices, scoring within 0.15 of the leader, that fully explain the payment.
    * Conflicting evidence: the reference says A (but the amount does not fit A) while the payment is paisa-exact for B.
      A coincidental exact match is far rarer than a mistyped reference, so B ranks first — but never auto-approves.
    """
    review = ctx.cfg["review_threshold"]
    top = cands[0]
    rivals = [o for o in cands[1:] if o.inv_ids != top.inv_ids and o.prob >= top.prob - 0.15 and o.kind in EXACT_KINDS and o.prob >= review]
    conflict = next((o for o in cands[1:] if o.inv_ids != top.inv_ids and o.customer_id == top.customer_id and o.kind in EXACT_KINDS
                     and o.prob >= review), None)
    if conflict and top.strategy in ("reference", "remittance_advice") and top.kind in ("partial", "over"):
        lead, follow = conflict, top
    elif rivals:
        lead, follow = top, rivals[0]
    else:
        return cands
    group = [lead] + [o for o in {id(x): x for x in [follow, *rivals]}.values() if o is not lead]
    for c in group:
        c.n_competitors = len(group) - 1
        if c.strategy in ("amount_combination", "amount_inference", "cross_currency"):
            c.feats["unique"] = 0.0
        finish(ctx, c)
    names = ", ".join(a.invoice.number for a in follow.allocs)
    lead.warnings.append(f"Conflicting or competing evidence: {names} also fits ({follow.strategy.replace('_', ' ')})")
    rest = sorted((c for c in cands if c is not lead), key=lambda c: (-c.prob, len(c.allocs)))
    # the lead keeps first place when it won on evidence strength, otherwise ordinary score order applies
    return [lead, *rest] if lead is conflict else sorted(cands, key=lambda c: (-c.prob, len(c.allocs)))


def _global_amount_inference(ctx: Ctx, txn: PaymentTransaction) -> list[Cand]:
    pay = txn.receivable_minor
    found: list[tuple[Customer, Invoice]] = []
    for cid, invs in ctx.open_by_cust.items():
        cust = ctx.customers.get(cid)
        tol = ctx.tolerance(cid, txn.currency, pay)
        for i in invs:
            if i.open_minor > 0 and i.currency == txn.currency and abs(i.open_minor - pay) <= tol and not i.disputed:
                found.append((cust, i))
    if len(found) != 1:
        return []
    cust, inv = found[0]
    g = Guess(cust.id, 0.55, ["Only open invoice in the system with exactly this amount"], 0.0)
    c = allocate_set(ctx, cust, [inv], pay, ctx.tolerance(cust.id, txn.currency, pay), {})
    c.strategy = "amount_inference"
    c.evidence.insert(0, f"{inv.number} ({cust.name}) is the only open invoice with this exact amount — payer is inferred, not confirmed")
    c.feats = base_feats(ctx, txn, cust, g, c)
    c.feats["unique"] = 1.0
    return [c]


def _raise_unmatched(ctx: Ctx, txn: PaymentTransaction, guesses: list[Guess], best: Cand | None, hits: dict) -> dict:
    top = [g for g in guesses if g.score >= 0.5][:3]
    identified = top[0] if top and top[0].score >= 0.7 else None
    suggestions = [{"action": "link_customer", "customer_id": g.customer_id, "label": f"Payer may be {ctx.customers[g.customer_id].name}",
                    "score": round(g.score, 2)} for g in top]
    cust = ctx.customers[identified.customer_id] if identified else None
    txn.status = TxnStatus.exception
    txn.customer_id, txn.customer_confidence = (cust.id, identified.score) if cust else (None, None)
    if cust:
        etype = ExceptionType.on_account
        reason = (f"Payer appears to be {cust.name} but no invoice reference or amount match was found"
                  + (f" (best guess confidence {best.prob:.0%}, below the review threshold)" if best else ""))
        if best:
            suggestions.append({"action": "apply_fifo", "label": "Apply to oldest open invoices"})
        suggestions.append({"action": "on_account", "label": "Hold as unapplied advance"})
    else:
        etype = ExceptionType.unidentified_receipt
        reason = f"Could not identify the payer from '{txn.payer_name or txn.narration[:60]}'"
        suggestions.append({"action": "request_remittance", "label": "Ask treasury/sales for the payer's remittance advice"})
    ex = open_exception(ctx, txn, etype, txn.receivable_minor, reason, cust, suggestions, bonus=5 if not cust else 0)
    audit.log(ctx.db, "engine", "txn.unmatched", "PaymentTransaction", txn.id, ctx.entity.id, {"exception": ex.id, "type": etype.value})
    return {"outcome": "exception", "type": etype.value}


def _propose(ctx: Ctx, txn: PaymentTransaction, c: Cand, guess: Guess | None, ambiguous: bool, dup: bool, all_c: list[Cand]) -> dict:
    db = ctx.db
    _supersede_open_proposals(db, txn)
    g = guardrails(ctx, txn, c, guess, ambiguous, dup)
    auto = not g
    contribs = contributions(ctx.weights, c.feats)
    warnings = list(c.warnings)
    if guess and guess.reasons:
        evidence = [*c.evidence, *dict.fromkeys(guess.reasons)]
    else:
        evidence = list(c.evidence)
    tol_total = c.writeoff
    p = MatchProposal(
        entity_id=ctx.entity.id, txn_id=txn.id, customer_id=c.customer_id, status=ProposalStatus.auto_approved if auto else ProposalStatus.pending_review,
        strategy=c.strategy, confidence=round(c.prob, 4), model_version=ctx.model_version,
        shadow_confidence=round(predict(ctx.shadow_weights, c.feats), 4) if ctx.shadow_weights else None, shadow_model_version=ctx.shadow_version,
        explanation={"summary": _summary(txn, c), "evidence": evidence, "warnings": warnings, "contributions": contribs,
                     "alternatives": [{"invoices": [a.invoice.number for a in o.allocs], "confidence": round(o.prob, 3), "strategy": o.strategy}
                                      for o in all_c if o is not c][:3]},
        features={k: round(v, 4) for k, v in c.feats.items()}, guardrails=g,
        applied_minor=c.applied, tds_minor=c.tds, writeoff_minor=tol_total, fee_minor=txn.fee_minor, unapplied_minor=c.unapplied,
        short_minor=c.short, created_by=f"engine:{ctx.model_version}", auto=auto,
        settle_currency=c.settle_currency, settle_rate_micro=c.settle_rate_micro, writeoff_kind=c.writeoff_kind)
    for a in c.allocs:
        p.allocations.append(MatchAllocation(invoice_id=a.invoice.id, applied_minor=a.applied, tds_minor=a.tds, writeoff_minor=a.writeoff,
                                             tds_section=a.tds_section, tds_rate_bp=a.tds_bp, reference_hit=a.hit))
    db.add(p)
    db.flush()
    txn.customer_id = c.customer_id
    txn.customer_confidence = round(guess.score, 3) if guess else None
    audit.log(db, "engine", "proposal.created", "MatchProposal", p.id, ctx.entity.id,
              {"txn": txn.id, "confidence": p.confidence, "strategy": c.strategy, "auto": auto, "guardrails": g, "model": ctx.model_version})
    if auto:
        actions.apply_proposal(db, ctx, p, actor="engine", actor_id=None, auto=True)
    else:
        txn.status = TxnStatus.review
        for a in c.allocs:
            ctx.reserved[a.invoice.id].add(txn.id)
    return {"outcome": "auto" if auto else "review", "proposal": p.id, "confidence": p.confidence}


def _summary(txn: PaymentTransaction, c: Cand) -> str:
    nums = ", ".join(a.invoice.number for a in c.allocs)
    cc = txn.currency
    bits = [f"Apply {inr(txn.receivable_minor, cc)} to {nums}"]
    if c.tds:
        bits.append(f"with {inr(c.tds, cc)} TDS credit")
    if c.writeoff:
        bits.append(f"and {inr(abs(c.writeoff), cc)} tolerance {'write-off' if c.writeoff > 0 else 'gain'}")
    if c.unapplied:
        bits.append(f"leaving {inr(c.unapplied, cc)} unapplied")
    if c.short:
        bits.append(f"leaving {inr(c.short, cc)} short-paid")
    return " ".join(bits)


# ---------------------------------------------------------------------------------------------- public API
def run_matching(db: Session, entity: LegalEntity, txn_ids: list[int] | None = None, actor: str = "engine",
                 actor_id: int | None = None) -> dict:
    ctx = build_ctx(db, entity)
    q = select(PaymentTransaction).where(PaymentTransaction.entity_id == entity.id, PaymentTransaction.direction == "credit",
                                         PaymentTransaction.status.in_([TxnStatus.new, TxnStatus.exception] if not txn_ids else list(TxnStatus)))
    if txn_ids:
        q = q.where(PaymentTransaction.id.in_(txn_ids))
    txns = db.execute(q.order_by(PaymentTransaction.txn_date, PaymentTransaction.id)).scalars().all()
    # sweep stale open exceptions of the txns we are re-running; they will be re-raised if still unmatched
    summary = {"processed": 0, "auto": 0, "review": 0, "exception": 0}
    for t in txns:
        if t.status in (TxnStatus.matched, TxnStatus.posted, TxnStatus.partially_matched):
            continue
        res = process_transaction(ctx, t)
        summary["processed"] += 1
        summary[res["outcome"]] += 1
        _close_stale_exceptions(ctx, t, res)
    audit.log(db, actor, "matching.run", "LegalEntity", entity.id, entity.id, {**summary, "model": ctx.model_version}, actor_id)
    db.flush()
    summary["advances_proposed"] = advances.propose_applications(db, entity)["proposed"]
    return {**summary, "model_version": ctx.model_version}


def _close_stale_exceptions(ctx: Ctx, txn: PaymentTransaction, res: dict) -> None:
    if res["outcome"] == "exception":
        return
    for ex in ctx.db.execute(select(ExceptionCase).where(ExceptionCase.txn_id == txn.id, ExceptionCase.status.in_([ExceptionStatus.open]))).scalars():
        ex.status, ex.resolved_at, ex.resolution = ExceptionStatus.resolved, utcnow(), "auto_rematched"
