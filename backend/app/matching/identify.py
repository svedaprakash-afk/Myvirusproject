"""Who paid? Combine VAN, learned VPA/account, tax IDs in text, fuzzy payer name and invoice-implied ownership."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from ..indian import normalise_account, normalise_name, normalise_ref
from ..models import PaymentTransaction
from .context import Ctx


@dataclass
class Guess:
    customer_id: int
    score: float
    reasons: list[str] = field(default_factory=list)
    name_sim: float = 0.0


def strip_for_name(t: str) -> str:
    import re

    return re.sub(r"\d+", " ", t)


def name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return (0.5 * fuzz.token_set_ratio(a, b) + 0.5 * fuzz.token_sort_ratio(a, b)) / 100.0


def identify(ctx: Ctx, txn: PaymentTransaction, extra_text: str = "", remit_payer: str | None = None,
             ref_customers: dict[int, str] | None = None) -> list[Guess]:
    """Return guesses sorted best-first. `ref_customers` maps customer_id -> 'exact'|'serial' from reference hits."""
    guesses: dict[int, Guess] = {}

    def add(cid: int, score: float, reason: str, name_sim: float = 0.0):
        g = guesses.setdefault(cid, Guess(cid, 0.0))
        if score > g.score:
            g.score = score
        g.reasons.append(reason)
        g.name_sim = max(g.name_sim, name_sim)

    text_up = f"{txn.narration} {extra_text}".upper()
    text_norm = normalise_ref(text_up)

    # 1. Virtual account number — the strongest signal in Indian corporate collections
    for c in ctx.customers.values():
        if c.virtual_account_no and len(c.virtual_account_no) >= 8 and normalise_ref(c.virtual_account_no) in text_norm:
            add(c.id, 1.0, f"Virtual account {c.virtual_account_no} credited")
    # 2. tax identifiers written into the text
    for c in ctx.customers.values():
        if c.gstin and c.gstin.upper() in text_up:
            add(c.id, 0.99, f"GSTIN {c.gstin} in remittance text")
        elif c.pan and c.pan.upper() in text_up:
            add(c.id, 0.95, f"PAN {c.pan} in remittance text")
    # 3. learned payer VPA / account
    if txn.payer_vpa and (cid := ctx.ident.get(("vpa", txn.payer_vpa.lower()))):
        add(cid, 0.97, f"UPI handle {txn.payer_vpa} previously used by this customer")
    if txn.payer_account and (cid := ctx.ident.get(("account", normalise_account(txn.payer_account)))):
        add(cid, 0.97, "Payer account previously used by this customer")
    # 4. fuzzy payer name (bank narration name, remittance name)
    for label, nm in (("Payer name", txn.payer_name), ("Remittance sender", remit_payer)):
        n = normalise_name(nm)
        if not n:
            continue
        for cid, names in ctx.cust_norm.items():
            best = max((name_similarity(n, x) for x in names if x), default=0.0)
            learned = n in names
            if best >= 0.999 or learned:
                add(cid, 0.96, f"{label} '{nm}' is a known name", 1.0)
            elif best >= 0.90:
                add(cid, 0.86, f"{label} '{nm}' ≈ '{ctx.customers[cid].name}' ({best:.0%})", best)
            elif best >= 0.80:
                add(cid, 0.72, f"{label} '{nm}' resembles '{ctx.customers[cid].name}' ({best:.0%})", best)
            elif best >= 0.68:
                add(cid, 0.50, f"{label} '{nm}' loosely resembles '{ctx.customers[cid].name}' ({best:.0%})", best)
    # 4b. customer name written anywhere in the text (covers narrations whose name could not be isolated)
    flat = " " + normalise_name(strip_for_name(text_up)) + " "
    for cid, names in ctx.cust_norm.items():
        for nm in names:
            if nm and (len(nm.split()) >= 2 or len(nm) >= 9) and f" {nm} " in flat:
                add(cid, 0.90, f"Customer name '{ctx.customers[cid].name}' appears in the payment text", 0.9)
                break
    # 5. owner of referenced invoices
    for cid, kind in (ref_customers or {}).items():
        add(cid, 0.95 if kind == "exact" else 0.80, "Owner of the invoice(s) referenced in the payment")

    out = sorted(guesses.values(), key=lambda g: (-g.score, -g.name_sim))
    # agreement of independent signals nudges confidence up (never past 0.99)
    for g in out:
        n = len({r.split(" ")[0] for r in g.reasons})
        g.score = min(0.99, g.score + 0.02 * (n - 1)) if g.score < 1.0 else 1.0
    return out


def is_ambiguous(guesses: list[Guess]) -> bool:
    if len(guesses) < 2:
        return False
    a, b = guesses[0], guesses[1]
    if a.score - b.score < 0.08 and a.score < 0.99:
        return True
    # corroboration conflict: the leader rests on invoice ownership, yet the payer's NAME points at someone else
    return a.name_sim < 0.6 and b.name_sim >= 0.8 and b.score >= 0.7
