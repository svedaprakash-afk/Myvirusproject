from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("EXPORT_DIR", tempfile.mkdtemp())
os.environ.setdefault("PBKDF2_ITERATIONS", "1000")

from app.db import Base, get_db, make_engine  # noqa: E402
from app.indian import make_gstin  # noqa: E402
from app.models import (DEFAULT_ENTITY_CONFIG, DEFAULT_LEDGERS, ApprovalKind, ApprovalPolicy, BankAccount, Customer, Invoice,  # noqa: E402
                        LegalEntity, PaymentTransaction, Role, ToleranceRule, TxnStatus, User)
from app.security import hash_password  # noqa: E402

TODAY = date(2026, 10, 16)


@pytest.fixture()
def db():
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False)()
    yield s
    s.close()


class World:
    """Small hand-built tenant with known facts."""

    def __init__(self, db):
        self.db = db
        self.e = LegalEntity(name="Test Mfg Pvt Ltd", gstin=make_gstin("27", "AABCT1234F"), pan="AABCT1234F", config=dict(DEFAULT_ENTITY_CONFIG), ledgers=dict(DEFAULT_LEDGERS))
        self.g = LegalEntity(name="Test Group Exports Pvt Ltd", gstin=make_gstin("27", "AABCG9999K"), pan="AABCG9999K", config=dict(DEFAULT_ENTITY_CONFIG), ledgers=dict(DEFAULT_LEDGERS))
        db.add_all([self.e, self.g])
        db.flush()
        self.bank = BankAccount(entity_id=self.e.id, bank_name="HDFC", account_no="50200099887766", ifsc="HDFC0000123", ledger_code="1100-HDFC")
        db.add(self.bank)
        db.add(ToleranceRule(entity_id=self.e.id, abs_minor=500))
        for kind, lo, hi, role in [(ApprovalKind.write_off, 0, 25_000_00, Role.ar_manager), (ApprovalKind.write_off, 25_000_00, None, Role.finance_head),
                                   (ApprovalKind.adjustment, 0, None, Role.ar_manager), (ApprovalKind.refund, 0, None, Role.finance_head),
                                   (ApprovalKind.manual_match, 0, None, Role.ar_manager)]:
            db.add(ApprovalPolicy(entity_id=self.e.id, kind=kind, min_minor=lo, max_minor=hi, approver_role=role))
        self.users = {}
        for role in Role:
            u = User(email=f"{role.value}@t.example", name=role.value.replace("_", " ").title(), role=role, password_hash=hash_password("pw-" + role.value))
            db.add(u)
            self.users[role.value] = u
        self.users["analyst2"] = User(email="a2@t.example", name="Analyst Two", role=Role.ar_analyst, password_hash=hash_password("x"))
        db.add(self.users["analyst2"])
        db.flush()
        self.cust = {}
        self.n = 0

    def customer(self, code, name, tds_bp=None, van=None, tier="B", pan_seed="AABCA1234F") -> Customer:
        c = Customer(entity_id=self.e.id, code=code, name=name, gstin=make_gstin("27", pan_seed), pan=pan_seed, tds_rate_bp=tds_bp, tds_base="taxable",
                     tds_section="194C" if tds_bp else None, virtual_account_no=van, tier=tier)
        self.db.add(c)
        self.db.flush()
        self.cust[code] = c
        return c

    def invoice(self, cust, number, taxable, gst_pct=18, days_ago=40, terms=30, doc_type="invoice", currency="INR", booking_rate=1_000_000) -> Invoice:
        from app.money import pct_of

        gst = pct_of(taxable, gst_pct * 100)
        total = taxable + gst
        sign = -1 if doc_type == "credit_note" else 1
        dt = TODAY - timedelta(days=days_ago)
        inv = Invoice(entity_id=self.e.id, customer_id=cust.id, number=number, doc_type=doc_type, invoice_date=dt, due_date=dt + timedelta(days=terms),
                      taxable_minor=sign * taxable, gst_minor=sign * gst, total_minor=sign * total, open_minor=sign * total,
                      currency=currency, booking_rate_micro=booking_rate)
        self.db.add(inv)
        self.db.flush()
        return inv

    def credit(self, amount, narration, d=None, utr=None, payer=None, channel="NEFT", fee=0, extra=None, currency="INR") -> PaymentTransaction:
        from app.indian import parse_narration

        pn = parse_narration(narration)
        self.n += 1
        t = PaymentTransaction(entity_id=self.e.id, bank_account_id=self.bank.id, txn_date=d or TODAY - timedelta(days=2), direction="credit", amount_minor=amount,
                               fee_minor=fee, currency=currency, fx_ccy=pn.extra.get("fx_ccy"), fx_minor=pn.extra.get("fx_minor"), channel=pn.channel if channel == "NEFT" else channel, utr=utr or pn.utr, narration=narration,
                               payer_name=payer if payer is not None else pn.payer_name, payer_vpa=pn.payer_vpa, dedupe_key=f"k{self.n}-{amount}", status=TxnStatus.new,
                               extra={"kind": pn.kind, **(extra or {})})
        self.db.add(t)
        self.db.flush()
        return t

    def run(self, *txns):
        from app.matching.engine import run_matching

        return run_matching(self.db, self.e, [t.id for t in txns] if txns else None, "test")


@pytest.fixture()
def w(db):
    return World(db)


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def login(client, email, password) -> dict:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}
