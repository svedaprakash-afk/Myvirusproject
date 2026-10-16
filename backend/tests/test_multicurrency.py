from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app import fx
from app.matching import actions
from app.models import ExchangeRate, MatchProposal, ProposalStatus
from app.posting import service as posting
from conftest import TODAY

D = TODAY - timedelta(days=2)


def rate(w, ccy, r, d=D):
    w.db.add(ExchangeRate(currency=ccy, rate_date=d, rate_micro=fx.parse_rate(r)))
    w.db.flush()


def prop(w, t):
    return w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t.id).order_by(MatchProposal.id.desc())).scalars().first()


def globex(w):
    return w.customer("X1", "Globex Corporation", pan_seed="AABCG1234F")


def lines(b, vno=None):
    return [l for l in b.lines if vno is None or l.voucher_no == vno]


def test_fx_helpers_are_exact_and_use_latest_prior_rate(w):
    rate(w, "USD", "83.45", date(2026, 10, 9))
    assert fx.rate_on(w.db, "USD", date(2026, 10, 12)) == 83_450_000      # weekend/holiday: latest prior rate
    assert fx.rate_on(w.db, "USD", date(2026, 10, 1)) is None
    assert fx.rate_on(w.db, "INR", date(2026, 10, 1)) == 1_000_000
    assert fx.to_base(1_000_000, 83_450_000) == 83_450_000          # $10,000.00 -> ₹8,34,500.00
    assert fx.from_base(83_450_000, 83_450_000) == 1_000_000


def test_same_currency_usd_receipt_matches_and_posts_realised_fx_gain(w):
    c = globex(w)
    inv = w.invoice(c, "EXP/26-27/0001", 10_000_00, gst_pct=0, currency="USD", booking_rate=82_500_000)
    t = w.credit(inv.total_minor, "SWIFT CR-GLOBEX CORPORATION-EXP/26-27/0001", currency="USD", channel="SWIFT", d=D)
    rate(w, "USD", "83.45")
    w.run(t)
    p = prop(w, t)
    assert p.status == ProposalStatus.auto_approved and [a.invoice.number for a in p.allocations] == ["EXP/26-27/0001"]
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    gain = [l for l in b.lines if l.line_type == "fx_gain"]
    assert len(gain) == 1 and gain[0].credit_minor == 950_000      # $10,000 x (83.45 - 82.50) = ₹9,500.00
    assert sum(l.base_debit_minor for l in b.lines) == sum(l.base_credit_minor for l in b.lines)
    ar = next(l for l in b.lines if l.line_type == "ar_clearance")
    assert ar.currency == "USD" and ar.credit_minor == 1_000_000 and ar.rate_micro == 82_500_000 and ar.base_credit_minor == 825_000_00


def test_foreign_receipt_without_a_rate_is_refused_not_guessed(w):
    c = globex(w)
    inv = w.invoice(c, "EXP/26-27/0002", 5_000_00, gst_pct=0, currency="USD", booking_rate=82_000_000)
    t = w.credit(inv.total_minor, "SWIFT CR-GLOBEX CORPORATION-EXP/26-27/0002", currency="USD", channel="SWIFT", d=D)
    w.run(t)
    with pytest.raises(posting.PostingError, match="exchange rate"):
        posting.build_batch(w.db, w.e, w.users["ar_manager"])


def test_implausible_fx_difference_is_refused(w):
    c = globex(w)
    inv = w.invoice(c, "EXP/26-27/0003", 5_000_00, gst_pct=0, currency="USD", booking_rate=40_000_000)   # fat-fingered booking rate
    t = w.credit(inv.total_minor, "SWIFT CR-GLOBEX CORPORATION-EXP/26-27/0003", currency="USD", channel="SWIFT", d=D)
    rate(w, "USD", "83.45")
    w.run(t)
    with pytest.raises(posting.PostingError, match="implausible"):
        posting.build_batch(w.db, w.e, w.users["ar_manager"])


def test_intermediary_bank_charges_on_foreign_receipt_are_booked_as_charges(w):
    c = globex(w)
    inv = w.invoice(c, "EXP/26-27/0004", 10_000_00, gst_pct=0, currency="USD", booking_rate=83_000_000)
    t = w.credit(inv.total_minor - 3_500, "SWIFT CR-GLOBEX CORPORATION-EXP/26-27/0004", currency="USD", channel="SWIFT", d=D)   # $35 short
    rate(w, "USD", "83.10")
    w.run(t)
    p = prop(w, t)
    assert p.writeoff_kind == "bank_charge" and p.writeoff_minor == 3_500 and p.status == ProposalStatus.auto_approved
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    assert any(l.line_type == "bank_charges_foreign" and l.account == "5400-BANK-CHG" and l.debit_minor == 3_500 for l in b.lines)


def test_same_gap_in_inr_is_not_treated_as_bank_charges(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    inv = w.invoice(c, "INV/26-27/9001", 100_000_00)
    t = w.credit(inv.total_minor - 3_500, "NEFT-HDFCN26274000900-ACME INDUSTRIES PVT LTD-INV/26-27/9001")   # ₹35 short, tolerance is ₹5
    w.run(t)
    assert prop(w, t).writeoff_kind == "tolerance" and prop(w, t).short_minor == 3_500


def test_inward_remittance_in_inr_for_usd_invoice_uses_stated_foreign_amount(w):
    c = globex(w)
    inv = w.invoice(c, "EXP/26-27/0005", 10_000_00, gst_pct=0, currency="USD", booking_rate=82_500_000)
    pay = 83_400_000  # ₹8,34,000.00 = USD 10,000 @ 83.40
    t = w.credit(pay, "INWARD REMITTANCE USD 10,000.00 FROM GLOBEX CORPORATION-FIRC 2610123-SWIFT CITIUS33", d=D)
    assert (t.fx_ccy, t.fx_minor) == ("USD", 1_000_000)
    rate(w, "USD", "83.40")
    w.run(t)
    p = prop(w, t)
    assert p.strategy == "cross_currency" and p.settle_currency == "USD" and p.status == ProposalStatus.auto_approved
    assert p.guardrails == [] and p.auto, "payer-stated foreign amount + default config => straight-through"
    assert inv.open_minor == 0
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    bank = next(l for l in b.lines if l.line_type == "bank_receipt")
    ar = next(l for l in b.lines if l.line_type == "ar_clearance")
    fxl = next(l for l in b.lines if l.line_type == "fx_gain")
    assert bank.currency == "INR" and bank.debit_minor == pay and ar.currency == "USD" and ar.credit_minor == 1_000_000
    assert ar.base_credit_minor == 82_500_000 and fxl.credit_minor == pay - 82_500_000
    assert sum(l.base_debit_minor for l in b.lines) == sum(l.base_credit_minor for l in b.lines)


def test_cross_currency_auto_is_default_but_can_be_switched_off(w):
    w.e.config = {**w.e.config, "auto_approve_cross_currency": False}
    c = globex(w)
    w.invoice(c, "EXP/26-27/0006", 10_000_00, gst_pct=0, currency="USD", booking_rate=82_500_000)
    t = w.credit(83_400_000, "INWARD REMITTANCE USD 10,000.00 FROM GLOBEX CORPORATION-FIRC 1-SWIFT X", d=D)
    rate(w, "USD", "83.40")
    w.run(t)
    p = prop(w, t)
    assert not p.auto and any("Cross-currency" in g for g in p.guardrails)


def test_estimated_foreign_amount_never_auto_approves_even_with_cross_currency_auto_on(w):
    c = globex(w)
    w.invoice(c, "EXP/26-27/0007", 7_000_00, gst_pct=0, currency="USD", booking_rate=82_500_000)
    t2 = w.credit(58_380_000, "NEFT-HDFCN26274000901-GLOBEX CORPORATION-EXP/26-27/0007", d=D)       # no foreign amount stated
    rate(w, "USD", "83.40")
    w.run(t2)
    p2 = prop(w, t2)
    assert p2.strategy == "cross_currency" and not p2.auto and any("estimated" in g for g in p2.guardrails)


def test_inr_receipt_far_from_rate_table_does_not_match_a_usd_invoice(w):
    c = globex(w)
    w.invoice(c, "EXP/26-27/0008", 10_000_00, gst_pct=0, currency="USD", booking_rate=82_500_000)
    t = w.credit(70_000_000, "NEFT-HDFCN26274000902-GLOBEX CORPORATION-EXP/26-27/0008", d=D)    # 16% below table
    rate(w, "USD", "83.40")
    w.run(t)
    p = prop(w, t)
    assert p is None or p.strategy != "cross_currency"


def test_usd_invoice_is_never_applied_to_inr_receipt_by_amount_coincidence(w):
    c = globex(w)
    inv = w.invoice(c, "EXP/26-27/0009", 10_000_00, gst_pct=0, currency="USD")
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000903-GLOBEX CORPORATION-PAYMENT", d=D)   # ₹10,000 == $10,000 numerically
    w.run(t)
    p = prop(w, t)
    assert p is None or all(a.invoice.currency == "INR" for a in p.allocations)


def test_tolerance_is_per_currency(w):
    from app.models import ToleranceRule

    w.db.add(ToleranceRule(entity_id=w.e.id, currency="USD", abs_minor=200))   # $2
    c = globex(w)
    inv = w.invoice(c, "EXP/26-27/0010", 10_000_00, gst_pct=0, currency="USD", booking_rate=83_000_000)
    t = w.credit(inv.total_minor - 150, "SWIFT CR-GLOBEX CORPORATION-EXP/26-27/0010", currency="USD", channel="SWIFT", d=D)
    rate(w, "USD", "83.10")
    w.run(t)
    p = prop(w, t)
    assert p.writeoff_minor == 150 and p.writeoff_kind == "tolerance"
