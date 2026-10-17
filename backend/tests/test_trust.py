"""Per-customer graduation to straight-through."""
from sqlalchemy import select

from app.matching import actions, trust
from app.models import CustomerTrust, MatchProposal, ProposalStatus
from tests.conftest import login


def enable(w, n=3):
    w.e.config = {**w.e.config, "trust_graduation": {"enabled": True, "graduate_after": n, "demote_on_correction": True}}
    w.db.flush()


def pay(w, cust, i, amount=100_000_00):
    inv = w.invoice(cust, f"INV/26-27/{2000 + i}", amount)
    t = w.credit(inv.total_minor, f"NEFT-HDFCN2627{4000 + i:06d}-ACME INDUSTRIES PVT LTD-INV/26-27/{2000 + i}")
    w.run(t)
    return w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t.id)).scalar_one()


def test_disabled_by_default_so_clean_matches_stay_straight_through(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    p = pay(w, c, 1)
    assert p.status == ProposalStatus.auto_approved and not any("graduated" in g for g in p.guardrails)


def test_customer_graduates_after_n_consecutive_confirmed_matches(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    enable(w, 3)
    for i in range(3):
        p = pay(w, c, i)
        assert p.status == ProposalStatus.pending_review, "still learning"
        assert any(f"{i}/3" in g for g in p.guardrails)
        actions.apply_proposal(w.db, None, p, "analyst", w.users["ar_analyst"].id)
    row = w.db.get(CustomerTrust, c.id)
    assert row.mode == "auto" and row.confirmed == 3 and row.clean_streak == 3
    p = pay(w, c, 9)
    assert p.status == ProposalStatus.auto_approved and p.guardrails == []


def test_a_rejection_resets_the_streak_and_demotes_an_auto_customer(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    enable(w, 2)
    for i in range(2):
        actions.apply_proposal(w.db, None, pay(w, c, i), "a", w.users["ar_analyst"].id)
    assert w.db.get(CustomerTrust, c.id).mode == "auto"
    p = pay(w, c, 5)
    assert p.status == ProposalStatus.auto_approved
    # a reviewer reverses the auto match: strongest negative signal
    actions.reverse_proposal(w.db, p, "analyst", w.users["ar_analyst"].id, "paid twice by mistake")
    row = w.db.get(CustomerTrust, c.id)
    assert row.mode == "learning" and row.clean_streak == 0 and row.corrections == 1 and not row.pinned
    assert pay(w, c, 6).status == ProposalStatus.pending_review


def test_reject_resets_streak_without_demoting_a_learning_customer(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    enable(w, 3)
    actions.apply_proposal(w.db, None, pay(w, c, 1), "a", w.users["ar_analyst"].id)
    actions.reject_proposal(w.db, pay(w, c, 2), "a", w.users["ar_analyst"].id, "wrong invoice")
    row = w.db.get(CustomerTrust, c.id)
    assert row.clean_streak == 0 and row.confirmed == 1 and row.corrections == 1 and row.mode == "learning"


def test_pinned_suggest_only_blocks_straight_through_and_moving_to_learning_unpins(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    trust.set_mode(w.db, c, "suggest_only", None, False, "disputes history", "mgr", w.users["ar_manager"].id)
    p = pay(w, c, 1)
    assert p.status == ProposalStatus.pending_review and any("suggest-only" in g for g in p.guardrails)
    trust.set_mode(w.db, c, "learning", None, False, "", "mgr", w.users["ar_manager"].id)
    assert w.db.get(CustomerTrust, c.id).pinned is False


def test_threshold_override_can_only_raise_the_bar(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    trust.set_mode(w.db, c, None, 0.999, False, "strict customer", "mgr", w.users["ar_manager"].id)
    p = pay(w, c, 1)
    assert p.status == ProposalStatus.pending_review and any("stricter threshold" in g for g in p.guardrails)
    row = w.db.get(CustomerTrust, c.id)
    assert trust.gate(w.e.config, row, 0.95) and not trust.gate({**w.e.config, "auto_approve_threshold": 0.9}, None, 0.95)


def test_manual_allocation_counts_as_a_correction_not_a_confirmation(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    enable(w, 3)
    actions.apply_proposal(w.db, None, pay(w, c, 1), "a", w.users["ar_analyst"].id)
    from app.models import MatchProposal as MP
    mp = MP(entity_id=w.e.id, txn_id=pay(w, c, 2).txn_id, customer_id=c.id, status=ProposalStatus.pending_review, strategy="manual", confidence=1.0,
            model_version="manual", features={}, created_by="user:1")
    w.db.add(mp)
    w.db.flush()
    actions.apply_proposal(w.db, None, mp, "a", w.users["ar_analyst"].id)
    assert w.db.get(CustomerTrust, c.id).clean_streak == 0


def test_api_pinning_needs_manager_authority_and_a_reason_and_is_audited(client, w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    w.db.flush()
    an = login(client, "ar_analyst@t.example", "pw-ar_analyst")
    mg = login(client, "ar_manager@t.example", "pw-ar_manager")
    assert client.post(f"/api/v1/customers/{c.id}/trust", json={"mode": "auto", "note": "trusted"}, headers=an).status_code == 403
    assert client.post(f"/api/v1/customers/{c.id}/trust", json={"mode": "auto"}, headers=mg).status_code == 422
    r = client.post(f"/api/v1/customers/{c.id}/trust", json={"mode": "auto", "note": "Paid cleanly for 3 years"}, headers=mg)
    assert r.status_code == 200 and r.json()["mode"] == "auto" and r.json()["pinned"] is True
    assert client.get(f"/api/v1/customers/{c.id}/trust", headers=an).json()["mode"] == "auto"
    cfg = client.put(f"/api/v1/entities/{w.e.id}/config", json={"trust_graduation": {"enabled": True, "graduate_after": 4}},
                     headers=login(client, "admin@t.example", "pw-admin"))
    assert cfg.status_code == 200 and cfg.json()["config"]["trust_graduation"] == {"enabled": True, "graduate_after": 4, "demote_on_correction": True}
    items = client.get(f"/api/v1/customers?entity_id={w.e.id}", headers=an).json()["items"]
    assert items[0]["trust_mode"] == "auto"


def test_a_reversed_auto_match_demotes_even_a_pinned_auto_customer_and_clears_the_pin(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    trust.set_mode(w.db, c, "auto", None, False, "trusted payer", "mgr", w.users["ar_manager"].id)
    enable(w, 3)
    p = pay(w, c, 1)
    assert p.status == ProposalStatus.auto_approved
    actions.reverse_proposal(w.db, p, "a", w.users["ar_analyst"].id, "customer says it was for another invoice")
    row = w.db.get(CustomerTrust, c.id)
    assert row.mode == "learning" and row.pinned is False
