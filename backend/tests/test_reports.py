"""TDS register with Form 16A due dates, ageing, netting and provision."""
from datetime import date

import pytest
from sqlalchemy import select

from app import reports
from app.matching import actions
from app.models import MatchProposal, OnAccountBalance, ProposalStatus, TdsEntry
from tests.conftest import TODAY, login


def add_tds(w, c, inv, amount, d, section="194C", cert=False, in26=False, kind="income_tax"):
    from app.indian import financial_year

    p = MatchProposal(entity_id=w.e.id, txn_id=w.credit(100_00 + w.n, f"NEFT-R{w.n}").id, status=ProposalStatus.accepted, strategy="manual",
                      confidence=1.0, model_version="manual")
    w.db.add(p)
    w.db.flush()
    fy, q = financial_year(d)
    t = TdsEntry(entity_id=w.e.id, customer_id=c.id, invoice_id=inv.id, proposal_id=p.id, txn_date=d, fy=fy, quarter=q, section=section, rate_bp=200,
                 amount_minor=amount, kind=kind, certificate_received=cert, in_26as=in26)
    w.db.add(t)
    w.db.flush()
    return t


def test_form16a_due_dates_follow_the_return_due_date_plus_15_days():
    assert reports.form16a_due("2026-27", "Q1") == date(2026, 8, 15)
    assert reports.form16a_due("2026-27", "Q2") == date(2026, 11, 15)
    assert reports.form16a_due("2026-27", "Q3") == date(2027, 2, 15)
    assert reports.form16a_due("2026-27", "Q4") == date(2027, 6, 15)


def test_register_groups_by_quarter_customer_and_section_and_flags_overdue_certificates(w):
    a, b = w.customer("C1", "Acme Industries Pvt Ltd"), w.customer("C2", "Bharat Rail Equipments Ltd", pan_seed="AABCB1234F")
    inv = w.invoice(a, "INV-1", 100_000_00)
    inv2 = w.invoice(b, "INV-2", 100_000_00)
    add_tds(w, a, inv, 2_000_00, date(2026, 5, 10))                       # Q1 FY26-27, certificate due 15 Aug: overdue by TODAY (16 Oct)
    add_tds(w, a, inv, 1_000_00, date(2026, 6, 20), cert=True, in26=True)
    add_tds(w, b, inv2, 4_000_00, date(2026, 9, 5))                       # Q2, due 15 Nov: not overdue yet
    add_tds(w, a, inv, 500_00, date(2026, 9, 6), kind="gst", section="GST-TDS")      # GST-TDS never appears in the income-tax register
    r = reports.tds_register(w.db, w.e.id, today=TODAY)
    rows = {(x["quarter"], x["customer_code"]): x for x in r["rows"]}
    q1 = rows[("Q1", "C1")]
    assert q1["entries"] == 2 and q1["amount_minor"] == 3_000_00 and q1["pending_minor"] == 2_000_00 and q1["certificate_minor"] == 1_000_00
    assert q1["in_26as_minor"] == 1_000_00 and q1["not_in_26as_minor"] == 2_000_00 and q1["overdue"] and q1["days_overdue"] == (TODAY - date(2026, 8, 15)).days
    assert not rows[("Q2", "C2")]["overdue"] and ("Q2", "C1") not in rows
    assert r["overdue_minor"] == 2_000_00 and r["overdue_customers"] == 1 and r["totals"]["amount_minor"] == 7_000_00
    assert [x["quarter"] for x in reports.tds_register(w.db, w.e.id, quarter="Q2", today=TODAY)["rows"]] == ["Q2"]


def test_register_csv_endpoint_exports_and_is_audited(client, w):
    from app.models import AuditLog

    a = w.customer("C1", "Acme Industries Pvt Ltd")
    add_tds(w, a, w.invoice(a, "INV-1", 100_000_00), 2_000_00, date(2026, 5, 10))
    h = login(client, "ar_manager@t.example", "pw-ar_manager")
    r = client.get(f"/api/v1/reports/tds-register?entity_id={w.e.id}&format=csv", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("FY,Quarter") and "C1,Acme Industries Pvt Ltd" in lines[1] and "2000.00" in lines[1]
    assert client.get(f"/api/v1/reports/tds-register?entity_id={w.e.id}&quarter=Q9", headers=h).status_code == 400
    assert w.db.execute(select(AuditLog).where(AuditLog.action == "report.exported")).scalars().first() is not None


def aged(w, c, number, taxable, overdue_days):
    # days_ago such that due date is `overdue_days` before TODAY with 30 day terms
    return w.invoice(c, number, taxable, days_ago=overdue_days + 30, terms=30)


def test_ageing_buckets_and_default_provision_matrix(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    a = aged(w, c, "INV-A", 100_000_00, -10)      # not due yet
    b = aged(w, c, "INV-B", 100_000_00, 20)       # 1-30
    d = aged(w, c, "INV-D", 100_000_00, 100)      # 91-180
    e = aged(w, c, "INV-E", 100_000_00, 400)      # 365+
    r = reports.ageing_provision(w.db, w.e, TODAY)
    row = r["rows"][0]
    tot = a.total_minor
    assert (row["not_due"], row["1-30"], row["91-180"], row["365+"]) == (tot, tot, tot, tot) and row["gross_minor"] == 4 * tot
    assert row["provision_minor"] == tot * 50 // 10000 * 2 + tot * 1500 // 10000 + tot * 10000 // 10000   # not due and 1-30 both 0.5%
    assert row["oldest_days_overdue"] == 400 and r["totals"]["provision_minor"] == row["provision_minor"]


def test_credit_notes_and_cash_on_account_net_against_the_oldest_invoices_first(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    old = aged(w, c, "INV-OLD", 100_000_00, 400)
    new = aged(w, c, "INV-NEW", 100_000_00, 5)
    cn = w.invoice(c, "CN-1", 40_000_00, doc_type="credit_note", days_ago=10)
    adv = w.credit(10_000_00, "NEFT-ADV")
    p = MatchProposal(entity_id=w.e.id, txn_id=adv.id, customer_id=c.id, status=ProposalStatus.accepted, strategy="manual_on_account", confidence=1.0, model_version="manual")
    w.db.add(p)
    w.db.flush()
    w.db.add(OnAccountBalance(entity_id=w.e.id, customer_id=c.id, txn_id=adv.id, proposal_id=p.id, currency="INR", original_minor=10_000_00,
                              remaining_minor=10_000_00, status="open"))
    w.db.flush()
    row = reports.ageing_provision(w.db, w.e, TODAY)["rows"][0]
    assert row["credits_minor"] == 47_200_00 and row["advances_minor"] == 10_000_00
    assert row["365+"] == old.total_minor - 57_200_00 and row["1-30"] == new.total_minor
    assert row["net_minor"] == old.total_minor + new.total_minor - 57_200_00


def test_disputed_invoice_is_provided_at_the_higher_specific_rate(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    inv = aged(w, c, "INV-D", 100_000_00, 5)
    inv.disputed = True
    w.db.flush()
    r = reports.ageing_provision(w.db, w.e, TODAY)
    assert r["rows"][0]["provision_minor"] == inv.total_minor * 5000 // 10000 and r["rows"][0]["disputed_minor"] == inv.total_minor


def test_custom_matrix_validation_and_use_via_the_api(client, w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    inv = aged(w, c, "INV-D", 100_000_00, 40)
    h = login(client, "finance_head@t.example", "pw-finance_head")
    url = f"/api/v1/entities/{w.e.id}/config"
    assert client.put(url, json={"provision_matrix": [{"from_days": 10, "pct_bp": 100}]}, headers=h).status_code == 422
    assert client.put(url, json={"provision_matrix": [{"from_days": 0, "pct_bp": 500}, {"from_days": 30, "pct_bp": 100}]}, headers=h).status_code == 422
    assert client.put(url, json={"provision_matrix": [{"from_days": 0, "pct_bp": 0}, {"from_days": 31, "pct_bp": 1000}]}, headers=h).status_code == 200
    r = client.get(f"/api/v1/reports/ageing?entity_id={w.e.id}&as_of={TODAY}", headers=h).json()
    assert r["rows"][0]["provision_minor"] == inv.total_minor * 1000 // 10000
    csvr = client.get(f"/api/v1/reports/ageing?entity_id={w.e.id}&as_of={TODAY}&format=csv", headers=h)
    assert csvr.text.splitlines()[-1].startswith(",TOTAL")
    assert client.get(f"/api/v1/reports/ageing?entity_id={w.e.id}&as_of=2999-01-01", headers=h).status_code == 400


def test_foreign_currency_invoices_use_the_as_of_rate_and_unrated_ones_are_listed_not_hidden(w):
    from app.models import ExchangeRate

    c = w.customer("C1", "Acme Industries Pvt Ltd")
    usd = w.invoice(c, "EXP-1", 1_000_00, currency="USD", booking_rate=83_000_000, days_ago=40, gst_pct=0)
    eur = w.invoice(c, "EXP-2", 1_000_00, currency="EUR", booking_rate=90_000_000, days_ago=40, gst_pct=0)
    w.db.add(ExchangeRate(currency="USD", rate_date=TODAY, rate_micro=84_000_000, source="test"))
    w.db.flush()
    r = reports.ageing_provision(w.db, w.e, TODAY)
    assert r["rows"][0]["gross_minor"] == 1_000_00 * 84, "USD at the as-of rate; EUR has no rate"
    assert r["missing_rates"] == ["EUR"]
