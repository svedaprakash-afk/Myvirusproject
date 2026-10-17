"""First-run setup of a legal entity on an empty database: the entity itself, its bank accounts, a default tolerance and approval policies.

Everything else (customers, open invoices, bank statements) comes in through Import. New entities start in suggest-only mode: nothing is
posted straight-through until a person has reviewed matches for a few weeks and turned it on in Settings.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .indian import is_valid_gstin, is_valid_pan, pan_from_gstin
from .models import (DEFAULT_ENTITY_CONFIG, DEFAULT_LEDGERS, ApprovalKind, ApprovalPolicy, BankAccount, LegalEntity, Role, ToleranceRule)


class OnboardError(ValueError):
    pass


def parse_bank(spec: str) -> dict:
    """'Bank name|account no|IFSC|ledger code[|currency]'. IFSC and ledger may be empty."""
    p = [x.strip() for x in spec.split("|")]
    if len(p) not in (4, 5) or not p[0] or not p[1]:
        raise OnboardError(f"bank '{spec}': use 'Bank name|account number|IFSC|ledger code[|currency]'")
    cur = (p[4] if len(p) == 5 and p[4] else "INR").upper()
    if len(cur) != 3:
        raise OnboardError(f"bank '{spec}': currency must be a 3-letter code")
    if p[2] and (len(p[2]) != 11 or p[2][4] != "0"):
        raise OnboardError(f"bank '{spec}': IFSC '{p[2]}' is not 11 characters with a 0 in the fifth")
    return {"bank_name": p[0], "account_no": p[1], "ifsc": p[2].upper() or None, "ledger_code": p[3] or "1100-BANK", "currency": cur}


def create_entity(db: Session, name: str, gstin: str | None = None, pan: str | None = None, banks: list[str] | None = None) -> LegalEntity:
    name, gstin, pan = name.strip(), (gstin or "").strip().upper() or None, (pan or "").strip().upper() or None
    if not name:
        raise OnboardError("name is required")
    if gstin and not is_valid_gstin(gstin):
        raise OnboardError(f"GSTIN '{gstin}' fails the format and check-digit test")
    pan = pan or (pan_from_gstin(gstin) if gstin else None)
    if pan and not is_valid_pan(pan):
        raise OnboardError(f"PAN '{pan}' is not valid")
    if db.scalar(select(LegalEntity.id).where(LegalEntity.name == name)):
        raise OnboardError(f"an entity named '{name}' already exists")
    parsed = [parse_bank(b) for b in (banks or [])]
    e = LegalEntity(name=name, gstin=gstin, pan=pan, config={**DEFAULT_ENTITY_CONFIG, "auto_approve_enabled": False}, ledgers=dict(DEFAULT_LEDGERS))
    db.add(e)
    db.flush()
    for b in parsed:
        db.add(BankAccount(entity_id=e.id, **b))
    db.add(ToleranceRule(entity_id=e.id, abs_minor=500, pct_bp=0))
    for kind, lo, hi, role in [(ApprovalKind.write_off, 0, 25_000_00, Role.ar_manager), (ApprovalKind.write_off, 25_000_00, None, Role.finance_head),
                               (ApprovalKind.adjustment, 0, 25_000_00, Role.ar_manager), (ApprovalKind.adjustment, 25_000_00, None, Role.finance_head),
                               (ApprovalKind.refund, 0, None, Role.finance_head), (ApprovalKind.manual_match, 0, None, Role.ar_manager),
                               (ApprovalKind.batch_release, 0, None, Role.treasury)]:
        db.add(ApprovalPolicy(entity_id=e.id, kind=kind, min_minor=lo, max_minor=hi, approver_role=role))
    db.flush()
    return e
