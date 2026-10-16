"""Seed a demo tenant by pushing synthetic files through the *real* ingestion pipeline."""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import audit
from ..db import Base
from ..ingestion import service as ing
from ..matching import engine
from ..models import (DEFAULT_ENTITY_CONFIG, DEFAULT_LEDGERS, ApprovalKind, ApprovalPolicy, BankAccount, Customer, LegalEntity, Role,
                      ToleranceRule, User)
from ..security import hash_password
from .generator import Dataset, build

DEMO_PASSWORD = "demo1234"  # demo tenant only; seeding is disabled when APP_ENV=prod
DEMO_USERS = [
    ("admin@meridian.example", "Isha Admin", Role.admin), ("analyst@meridian.example", "Arjun Analyst", Role.ar_analyst),
    ("analyst2@meridian.example", "Meera Analyst", Role.ar_analyst), ("manager@meridian.example", "Rohan AR Manager", Role.ar_manager),
    ("treasury@meridian.example", "Tara Treasury", Role.treasury), ("cfo@meridian.example", "Vikram CFO", Role.finance_head),
    ("auditor@meridian.example", "Anita Auditor", Role.auditor),
]


def reset_all(db: Session) -> None:
    for t in reversed(Base.metadata.sorted_tables):
        db.execute(delete(t))
    db.flush()


def seed_demo(db: Session, ds: Dataset | None = None, run_matching: bool = True, reset: bool = True, seed: int = 42) -> dict:
    if reset:
        reset_all(db)
    ds = ds or build(seed=seed)
    for email, name, role in DEMO_USERS:
        db.add(User(email=email, name=name, role=role, password_hash=hash_password(DEMO_PASSWORD)))
    e = LegalEntity(name=ds.entity["name"], gstin=ds.entity["gstin"], pan=ds.entity["pan"], config=dict(DEFAULT_ENTITY_CONFIG), ledgers=dict(DEFAULT_LEDGERS))
    g = LegalEntity(name=ds.group_entity["name"], gstin=ds.group_entity["gstin"], pan=ds.group_entity["pan"], config=dict(DEFAULT_ENTITY_CONFIG), ledgers=dict(DEFAULT_LEDGERS))
    db.add_all([e, g])
    db.flush()
    banks = {
        "main": BankAccount(entity_id=e.id, bank_name="HDFC Bank — Current A/c", account_no="50200012345678", ifsc="HDFC0000123", ledger_code="1100-HDFC"),
        "collect": BankAccount(entity_id=e.id, bank_name="ICICI Bank — Collections (VAN)", account_no="000405012345", ifsc="ICIC0000456", ledger_code="1110-ICICI-COLL", van_prefix="MERID"),
        "gateway": BankAccount(entity_id=e.id, bank_name="Razorpay settlements", account_no="RZP-SETTLE-01", ifsc=None, ledger_code="1120-GATEWAY", is_gateway=True),
    }
    db.add_all(banks.values())
    db.add(BankAccount(entity_id=g.id, bank_name="Axis Bank — Current A/c", account_no="917020012345678", ifsc="UTIB0000789"))
    db.add(ToleranceRule(entity_id=e.id, abs_minor=500, pct_bp=0))  # ₹5 default: absorbs rupee-rounded TDS across a few invoices
    for kind, lo, hi, role in [(ApprovalKind.write_off, 0, 25_000_00, Role.ar_manager), (ApprovalKind.write_off, 25_000_00, None, Role.finance_head),
                               (ApprovalKind.adjustment, 0, 25_000_00, Role.ar_manager), (ApprovalKind.adjustment, 25_000_00, None, Role.finance_head),
                               (ApprovalKind.refund, 0, None, Role.finance_head), (ApprovalKind.manual_match, 0, None, Role.ar_manager),
                               (ApprovalKind.batch_release, 0, None, Role.treasury)]:
        db.add(ApprovalPolicy(entity_id=e.id, kind=kind, min_minor=lo, max_minor=hi, approver_role=role))
    db.flush()

    rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in ds.invoice_rows]
    imp = ing.import_open_items(db, e, rows, "seed", None)
    # master-data attributes the ERP extract does not carry
    for c in ds.customers:
        cust = db.execute(select(Customer).where(Customer.entity_id == e.id, Customer.code == c.code)).scalar_one()
        cust.tier, cust.tds_rate_bp, cust.tds_base, cust.virtual_account_no = c.tier, c.tds_bp, c.tds_base, c.van
        cust.tds_section = "194C" if c.tds_bp in (100, 200) else ("194J" if c.tds_bp == 1000 else ("194Q" if c.tds_bp == 10 else ("194C+GST-TDS" if c.tds_bp == 400 else None)))
    db.flush()
    summary = {"customers": len(ds.customers), "invoices": imp, "statements": {}}
    for key, csv_text in ds.statements.items():
        summary["statements"][key] = ing.ingest_statement(db, banks[key], f"{key}_statement.csv", csv_text.encode(), "seed", None)
    if ds.gateway_csv:
        summary["statements"]["gateway"] = ing.ingest_statement(db, banks["gateway"], "razorpay_settlement.csv", ds.gateway_csv.encode(), "seed", None, gateway=True)
    for r in ds.remittances:
        ing.ingest_remittance(db, e.id, r["text"], "advice.txt", "email", "seed", None)
    db.flush()
    if run_matching:
        summary["matching"] = engine.run_matching(db, e, actor="seed")
    summary["entity_id"] = e.id
    return summary
