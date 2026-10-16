"""Seed a demo tenant by pushing synthetic files through the *real* ingestion pipeline."""
from __future__ import annotations

import csv
import io

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import audit
from ..db import Base
from ..ingestion import service as ing
from ..matching import engine
from ..models import (ExchangeRate, DEFAULT_ENTITY_CONFIG, DEFAULT_LEDGERS, ApprovalKind, ApprovalPolicy, BankAccount, Customer, LegalEntity, Role,
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
    summary["export"] = _seed_export_business(db, e, banks)
    if run_matching:
        summary["matching"] = engine.run_matching(db, e, actor="seed")
    summary["entity_id"] = e.id
    return summary


def _seed_export_business(db: Session, e: LegalEntity, banks: dict) -> dict:
    """Multi-currency demo: USD/EUR export invoices, a daily FX table, a USD bank account and the four settlement patterns."""
    import random
    from datetime import date, timedelta

    from ..fx import parse_rate, rate_on

    rng, today = random.Random(7), date.today()
    usd = BankAccount(entity_id=e.id, bank_name="HDFC Bank — USD EEFC A/c", account_no="50200099990001", ifsc="HDFC0000123", currency="USD",
                      ledger_code="1115-HDFC-USD", profile={"currency": "USD"})
    db.add(usd)
    r_usd, r_eur = 83.0, 90.5
    for k in range(240, -1, -1):
        d = today - timedelta(days=k)
        r_usd += rng.uniform(-0.12, 0.12) - 0.03 * (r_usd - 83.0)    # mean-reverting so demo rates stay realistic
        r_eur += rng.uniform(-0.15, 0.15) - 0.03 * (r_eur - 90.5)
        if d.weekday() < 5:
            db.add(ExchangeRate(currency="USD", rate_date=d, rate_micro=parse_rate(f"{r_usd:.4f}"), source="seed"))
            db.add(ExchangeRate(currency="EUR", rate_date=d, rate_micro=parse_rate(f"{r_eur:.4f}"), source="seed"))
    db.flush()
    cust = [("X1", "Globex Corporation", "USD"), ("X2", "Nordwind Maschinen GmbH", "EUR")]
    rows, inv = [], {}
    for i, (code, name, ccy) in enumerate(cust):
        for j in range(4):
            dt = today - timedelta(days=60 - j * 9 - i * 3)
            total = rng.choice([8_450, 12_900, 21_350, 6_780, 15_400]) + j * 137
            n = f"EXP/26-27/{i * 10 + j + 1:04d}"
            inv[n] = total
            rows.append({"invoice_no": n, "customer_code": code, "customer_name": name, "invoice_date": dt.isoformat(), "due_date": (dt + timedelta(days=45)).isoformat(),
                         "taxable": f"{total:.2f}", "gst": "0", "total": f"{total:.2f}", "open": f"{total:.2f}", "currency": ccy,
                         "booking_rate": f"{rate_on(db, ccy, dt) / 1e6:.4f}", "payment_terms": "45"})
    ing.import_open_items(db, e, rows, "seed", None)
    d1, d2, d3 = today - timedelta(days=4), today - timedelta(days=3), today - timedelta(days=2)
    rt = lambda ccy, d: rate_on(db, ccy, d) / 1e6
    lines = [  # USD account: full payment, and one short by $35 of intermediary charges
        (d1, "SWIFT CR-GLOBEX CORPORATION-EXP/26-27/0001", inv["EXP/26-27/0001"] * 100, ""),
        (d2, "SWIFT CR-GLOBEX CORPORATION-EXP/26-27/0002", inv["EXP/26-27/0002"] * 100 - 3500, ""),
    ]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"])
    bal = 0
    for d, nar, amt, _ in lines:
        bal += amt
        w.writerow([d.strftime("%d/%m/%y"), nar, "", f"{amt / 100:.2f}", f"{bal / 100:.2f}"])
    ing.ingest_statement(db, usd, "usd_statement.csv", out.getvalue().encode(), "seed", None)
    # INR account: (a) payer's USD amount stated in the inward-remittance narration; (b) EUR payment with no stated amount (rate-table estimate)
    a_inr = round(inv["EXP/26-27/0003"] * rt("USD", d3) * 0.9985, 2)
    b_inr = round(inv["EXP/26-27/0011"] * rt("EUR", d3) * 0.998, 2)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"])
    w.writerow([d3.strftime("%d/%m/%y"), f"INWARD REMITTANCE USD {inv['EXP/26-27/0003']:,.2f} FROM GLOBEX CORPORATION-FIRC 2610{rng.randint(100, 999)}-SWIFT CITIUS33", "", f"{a_inr:.2f}", "1.00"])
    w.writerow([d3.strftime("%d/%m/%y"), f"NEFT-HDFCN26{rng.randint(10**9, 10**10 - 1)}-NORDWIND MASCHINEN GMBH-EXP/26-27/0011", "", f"{b_inr:.2f}", "2.00"])
    ing.ingest_statement(db, banks["main"], "inr_export_receipts.csv", out.getvalue().encode(), "seed", None)
    return {"customers": len(cust), "invoices": len(rows)}
