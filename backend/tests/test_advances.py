"""On-account balances, advance application, part-payments, dispute-aware allocation, and the overpayment double-post regression."""
import pytest
from sqlalchemy import select

from app import workflow
from app.matching import actions, advances
from app.models import (Adjustment, AdvanceApplication, ApprovalKind, ExceptionCase, ExceptionType, MatchProposal, OnAccountBalance, PostingLine,
                        ProposalStatus)
from app.models import DEFAULT_LEDGERS
from app.posting import service as posting
from tests.conftest import login


def overpaid(w, extra=10_000_00, number="INV/26-27/1201", hold=False):
    c = w.cust.get("C1") or w.customer("C1", "Acme Industries Pvt Ltd")
    inv = w.invoice(c, number, 100_000_00)
    t = w.credit(inv.total_minor + extra, f"NEFT-HDFCN26274000999-ACME INDUSTRIES PVT LTD-{number}")
    w.run(t)
    p = w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t.id)).scalar_one()
    if p.status == ProposalStatus.pending_review:
        actions.apply_proposal(w.db, None, p, "a", w.users["ar_analyst"].id)
    if hold:
        ex = w.db.execute(select(ExceptionCase).where(ExceptionCase.type == ExceptionType.overpayment, ExceptionCase.txn_id == t.id)).scalar_one()
        workflow.resolve_exception(w.db, w.users["ar_analyst"], ex, "on_account", {})
    return c, inv, t, p


def test_holding_an_overpayment_on_account_does_not_post_the_bank_receipt_twice(w):
    c, inv, t, p = overpaid(w)
    ex = w.db.execute(select(ExceptionCase).where(ExceptionCase.type == ExceptionType.overpayment)).scalar_one()
    res = workflow.resolve_exception(w.db, w.users["ar_analyst"], ex, "on_account", {})
    assert res["proposal_id"] == p.id
    assert len(w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t.id)).scalars().all()) == 1
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    assert sum(l.debit_minor for l in b.lines if l.line_type == "bank_receipt") == t.amount_minor
    assert sum(l.credit_minor for l in b.lines if l.line_type == "unapplied_cash") == 10_000_00
    assert sum(l.base_debit_minor for l in b.lines) == sum(l.base_credit_minor for l in b.lines)


def test_balance_is_created_when_the_overpayment_is_applied_and_voided_on_reversal(w):
    c, inv, t, p = overpaid(w)
    bal = w.db.execute(select(OnAccountBalance)).scalar_one()
    assert bal.remaining_minor == bal.original_minor == 10_000_00 and bal.customer_id == c.id and bal.status == "open"
    actions.reverse_proposal(w.db, p, "a", w.users["ar_analyst"].id, "wrong customer")
    assert w.db.get(OnAccountBalance, bal.id).status == "void" and w.db.get(OnAccountBalance, bal.id).remaining_minor == 0


def test_advance_is_proposed_for_a_later_invoice_by_run_matching(w):
    c, inv, t, p = overpaid(w, extra=30_000_00, hold=True)
    new = w.invoice(c, "INV/26-27/1300", 20_000_00, days_ago=1)       # raised after the advance arrived
    w.run()   # run_matching proposes advance applications as a last step
    app = w.db.execute(select(AdvanceApplication)).scalar_one()
    assert new.total_minor == 23_600_00
    assert app.invoice_id == new.id and app.amount_minor == 23_600_00 and app.status == "proposed" and app.basis == "oldest_first"


def test_application_clears_the_invoice_reduces_the_balance_and_posts_dr_unapplied_cr_ar(w):
    c, inv, t, p = overpaid(w, extra=30_000_00, hold=True)
    new = w.invoice(c, "INV/26-27/1300", 20_000_00, days_ago=1)
    w.db.flush()
    advances.propose_applications(w.db, w.e)
    a = w.db.execute(select(AdvanceApplication)).scalar_one()
    assert a.amount_minor == new.open_minor == 23_600_00
    advances.accept(w.db, a, w.users["ar_analyst"])
    bal = w.db.execute(select(OnAccountBalance)).scalar_one()
    assert new.open_minor == 0 and bal.remaining_minor == 6_400_00 and bal.status == "open" and a.status == "applied"
    adj = w.db.get(Adjustment, a.adjustment_id)
    assert adj.kind == "advance_application" and adj.debit_account == DEFAULT_LEDGERS["unapplied_cash"] and adj.credit_account == DEFAULT_LEDGERS["ar_control"]
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    lines = [l for l in b.lines if l.adjustment_id == adj.id]
    assert sum(l.debit_minor for l in lines) == sum(l.credit_minor for l in lines) == 23_600_00
    assert not any(l.line_type == "bank_receipt" and l.adjustment_id == adj.id for l in lines), "no second bank line"
    with pytest.raises(advances.AdvanceError, match="already applied"):
        advances.accept(w.db, a, w.users["ar_analyst"])


def test_po_reference_in_the_payment_text_picks_the_right_invoice_over_the_oldest(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    t = w.credit(50_000_00, "NEFT-HDFCN26274000777-ACME INDUSTRIES PVT LTD-ADVANCE PO-7788")
    w.run(t)           # payer is known but nothing is open yet: an advance, held on account
    ex = w.db.execute(select(ExceptionCase).where(ExceptionCase.txn_id == t.id)).scalar_one()
    assert ex.type == ExceptionType.on_account
    workflow.resolve_exception(w.db, w.users["ar_analyst"], ex, "on_account", {"customer_id": c.id})
    old = w.invoice(c, "INV/26-27/0100", 40_000_00, days_ago=30)
    target = w.invoice(c, "INV/26-27/0200", 40_000_00, days_ago=5)
    target.po_ref = "PO-7788"
    w.db.flush()
    bal = w.db.execute(select(OnAccountBalance)).scalar_one()
    assert bal.remaining_minor == 50_000_00
    advances.propose_applications(w.db, w.e)
    apps = w.db.execute(select(AdvanceApplication).order_by(AdvanceApplication.id)).scalars().all()
    assert apps[0].invoice_id == target.id and apps[0].basis == "reference" and apps[0].amount_minor == 47_200_00
    assert apps[1].invoice_id == old.id and apps[1].basis == "oldest_first" and apps[1].amount_minor == 2_800_00


def test_disputed_invoices_and_foreign_balances_are_never_proposed(w):
    c, inv, t, p = overpaid(w, extra=30_000_00, hold=True)
    d = w.invoice(c, "INV/26-27/1400", 10_000_00, days_ago=1)
    d.disputed = True
    w.db.flush()
    assert advances.propose_applications(w.db, w.e)["proposed"] == 0
    bal = w.db.execute(select(OnAccountBalance)).scalar_one()
    bal.currency = "USD"
    d.disputed = False
    w.db.flush()
    assert advances.propose_applications(w.db, w.e) == {"proposed": 0, "skipped_foreign_balances": 1}


def test_a_balance_that_is_partly_applied_blocks_reversal_of_its_receipt(w):
    c, inv, t, p = overpaid(w, extra=30_000_00, hold=True)
    w.invoice(c, "INV/26-27/1300", 20_000_00, days_ago=1)
    advances.propose_applications(w.db, w.e)
    advances.accept(w.db, w.db.execute(select(AdvanceApplication)).scalar_one(), w.users["ar_analyst"])
    with pytest.raises(actions.ActionError, match="already been applied"):
        actions.reverse_proposal(w.db, p, "a", w.users["ar_analyst"].id, "oops")
    assert p.status != ProposalStatus.reversed and inv.open_minor == 0, "nothing was half-undone"


def test_refund_approval_closes_the_balance(w):
    c, inv, t, p = overpaid(w)
    ex = w.db.execute(select(ExceptionCase).where(ExceptionCase.type == ExceptionType.overpayment)).scalar_one()
    res = workflow.resolve_exception(w.db, w.users["ar_analyst"], ex, "refund", {"note": "Customer asked for a refund of the excess"})
    req = w.db.get(workflow.ApprovalRequest, res["approval_id"])
    workflow.decide(w.db, w.users["finance_head"], req, True, "ok")
    bal = w.db.execute(select(OnAccountBalance)).scalar_one()
    assert bal.status == "refunded" and bal.remaining_minor == 0


def test_second_part_payment_for_the_exact_remainder_matches_automatically(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    inv = w.invoice(c, "INV/26-27/1500", 100_000_00)
    first = w.credit(60_000_00, "NEFT-HDFCN26274000111-ACME INDUSTRIES PVT LTD-INV/26-27/1500")
    w.run(first)
    p1 = w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == first.id)).scalar_one()
    if p1.status == ProposalStatus.pending_review:
        actions.apply_proposal(w.db, None, p1, "a", w.users["ar_analyst"].id)
    assert inv.open_minor == inv.total_minor - 60_000_00
    second = w.credit(inv.open_minor, "NEFT-HDFCN26274000222-ACME INDUSTRIES PVT LTD-INV/26-27/1500")
    w.run(second)
    p2 = w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == second.id)).scalar_one()
    assert [a.invoice_id for a in p2.allocations] == [inv.id] and p2.short_minor == 0 and p2.unapplied_minor == 0


def test_fifo_guess_skips_disputed_invoices_and_says_so(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    contested = w.invoice(c, "INV/26-27/1601", 50_000_00, days_ago=90)
    contested.disputed = True
    clean = w.invoice(c, "INV/26-27/1602", 50_000_00, days_ago=40)
    t = w.credit(clean.total_minor - 7_00, "NEFT-HDFCN26274000333-ACME INDUSTRIES PVT LTD")
    w.run(t)
    ex = w.db.execute(select(ExceptionCase).where(ExceptionCase.txn_id == t.id)).scalar_one()
    workflow.resolve_exception(w.db, w.users["ar_analyst"], ex, "apply_fifo", {})
    p = w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t.id)).scalar_one()
    assert [a.invoice_id for a in p.allocations] == [clean.id]
    assert contested.open_minor == contested.total_minor, "the contested invoice was left alone"


def test_api_lists_balances_and_enforces_roles(client, w):
    c, inv, t, p = overpaid(w, extra=30_000_00, hold=True)
    w.invoice(c, "INV/26-27/1300", 20_000_00, days_ago=1)
    an = login(client, "ar_analyst@t.example", "pw-ar_analyst")
    au = login(client, "auditor@t.example", "pw-auditor")
    assert client.post(f"/api/v1/advances/propose?entity_id={w.e.id}", headers=au).status_code == 403
    assert client.post(f"/api/v1/advances/propose?entity_id={w.e.id}", headers=an).json()["proposed"] == 1
    out = client.get(f"/api/v1/advances?entity_id={w.e.id}", headers=au).json()
    assert out["total_minor"] == 30_000_00 and out["balances"][0]["remaining_minor"] == 30_000_00 and out["proposals"][0]["invoice"]["number"] == "INV/26-27/1300"
    aid = out["proposals"][0]["id"]
    assert client.post(f"/api/v1/advances/{aid}/accept", headers=au).status_code == 403
    assert client.post(f"/api/v1/advances/{aid}/accept", headers=an).status_code == 200
    assert client.post(f"/api/v1/advances/{aid}/accept", headers=an).status_code == 409


def test_an_undecided_overpayment_is_not_offered_for_application_until_it_is_held_on_account(w):
    c, inv, t, p = overpaid(w, extra=30_000_00)             # exception still open: refund or hold?
    w.invoice(c, "INV/26-27/1300", 20_000_00, days_ago=1)
    assert advances.propose_applications(w.db, w.e)["proposed"] == 0
    ex = w.db.execute(select(ExceptionCase).where(ExceptionCase.type == ExceptionType.overpayment)).scalar_one()
    workflow.resolve_exception(w.db, w.users["ar_analyst"], ex, "on_account", {})
    assert advances.propose_applications(w.db, w.e)["proposed"] == 1
