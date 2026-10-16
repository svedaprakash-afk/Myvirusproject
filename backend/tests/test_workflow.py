"""Maker-checker, segregation of duties, posting integrity."""
import pytest
from sqlalchemy import select

from app import audit, workflow
from app.matching import actions
from app.models import (Adjustment, ApprovalKind, ApprovalStatus, BatchStatus, ExceptionCase, ExceptionStatus, ExceptionType, Invoice, MatchProposal,
                        PostingLine, ProposalStatus, TdsEntry, TxnStatus)
from app.posting import service as posting
from app.posting.connectors import REGISTRY


def short_pay_case(w, short=4_321_00):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    inv = w.invoice(c, "INV/26-27/1101", 100_000_00)
    t = w.credit(inv.total_minor - short, "NEFT-HDFCN26274000128-ACME INDUSTRIES PVT LTD-INV/26-27/1101")
    w.run(t)
    p = w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t.id)).scalar_one()
    actions.apply_proposal(w.db, None, p, "analyst", w.users["ar_analyst"].id)
    ex = w.db.execute(select(ExceptionCase).where(ExceptionCase.type == ExceptionType.short_pay)).scalar_one()
    return inv, t, p, ex


def test_write_off_requires_a_different_authorised_approver(w):
    inv, t, p, ex = short_pay_case(w)
    analyst, manager, cfo = w.users["ar_analyst"], w.users["ar_manager"], w.users["finance_head"]
    res = workflow.resolve_exception(w.db, analyst, ex, "write_off", {"note": "Customer quality claim agreed by sales"})
    req = w.db.get(workflow.ApprovalRequest, res["approval_id"])
    assert ex.status == ExceptionStatus.pending_approval and req.required_role.value == "ar_manager"
    with pytest.raises(workflow.WorkflowError, match="your own"):
        workflow.decide(w.db, analyst, req, True)                       # maker cannot check
    with pytest.raises(workflow.WorkflowError):
        workflow.decide(w.db, w.users["auditor"], req, True)            # auditor is read-only
    assert inv.open_minor == 4_321_00
    workflow.decide(w.db, manager, req, True, "ok")
    assert inv.open_minor == 0 and ex.status == ExceptionStatus.written_off
    adj = w.db.execute(select(Adjustment)).scalar_one()
    assert adj.amount_minor == 4_321_00 and adj.debit_account == "5900-WRITEOFF"


def test_large_write_off_escalates_to_finance_head(w):
    inv, t, p, ex = short_pay_case(w, short=40_000_00)
    req = w.db.get(workflow.ApprovalRequest, workflow.resolve_exception(w.db, w.users["ar_analyst"], ex, "write_off", {"note": "Disputed claim settled"})["approval_id"])
    assert req.required_role.value == "finance_head"
    with pytest.raises(workflow.WorkflowError, match="finance_head"):
        workflow.decide(w.db, w.users["ar_manager"], req, True)
    workflow.decide(w.db, w.users["finance_head"], req, True)
    assert req.status == ApprovalStatus.approved


def test_rejected_write_off_leaves_balance_untouched_and_case_open(w):
    inv, t, p, ex = short_pay_case(w)
    req = w.db.get(workflow.ApprovalRequest, workflow.resolve_exception(w.db, w.users["ar_analyst"], ex, "write_off", {"note": "please approve"})["approval_id"])
    workflow.decide(w.db, w.users["ar_manager"], req, False, "Chase customer instead")
    assert inv.open_minor == 4_321_00 and ex.status == ExceptionStatus.in_progress


def test_write_off_needs_a_reason(w):
    inv, t, p, ex = short_pay_case(w)
    with pytest.raises(workflow.WorkflowError, match="reason"):
        workflow.resolve_exception(w.db, w.users["ar_analyst"], ex, "write_off", {"note": ""})


def test_cannot_write_off_more_than_is_open(w):
    inv, t, p, ex = short_pay_case(w)
    with pytest.raises(workflow.WorkflowError, match="open"):
        workflow.resolve_exception(w.db, w.users["ar_analyst"], ex, "write_off", {"note": "typo", "amount_minor": 999_999_00})


# ------------------------------------------------------------------------------------------- manual match
def test_manual_match_must_reconcile_cash_and_respect_balances(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    inv = w.invoice(c, "INV/26-27/3001", 100_000_00)
    t = w.credit(inv.total_minor, "NEFT-ZZZZN26274000150-MYSTERY PAYER")
    big = w.credit(inv.total_minor * 2, "NEFT-ZZZZN26274000160-MYSTERY PAYER")
    w.run(t)
    a = w.users["ar_analyst"]
    with pytest.raises(workflow.WorkflowError, match="exceed"):
        workflow.manual_match(w.db, a, t, [{"invoice_id": inv.id, "applied_minor": inv.total_minor + 1}], "x")
    with pytest.raises(workflow.WorkflowError, match="outside"):       # cash is available, but the invoice cannot absorb it
        workflow.manual_match(w.db, a, big, [{"invoice_id": inv.id, "applied_minor": inv.total_minor + 1}], "x")
    r = workflow.manual_match(w.db, a, t, [{"invoice_id": inv.id, "applied_minor": inv.total_minor}], "Payer is Acme's treasury arm, confirmed by phone")
    assert r["status"] == "applied" and inv.open_minor == 0 and t.status == TxnStatus.matched


def test_manual_match_learns_the_payer_for_next_time(w):
    from app.models import CustomerIdentifier

    c = w.customer("C1", "Acme Industries Pvt Ltd")
    inv = w.invoice(c, "INV/26-27/3002", 100_000_00)
    t = w.credit(inv.total_minor, "NEFT-ZZZZN26274000151-ZENITH TREASURY SERVICES")
    w.run(t)
    workflow.manual_match(w.db, w.users["ar_analyst"], t, [{"invoice_id": inv.id, "applied_minor": inv.total_minor}], "Zenith is Acme's treasury arm")
    assert w.db.execute(select(CustomerIdentifier).where(CustomerIdentifier.value == "ZENITH TREASURY SERVICES")).scalar_one().customer_id == c.id


def test_manual_write_off_beyond_tolerance_needs_approval_before_balances_move(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    inv = w.invoice(c, "INV/26-27/3003", 100_000_00)
    t = w.credit(inv.total_minor - 5_000_00, "NEFT-ZZZZN26274000152-ACME INDUSTRIES PVT LTD")
    w.run(t)
    r = workflow.manual_match(w.db, w.users["ar_analyst"], t, [{"invoice_id": inv.id, "applied_minor": inv.total_minor - 5_000_00, "writeoff_minor": 5_000_00}], "Agreed discount")
    assert r["status"] == "awaiting_approval" and inv.open_minor == inv.total_minor
    workflow.decide(w.db, w.users["ar_manager"], w.db.get(workflow.ApprovalRequest, r["approval_id"]), True)
    assert inv.open_minor == 0


def test_reversal_restores_balances_voids_tds_and_records_negative_feedback(w):
    from app.models import MatchFeedback

    c = w.customer("C1", "Acme Industries Pvt Ltd", tds_bp=200)
    inv = w.invoice(c, "INV/26-27/3004", 100_000_00)
    t = w.credit(inv.total_minor - 2_000_00, "NEFT-HDFCN26274000153-ACME INDUSTRIES PVT LTD-INV/26-27/3004")
    w.run(t)
    p = w.db.execute(select(MatchProposal)).scalar_one()
    assert p.auto and inv.open_minor == 0
    actions.reverse_proposal(w.db, p, "Manager", w.users["ar_manager"].id, "Payment was for a different PO")
    assert inv.open_minor == inv.total_minor and p.status == ProposalStatus.reversed and t.status == TxnStatus.new
    assert w.db.execute(select(TdsEntry)).scalar_one().reversed
    assert w.db.execute(select(MatchFeedback)).scalar_one().label == 0


# ------------------------------------------------------------------------------------------- posting
def applied_world(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd", tds_bp=200)
    inv = w.invoice(c, "INV/26-27/4001", 100_000_00)
    t = w.credit(inv.total_minor - 2_000_00, "NEFT-HDFCN26274000154-ACME INDUSTRIES PVT LTD-INV/26-27/4001")
    c2 = w.customer("C2", "Bharat Forgings Ltd", pan_seed="AABCB1234F")
    inv2 = w.invoice(c2, "INV/26-27/4002", 50_000_00)
    t2 = w.credit(inv2.total_minor - 250, "NEFT-HDFCN26274000155-BHARAT FORGINGS LTD-INV/26-27/4002")      # tolerance write-off
    c3 = w.customer("C3", "Kaveri Tools Ltd", pan_seed="AABCK1234F")
    inv3 = w.invoice(c3, "INV/26-27/4003", 30_000_00)
    t3 = w.credit(inv3.total_minor + 300, "NEFT-HDFCN26274000156-KAVERI TOOLS LTD-INV/26-27/4003")      # ₹3 over: rounding gain
    w.run()
    return [t, t2, t3]


def test_every_voucher_balances_and_covers_each_economic_effect(w):
    applied_world(w)
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    by_v = {}
    for ln in b.lines:
        by_v.setdefault(ln.voucher_no, []).append(ln)
    assert len(by_v) == 3
    for v, lines in by_v.items():
        assert sum(x.debit_minor for x in lines) == sum(x.credit_minor for x in lines), v
    types = {x.line_type for x in b.lines}
    assert {"bank_receipt", "tds_receivable", "ar_clearance", "writeoff_tolerance", "rounding_gain"} <= types


def test_batch_sod_builder_cannot_release_and_accepter_cannot_release(w):
    applied_world(w)
    mgr, treas = w.users["ar_manager"], w.users["treasury"]
    b = posting.build_batch(w.db, w.e, mgr)
    with pytest.raises(posting.PostingError, match="cannot release"):
        posting.release_batch(w.db, b, mgr)
    posting.release_batch(w.db, b, treas)
    assert b.status == BatchStatus.approved


def test_export_requires_release_then_ack_marks_everything_posted(w):
    ts = applied_world(w)
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    with pytest.raises(posting.PostingError, match="released"):
        posting.export_batch(w.db, b, "generic_csv", w.users["treasury"])
    posting.release_batch(w.db, b, w.users["treasury"])
    name, content, ctype = posting.export_batch(w.db, b, "generic_csv", w.users["treasury"])
    assert b"voucher_no" in content and b.checksum and b.status == BatchStatus.exported
    vouchers = {x.voucher_no for x in b.lines}
    with pytest.raises(posting.PostingError, match="missing"):
        posting.acknowledge(w.db, b, {sorted(vouchers)[0]: "TALLY-1"}, w.users["treasury"])
    posting.acknowledge(w.db, b, {v: f"ERP-{i}" for i, v in enumerate(sorted(vouchers))}, w.users["treasury"])
    assert b.status == BatchStatus.acknowledged and all(t.status == TxnStatus.posted for t in ts)
    with pytest.raises(posting.PostingError):
        posting.build_batch(w.db, w.e, w.users["ar_manager"])   # nothing left to post


def test_planned_connectors_are_refused_not_faked(w):
    applied_world(w)
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    posting.release_batch(w.db, b, w.users["treasury"])
    assert REGISTRY["tally_prime"].status == "planned"
    with pytest.raises(posting.PostingError, match="planned"):
        posting.export_batch(w.db, b, "tally_prime", w.users["treasury"])


def test_canonical_json_connector_roundtrips_balanced_vouchers(w):
    import json

    applied_world(w)
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    posting.release_batch(w.db, b, w.users["treasury"])
    _, content, _ = posting.export_batch(w.db, b, "canonical_json", w.users["treasury"])
    doc = json.loads(content)
    assert doc["schema"] == "cashapp.journal/1" and all(v["balanced"] for v in doc["vouchers"]) and len(doc["vouchers"]) == 3


def test_reversal_blocked_once_batched(w):
    applied_world(w)
    posting.build_batch(w.db, w.e, w.users["ar_manager"])
    p = w.db.execute(select(MatchProposal)).scalars().first()
    with pytest.raises(actions.ActionError, match="batch"):
        actions.reverse_proposal(w.db, p, "m", w.users["ar_manager"].id, "too late")


def test_cancelled_batch_releases_items_for_rebatching(w):
    applied_world(w)
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    posting.cancel_batch(w.db, b, w.users["ar_manager"])
    b2 = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    assert b2.id != b.id and len({x.voucher_no for x in b2.lines}) == 3


# ------------------------------------------------------------------------------------------- audit chain
def test_audit_chain_detects_tampering_and_deletion(w):
    applied_world(w)
    w.db.commit()
    assert audit.verify_chain(w.db)["ok"]
    from app.models import AuditLog

    rows = w.db.execute(select(AuditLog).order_by(AuditLog.seq)).scalars().all()
    assert len(rows) > 5
    rows[2].detail = {"forged": True}
    w.db.flush()
    bad = audit.verify_chain(w.db)
    assert not bad["ok"] and bad["broken_at_seq"] == rows[2].seq
    w.db.rollback()   # discard the forgery; now test deletion on the intact chain
    w.db.delete(w.db.execute(select(AuditLog).where(AuditLog.seq == 3)).scalar_one())
    w.db.flush()
    gap = audit.verify_chain(w.db)
    assert not gap["ok"] and "gap" in gap["reason"]
