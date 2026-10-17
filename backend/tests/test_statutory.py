"""194Q threshold-aware TDS, GST-TDS split from income-tax TDS, and where each shows up."""
import pytest
from sqlalchemy import select

from app.matching import actions
from app.matching.tds import TdsScenario, split_components, tds_on
from app.models import MatchProposal, PostingLine, ProposalStatus, TdsEntry
from app.posting import service as posting
from tests.conftest import login


def test_194q_deducts_only_on_the_slice_above_the_yearly_threshold(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    inv = w.invoice(c, "INV-1", 5_00_000_00)               # taxable ₹5 lakh
    sc = TdsScenario(10, "taxable", "194Q", True, threshold_minor=10_00_000_00)
    assert tds_on(inv, sc, cum_before=0) == 0                              # 5L cumulative: below the 10L threshold
    assert tds_on(inv, sc, cum_before=8_00_000_00) == 3_00_000_00 * 10 // 10000   # 13L cumulative: only 3L is above it
    assert tds_on(inv, sc, cum_before=10_00_000_00) == 5_00_000_00 * 10 // 10000  # already above: the whole invoice
    flat = TdsScenario(10, "taxable", "194Q", False)
    assert tds_on(inv, flat) == 5_00_000_00 * 10 // 10000, "a non-usual 194Q guess has no threshold knowledge"


def test_194q_customer_is_matched_with_the_threshold_applied_and_the_deduction_is_the_usual_one(w):
    w.e.config = {**w.e.config, "tds_194q_threshold_minor": 10_00_000_00}
    c = w.customer("C1", "Acme Industries Pvt Ltd", tds_bp=10)
    c.tds_section = "194Q"
    a = w.invoice(c, "INV/26-27/4001", 8_00_000_00, days_ago=60)           # cumulative 8L: no TDS
    b = w.invoice(c, "INV/26-27/4002", 5_00_000_00, days_ago=30)           # cumulative 13L: TDS on 3L = ₹300
    w.db.flush()
    t1 = w.credit(a.total_minor, "NEFT-HDFCN26274004001-ACME INDUSTRIES PVT LTD-INV/26-27/4001")
    t2 = w.credit(b.total_minor - 3_00_00, "NEFT-HDFCN26274004002-ACME INDUSTRIES PVT LTD-INV/26-27/4002")
    w.run(t1, t2)
    p1 = w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t1.id)).scalar_one()
    p2 = w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t2.id)).scalar_one()
    assert p1.tds_minor == 0 and p1.status == ProposalStatus.auto_approved
    assert p2.tds_minor == 3_00_00 and p2.allocations[0].tds_section == "194Q" and p2.status == ProposalStatus.auto_approved
    assert not any("usual rate" in g for g in p2.guardrails)


def test_194q_threshold_resets_each_financial_year(w):
    from app.matching.context import build_ctx

    w.e.config = {**w.e.config, "tds_194q_threshold_minor": 10_00_000_00}
    c = w.customer("C1", "Acme Industries Pvt Ltd", tds_bp=10)
    c.tds_section = "194Q"
    old = w.invoice(c, "INV-OLD", 9_00_000_00, days_ago=400)                # previous financial year
    new = w.invoice(c, "INV-NEW", 9_00_000_00, days_ago=10)
    w.db.flush()
    ctx = build_ctx(w.db, w.e)
    assert ctx.cum_before[old.id] == 0 and ctx.cum_before[new.id] == 0


def test_split_components_always_adds_up_and_labels_each_part():
    assert split_components("194C+GST-TDS", 400, 4_000) == [("income_tax", "194C", 200, 2_000), ("gst", "GST-TDS", 200, 2_000)]
    assert split_components("194J+GST-TDS", 1200, 12_000) == [("income_tax", "194J", 1000, 10_000), ("gst", "GST-TDS", 200, 2_000)]
    assert split_components("GST-TDS", 200, 777) == [("gst", "GST-TDS", 200, 777)]
    assert split_components("194C", 200, 500) == [("income_tax", "194C", 200, 500)]
    assert split_components(None, None, 5) == [("income_tax", None, None, 5)]
    for amt in range(1, 3000, 7):
        assert sum(x[3] for x in split_components("194C+GST-TDS", 400, amt)) == amt


def gst_world(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd", tds_bp=400)
    c.tds_section = "194C+GST-TDS"
    inv = w.invoice(c, "INV/26-27/5001", 1_00_000_00)
    t = w.credit(inv.total_minor - 4_000_00, "NEFT-HDFCN26274005001-ACME INDUSTRIES PVT LTD-INV/26-27/5001")
    w.run(t)
    p = w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t.id)).scalar_one()
    if p.status == ProposalStatus.pending_review:
        actions.apply_proposal(w.db, None, p, "a", w.users["ar_analyst"].id)
    return c, inv, t, p


def test_combined_deduction_is_recorded_as_two_entries_and_only_income_tax_needs_a_certificate(w):
    c, inv, t, p = gst_world(w)
    rows = w.db.execute(select(TdsEntry).order_by(TdsEntry.kind)).scalars().all()
    assert [(r.kind, r.section, r.rate_bp, r.amount_minor) for r in rows] == [("gst", "GST-TDS", 200, 2_000_00), ("income_tax", "194C", 200, 2_000_00)]


def test_posting_credits_gst_tds_to_its_own_ledger_and_the_journal_still_balances(w):
    c, inv, t, p = gst_world(w)
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    by = {l.line_type: l for l in b.lines if l.line_type in ("tds_receivable", "gst_tds_receivable")}
    assert by["tds_receivable"].debit_minor == 2_000_00 and by["tds_receivable"].account == "1350-TDS-RECV"
    assert by["gst_tds_receivable"].debit_minor == 2_000_00 and by["gst_tds_receivable"].account == "1355-GST-TDS-RECV"
    assert sum(l.debit_minor for l in b.lines) == sum(l.credit_minor for l in b.lines)


def test_tds_worklist_and_dashboard_exclude_gst_tds_unless_asked(client, w):
    gst_world(w)
    h = login(client, "ar_manager@t.example", "pw-ar_manager")
    base = client.get(f"/api/v1/tds?entity_id={w.e.id}", headers=h).json()
    assert [x["kind"] for x in base["items"]] == ["income_tax"] and base["total_amount_minor"] == 2_000_00
    gst = client.get(f"/api/v1/tds?entity_id={w.e.id}&kind=gst", headers=h).json()
    assert [x["section"] for x in gst["items"]] == ["GST-TDS"]
    assert client.get(f"/api/v1/analytics/summary?entity_id={w.e.id}", headers=h).json()["tds"]["awaiting_certificate_minor"] == 2_000_00
