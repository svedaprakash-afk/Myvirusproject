"""Per-customer straight-through graduation.

A customer begins in `learning`: every match proposed for them waits for a human. After `graduate_after` consecutive
human-confirmed engine proposals with no correction in between, the customer graduates to `auto` and the engine may post
straight through (still subject to every other guardrail). Any rejection, manual override or reversal resets the streak and
demotes an `auto` customer. A reversal of an auto-applied match is treated as the strongest signal: it also clears a human pin.

A manager may pin a customer (`auto` or `suggest_only`); automation never *promotes* a pinned customer, but it will still demote
one after a reversed auto-match, because losing convenience is cheaper than a wrong posting.

Graduation is OFF by default for existing entities (behaviour unchanged) and is switched on per entity in settings.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import audit
from ..models import Customer, CustomerTrust, MatchProposal, utcnow

DEFAULT_TRUST_CFG = {"enabled": False, "graduate_after": 5, "demote_on_correction": True}
MODES = ("learning", "auto", "suggest_only")


def trust_cfg(cfg: dict) -> dict:
    return {**DEFAULT_TRUST_CFG, **(cfg.get("trust_graduation") or {})}


def _row(db: Session, customer_id: int, entity_id: int) -> CustomerTrust:
    row = db.get(CustomerTrust, customer_id)
    if not row:
        row = CustomerTrust(customer_id=customer_id, entity_id=entity_id, mode="learning", clean_streak=0, confirmed=0, corrections=0, pinned=False, note="")
        db.add(row)
        db.flush()
    return row


def _cfg_for(db: Session, entity_id: int) -> dict:
    from ..models import DEFAULT_ENTITY_CONFIG, LegalEntity

    e = db.get(LegalEntity, entity_id)
    return trust_cfg({**DEFAULT_ENTITY_CONFIG, **((e.config if e else None) or {})})


def is_engine_proposal(p: MatchProposal) -> bool:
    """Only proposals the engine scored (they carry features) can confirm trust; hand-built allocations cannot."""
    return bool(p.features) and (p.created_by or "").startswith("engine")


def record_confirmation(db: Session, p: MatchProposal, actor: str, actor_id: int | None) -> None:
    """A human accepted an engine proposal unchanged."""
    if not p.customer_id or not is_engine_proposal(p):
        return
    cfg = _cfg_for(db, p.entity_id)
    row = _row(db, p.customer_id, p.entity_id)
    row.confirmed += 1
    row.clean_streak += 1
    row.updated_at = utcnow()
    if cfg["enabled"] and row.mode == "learning" and not row.pinned and row.clean_streak >= cfg["graduate_after"]:
        row.mode, row.note = "auto", f"Graduated after {row.clean_streak} consecutive confirmed matches"
        audit.log(db, "engine", "trust.graduated", "Customer", p.customer_id, p.entity_id, {"streak": row.clean_streak, "threshold": cfg["graduate_after"]}, None)


def record_correction(db: Session, customer_id: int | None, entity_id: int, reason: str, actor_id: int | None = None, was_auto: bool = False) -> None:
    """A human rejected, overrode or reversed a match for this customer."""
    if not customer_id:
        return
    cfg = _cfg_for(db, entity_id)
    row = _row(db, customer_id, entity_id)
    row.corrections += 1
    row.clean_streak = 0
    row.updated_at = utcnow()
    demote = row.mode == "auto" and (cfg["demote_on_correction"] or was_auto)
    if demote:
        row.mode, row.note = "learning", f"Demoted: {reason}"[:300]
        if was_auto:
            row.pinned = False
        audit.log(db, "engine", "trust.demoted", "Customer", customer_id, entity_id, {"reason": reason, "was_auto": was_auto}, actor_id)


def set_mode(db: Session, customer: Customer, mode: str | None, threshold_override: float | None, clear_threshold: bool, note: str,
             actor: str, actor_id: int) -> CustomerTrust:
    row = _row(db, customer.id, customer.entity_id)
    before = {"mode": row.mode, "pinned": row.pinned, "threshold_override": row.threshold_override}
    if mode:
        if mode not in MODES:
            raise ValueError("mode must be learning, auto or suggest_only")
        row.mode = mode
        row.pinned = mode != "learning"   # moving back to `learning` hands control to automation again
        if mode == "learning":
            row.clean_streak = 0
    if clear_threshold:
        row.threshold_override = None
    elif threshold_override is not None:
        row.threshold_override = threshold_override
    row.note, row.updated_by, row.updated_at = (note or "")[:300], actor_id, utcnow()
    audit.log(db, actor, "trust.changed", "Customer", customer.id, customer.entity_id,
              {"before": before, "after": {"mode": row.mode, "pinned": row.pinned, "threshold_override": row.threshold_override}, "note": note}, actor_id)
    return row


def gate(cfg: dict, row: CustomerTrust | None, prob: float) -> list[str]:
    """Guardrail reasons contributed by trust. Empty list = trust does not block straight-through."""
    t = trust_cfg(cfg)
    reasons: list[str] = []
    n = t["graduate_after"]
    if row and row.threshold_override is not None and prob < max(row.threshold_override, cfg["auto_approve_threshold"]):
        reasons.append(f"Confidence {prob:.0%} below this customer's stricter threshold {row.threshold_override:.0%}")
    if row and row.mode == "suggest_only":
        reasons.append("Customer is pinned to suggest-only by a manager")
    elif t["enabled"] and not (row and row.mode == "auto"):
        streak = row.clean_streak if row else 0
        reasons.append(f"Customer has not yet graduated to straight-through ({streak}/{n} consecutive confirmed matches)")
    return reasons


def snapshot(db: Session, customer: Customer) -> dict:
    cfg = _cfg_for(db, customer.entity_id)
    row = db.get(CustomerTrust, customer.id)
    return {"customer_id": customer.id, "enabled": cfg["enabled"], "graduate_after": cfg["graduate_after"],
            "mode": row.mode if row else "learning", "pinned": bool(row and row.pinned), "clean_streak": row.clean_streak if row else 0,
            "confirmed": row.confirmed if row else 0, "corrections": row.corrections if row else 0,
            "threshold_override": row.threshold_override if row else None, "note": row.note if row else "",
            "updated_at": row.updated_at.isoformat() if row else None}
