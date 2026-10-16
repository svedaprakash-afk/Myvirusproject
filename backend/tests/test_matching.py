"""Engine behaviour on a hand-built tenant. Each test states a fact about the world and the expected control outcome."""
from sqlalchemy import select

from app.matching.subset import find_subsets
from app.models import (ExceptionCase, ExceptionType, Invoice, MatchProposal, ProposalStatus, TdsEntry, TxnStatus)


def proposal(w, txn) -> MatchProposal:
    return w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == txn.id).order_by(MatchProposal.id.desc())).scalars().first()


def excs(w, txn):
    return [x.type for x in w.db.execute(select(ExceptionCase).where(ExceptionCase.txn_id == txn.id)).scalars()]


def inv_open(w, number):
    return w.db.execute(select(Invoice).where(Invoice.number == number)).scalar_one().open_minor


def acme(w, **kw):
    c = w.customer("C1", "Acme Industries Pvt Ltd", **kw)
    return c


# ------------------------------------------------------------------------------------------- happy paths
def test_exact_reference_match_auto_approves_and_clears_invoice(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/0101", 100_000_00)
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000111-ACME INDUSTRIES PVT LTD-INV/26-27/0101-HDFC0000123")
    out = w.run(t)
    p = proposal(w, t)
    assert out["auto"] == 1 and p.status == ProposalStatus.auto_approved and p.auto and p.confidence > 0.95
    assert inv_open(w, "INV/26-27/0101") == 0 and t.status == TxnStatus.matched
    assert p.explanation["contributions"], "an analyst must be able to see WHY"


def test_bare_serial_reference_works_for_identified_payer(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/0777", 50_000_00)
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000112-ACME INDUSTRIES PVT LTD-PAYMENT AGAINST 777-HDFC0000123")
    w.run(t)
    p = proposal(w, t)
    assert [a.invoice.number for a in p.allocations] == ["INV/26-27/0777"] and p.allocations[0].reference_hit == "serial"


def test_virtual_account_identifies_payer_without_any_name(w):
    c = acme(w, van="MERID123456")
    inv = w.invoice(c, "INV/26-27/0200", 75_000_00)
    t = w.credit(inv.total_minor, "NEFT-ICICN26274000113-UNKNOWN REMITTER-INV/26-27/0200 VAN MERID123456", payer="UNKNOWN REMITTER")
    w.run(t)
    assert proposal(w, t).customer_id == c.id


# ------------------------------------------------------------------------------------------- TDS
def test_tds_short_pay_at_usual_rate_is_explained_and_creates_tds_credit(w):
    c = acme(w, tds_bp=200)
    inv = w.invoice(c, "INV/26-27/0300", 100_000_00)           # taxable 1,00,000; GST 18,000; total 1,18,000
    t = w.credit(inv.total_minor - 2_000_00, "NEFT-HDFCN26274000114-ACME INDUSTRIES PVT LTD-INV/26-27/0300")  # 2% of TAXABLE value withheld
    w.run(t)
    p = proposal(w, t)
    assert p.status == ProposalStatus.auto_approved and p.tds_minor == 2_000_00
    assert inv_open(w, "INV/26-27/0300") == 0
    tds = w.db.execute(select(TdsEntry)).scalar_one()
    assert tds.amount_minor == 2_000_00 and tds.fy == "2026-27" and tds.certificate_received is False


def test_tds_base_is_taxable_value_not_gross(w):
    """Customer withholding 2% of GROSS (an error or different practice) must NOT be silently accepted as their usual TDS."""
    c = acme(w, tds_bp=200)
    inv = w.invoice(c, "INV/26-27/0301", 100_000_00)
    t = w.credit(inv.total_minor - 2_360_00, "NEFT-HDFCN26274000115-ACME INDUSTRIES PVT LTD-INV/26-27/0301")  # 2% of 1,18,000
    w.run(t)
    p = proposal(w, t)
    assert p.status == ProposalStatus.pending_review, "non-usual TDS base needs a human"
    assert any("usual" in g for g in p.guardrails)


def test_unusual_tds_rate_goes_to_review_not_auto(w):
    c = acme(w, tds_bp=200)
    inv = w.invoice(c, "INV/26-27/0302", 100_000_00)
    t = w.credit(inv.total_minor - 10_000_00, "NEFT-HDFCN26274000116-ACME INDUSTRIES PVT LTD-INV/26-27/0302")  # 10%
    w.run(t)
    p = proposal(w, t)
    assert p.status == ProposalStatus.pending_review and p.tds_minor == 10_000_00


def test_rupee_rounded_tds_is_absorbed_but_large_gaps_are_not(w):
    c = acme(w, tds_bp=200)
    inv = w.invoice(c, "INV/26-27/0303", 123_457_50)   # 2% = 2,469.15 -> payer deducts 2,469
    t = w.credit(inv.total_minor - 2_469_00, "NEFT-HDFCN26274000117-ACME INDUSTRIES PVT LTD-INV/26-27/0303")
    w.run(t)
    p = proposal(w, t)
    assert p.status == ProposalStatus.auto_approved
    assert p.tds_minor == 2_469_15, "TDS credit must be the computed statutory amount, not the amount the payer happened to deduct"
    assert p.writeoff_minor == -15, "the 15-paisa rounding is booked as a rounding gain, visibly, instead of being hidden inside TDS"


# ------------------------------------------------------------------------------------------- bundles & credit notes
def test_multi_invoice_bundle_by_reference(w):
    c = acme(w)
    a = w.invoice(c, "INV/26-27/0401", 40_000_00)
    b = w.invoice(c, "INV/26-27/0402", 60_000_00)
    t = w.credit(a.total_minor + b.total_minor, "NEFT-HDFCN26274000118-ACME INDUSTRIES PVT LTD-INV/26-27/0401 INV/26-27/0402")
    w.run(t)
    p = proposal(w, t)
    assert sorted(x.invoice.number for x in p.allocations) == ["INV/26-27/0401", "INV/26-27/0402"] and p.auto


def test_credit_note_netting_when_referenced(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/0501", 100_000_00)
    cn = w.invoice(c, "CN/26-27/0009", 10_000_00, doc_type="credit_note")
    t = w.credit(inv.total_minor + cn.total_minor, "NEFT-HDFCN26274000119-ACME INDUSTRIES PVT LTD-INV/26-27/0501 CN/26-27/0009")
    w.run(t)
    p = proposal(w, t)
    assert p.auto and inv_open(w, "INV/26-27/0501") == 0 and inv_open(w, "CN/26-27/0009") == 0


def test_unreferenced_credit_note_is_never_netted_automatically(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/0502", 100_000_00)
    w.invoice(c, "CN/26-27/0010", 10_000_00, doc_type="credit_note")
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000120-ACME INDUSTRIES PVT LTD-INV/26-27/0502")
    w.run(t)
    p = proposal(w, t)
    assert [a.invoice.number for a in p.allocations] == ["INV/26-27/0502"]


# ------------------------------------------------------------------------------------------- amount-only discipline
def test_amount_only_match_is_suggested_but_not_auto_by_default(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/0601", 80_000_00)
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000121-ACME INDUSTRIES PVT LTD-PAYMENT")
    w.run(t)
    p = proposal(w, t)
    assert p.status == ProposalStatus.pending_review and any("amount alone" in g for g in p.guardrails)
    assert [a.invoice.number for a in p.allocations] == ["INV/26-27/0601"]


def test_amount_only_auto_can_be_enabled_per_entity(w):
    w.e.config = {**w.e.config, "auto_approve_amount_only": True}
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/0602", 80_000_00)
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000122-ACME INDUSTRIES PVT LTD-PAYMENT")
    w.run(t)
    assert proposal(w, t).auto


def test_two_identical_invoices_are_ambiguous_and_never_auto(w):
    w.e.config = {**w.e.config, "auto_approve_amount_only": True}   # even with the permissive switch on
    c = acme(w)
    a = w.invoice(c, "INV/26-27/0701", 80_000_00)
    w.invoice(c, "INV/26-27/0702", 80_000_00)
    t = w.credit(a.total_minor, "NEFT-HDFCN26274000123-ACME INDUSTRIES PVT LTD-PAYMENT")
    w.run(t)
    p = proposal(w, t)
    assert p.status == ProposalStatus.pending_review and any("combination" in g for g in p.guardrails)
    assert p.explanation["alternatives"], "the analyst must be shown the other candidate"


# ------------------------------------------------------------------------------------------- conflicts & third parties
def test_mistyped_reference_with_exact_amount_for_another_invoice_prefers_amount_but_needs_review(w):
    c = acme(w)
    wanted = w.invoice(c, "INV/26-27/0801", 90_000_00)
    other = w.invoice(c, "INV/26-27/0802", 33_333_00)
    t = w.credit(wanted.total_minor, f"NEFT-HDFCN26274000124-ACME INDUSTRIES PVT LTD-INV/26-27/0802")
    w.run(t)
    p = proposal(w, t)
    assert p.status == ProposalStatus.pending_review
    assert [a.invoice.number for a in p.allocations] == ["INV/26-27/0801"]
    assert other.open_minor == other.total_minor


def test_payer_name_of_another_customer_citing_an_invoice_is_not_auto(w):
    x = acme(w)
    y = w.customer("C2", "Bharat Forgings Ltd", pan_seed="AABCB1234F")
    inv = w.invoice(y, "INV/26-27/0901", 70_000_00)
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000125-ACME INDUSTRIES PVT LTD-INV/26-27/0901")
    w.run(t)
    p = proposal(w, t)
    assert p.status == ProposalStatus.pending_review and any("plausible payer" in g or "third-party" in g for g in p.guardrails)


def test_unknown_third_party_paying_an_invoice_is_flagged(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/0902", 70_000_00)
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000126-ZENITH TREASURY SERVICES-INV/26-27/0902")
    w.run(t)
    p = proposal(w, t)
    assert p.status == ProposalStatus.pending_review and any("third-party" in g for g in p.guardrails)


def test_confusable_customer_names_do_not_auto_match(w):
    a = w.customer("C1", "Shree Balaji Traders Pvt Ltd")
    b = w.customer("C2", "Shree Balaji Industries Pvt Ltd", pan_seed="AABCB1234F")
    ia = w.invoice(a, "INV/26-27/1001", 60_000_00)
    w.invoice(b, "INV/26-27/1002", 61_000_00)
    t = w.credit(ia.total_minor, "NEFT-HDFCN26274000127-SHREE BALAJI-PAYMENT")
    w.run(t)
    p = proposal(w, t)
    assert p is None or p.status == ProposalStatus.pending_review


# ------------------------------------------------------------------------------------------- residuals
def test_short_payment_without_reason_applies_cash_and_opens_exception(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/1101", 100_000_00)
    t = w.credit(inv.total_minor - 4_321_00, "NEFT-HDFCN26274000128-ACME INDUSTRIES PVT LTD-INV/26-27/1101")
    w.run(t)
    p = proposal(w, t)
    assert p.status == ProposalStatus.pending_review and p.short_minor == 4_321_00
    from app.matching import actions

    actions.apply_proposal(w.db, None, p, "analyst", w.users["ar_analyst"].id)
    assert inv_open(w, "INV/26-27/1101") == 4_321_00
    assert ExceptionType.short_pay in excs(w, t)


def test_overpayment_applies_invoice_and_holds_remainder(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/1201", 100_000_00)
    t = w.credit(inv.total_minor + 25_000_00, "NEFT-HDFCN26274000129-ACME INDUSTRIES PVT LTD-INV/26-27/1201")
    w.run(t)
    p = proposal(w, t)
    assert p.unapplied_minor == 25_000_00 and p.status == ProposalStatus.pending_review


def test_difference_within_tolerance_is_written_off_and_auto_approved(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/1301", 100_000_00)
    t = w.credit(inv.total_minor - 250, "NEFT-HDFCN26274000130-ACME INDUSTRIES PVT LTD-INV/26-27/1301")
    w.run(t)
    p = proposal(w, t)
    assert p.auto and p.writeoff_minor == 250


def test_difference_above_tolerance_is_not_written_off(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/1302", 100_000_00)
    t = w.credit(inv.total_minor - 600, "NEFT-HDFCN26274000131-ACME INDUSTRIES PVT LTD-INV/26-27/1302")  # ₹6 > ₹5
    w.run(t)
    p = proposal(w, t)
    assert p.writeoff_minor == 0 and p.short_minor == 600 and not p.auto


def test_disputed_invoice_blocks_auto_approval(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/1401", 100_000_00)
    inv.disputed = True
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000132-ACME INDUSTRIES PVT LTD-INV/26-27/1401")
    w.run(t)
    assert proposal(w, t).status == ProposalStatus.pending_review


def test_high_value_receipt_always_gets_a_human(w):
    w.e.config = {**w.e.config, "auto_approve_max_minor": 50_000_00}
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/1501", 100_000_00)
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000133-ACME INDUSTRIES PVT LTD-INV/26-27/1501")
    w.run(t)
    assert any("ceiling" in g for g in proposal(w, t).guardrails)


def test_suggest_only_mode_never_auto_approves(w):
    w.e.config = {**w.e.config, "auto_approve_enabled": False}
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/1601", 100_000_00)
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000134-ACME INDUSTRIES PVT LTD-INV/26-27/1601")
    out = w.run(t)
    assert out["auto"] == 0 and proposal(w, t).status == ProposalStatus.pending_review


# ------------------------------------------------------------------------------------------- non-customer & duplicates
def test_unknown_payer_becomes_unidentified_exception_with_no_proposal(w):
    acme(w)
    t = w.credit(12_345_00, "NEFT-ZZZZN26274000135-GLOBAL TRADELINKS-ADVANCE")
    w.run(t)
    assert proposal(w, t) is None and ExceptionType.unidentified_receipt in excs(w, t) and t.status == TxnStatus.exception


def test_weak_name_resemblance_is_not_treated_as_identification(w):
    w.customer("C1", "Ananya Enterprises Pvt Ltd")
    t = w.credit(12_345_00, "NEFT-ZZZZN26274000136-ANANYA ENTERPRISE SOLUTIONS-ADVANCE")
    w.run(t)
    assert ExceptionType.unidentified_receipt in excs(w, t) or ExceptionType.on_account in excs(w, t)


def test_interest_and_bank_charge_credits_are_not_receivables(w):
    t = w.credit(4_250_00, "INT.PD:01-07-2026 TO 30-09-2026")
    w.run(t)
    assert ExceptionType.non_ar_credit in excs(w, t) and proposal(w, t) is None


def test_intercompany_transfer_is_classified_not_applied(w):
    acme(w)
    t = w.credit(10_00_000_00, "RTGS-HDFCR26274000137ABCDEFGH-TEST GROUP EXPORTS PVT LTD-FUND TRANSFER", payer="TEST GROUP EXPORTS PVT LTD")
    w.run(t)
    assert ExceptionType.intercompany in excs(w, t)


def test_same_utr_twice_flags_only_the_later_line(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/1701", 100_000_00)
    first = w.credit(inv.total_minor, "NEFT-HDFCN26274000138-ACME INDUSTRIES PVT LTD-INV/26-27/1701")
    second = w.credit(inv.total_minor, "NEFT-HDFCN26274000138-ACME INDUSTRIES PVT LTD-INV/26-27/1701")
    w.run(first, second)
    assert proposal(w, first).auto and proposal(w, second) is None and ExceptionType.duplicate_payment in excs(w, second)


def test_repeat_payment_of_settled_invoice_is_a_duplicate_payment(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/1801", 100_000_00)
    first = w.credit(inv.total_minor, "NEFT-HDFCN26274000139-ACME INDUSTRIES PVT LTD-INV/26-27/1801")
    w.run(first)
    again = w.credit(inv.total_minor, "NEFT-HDFCN26274000140-ACME INDUSTRIES PVT LTD-INV/26-27/1801")
    w.run(again)
    assert ExceptionType.duplicate_payment in excs(w, again) and proposal(w, again) is None


def test_reference_to_settled_invoice_but_amount_fits_open_one_is_not_called_duplicate(w):
    c = acme(w)
    settled = w.invoice(c, "INV/26-27/1901", 100_000_00)
    openi = w.invoice(c, "INV/26-27/1902", 45_000_00)
    w.run(w.credit(settled.total_minor, "NEFT-HDFCN26274000141-ACME INDUSTRIES PVT LTD-INV/26-27/1901"))
    t = w.credit(openi.total_minor, "NEFT-HDFCN26274000142-ACME INDUSTRIES PVT LTD-INV/26-27/1901")
    w.run(t)
    p = proposal(w, t)
    assert p and p.status == ProposalStatus.pending_review and [a.invoice.number for a in p.allocations] == ["INV/26-27/1902"]


def test_remittance_advice_drives_allocation_and_tds(w):
    from app.ingestion import service as ing

    c = acme(w, tds_bp=200)
    a = w.invoice(c, "INV/26-27/2001", 100_000_00)
    b = w.invoice(c, "INV/26-27/2002", 50_000_00)
    paid = a.total_minor - 2_000_00 + b.total_minor - 1_000_00
    t = w.credit(paid, "NEFT-HDFCN26274000143-ACME IND-", payer="ACME IND")
    advice = (f"From: Acme Industries Pvt Ltd\nUTR: {t.utr}\n"
              f"INV/26-27/2001  {a.total_minor / 100:,.2f}  TDS 2,000.00  Net {(a.total_minor - 200000) / 100:,.2f}\n"
              f"INV/26-27/2002  {b.total_minor / 100:,.2f}  TDS 1,000.00  Net {(b.total_minor - 100000) / 100:,.2f}\nTotal payment: {paid / 100:,.2f}")
    doc = ing.ingest_remittance(w.db, w.e.id, advice, "a.txt", "email", "t", None)
    assert doc.linked_txn_id == t.id
    w.run(t)
    p = proposal(w, t)
    assert p.auto and p.tds_minor == 3_000_00 and {x.reference_hit for x in p.allocations} == {"remittance"}


# ------------------------------------------------------------------------------------------- re-run safety
def test_rerunning_matching_is_idempotent(w):
    c = acme(w)
    inv = w.invoice(c, "INV/26-27/2101", 100_000_00)
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000144-ACME INDUSTRIES PVT LTD-INV/26-27/2101")
    w.run(t)
    out2 = w.run()
    assert out2["processed"] == 0
    assert w.db.query(MatchProposal).count() == 1 and inv_open(w, "INV/26-27/2101") == 0


def test_two_receipts_cannot_both_consume_one_invoice(w):
    from app.matching import actions

    c = acme(w)
    inv = w.invoice(c, "INV/26-27/2201", 100_000_00)
    t1 = w.credit(inv.total_minor, "NEFT-HDFCN26274000145-ACME INDUSTRIES PVT LTD-PAYMENT")
    t2 = w.credit(inv.total_minor, "NEFT-HDFCN26274000146-ACME INDUSTRIES PVT LTD-PAYMENT")
    w.run(t1, t2)
    p1, p2 = proposal(w, t1), proposal(w, t2)
    assert any("another receipt" in g for g in (p1.guardrails + p2.guardrails))
    actions.apply_proposal(w.db, None, p1, "a", w.users["ar_analyst"].id)
    try:
        actions.apply_proposal(w.db, None, p2, "a", w.users["ar_analyst"].id)
        raised = False
    except actions.ActionError:
        raised = True
    assert raised and inv_open(w, "INV/26-27/2201") == 0


# ------------------------------------------------------------------------------------------- subset search
def test_subset_search_finds_exact_and_respects_limits():
    vals = [(1, 100), (2, 250), (3, 400), (4, 650), (5, 1000)]
    hits = find_subsets(vals, 1050, 0)
    assert any(sorted(k) == [3, 4] for k, _ in hits)
    assert any(sorted(k) == [1, 2, 3, 4] for k, _ in find_subsets(vals, 1400, 0)) or any(sorted(k) == [4, 3, 2] for k, _ in find_subsets(vals, 1300, 0))
    assert find_subsets(vals, 7, 0) == []
    big = [(i, 1000 + i) for i in range(200)]
    find_subsets(big, 999_999_999, 0, node_limit=5000)  # must terminate promptly via node limit


def test_subset_search_tolerance_window():
    hits = find_subsets([(1, 1000), (2, 2000)], 2998, 5)
    assert hits and sorted(hits[0][0]) == [1, 2]
