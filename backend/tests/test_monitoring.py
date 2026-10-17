"""Drift/health analytics and shadow-model evaluation."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.matching import monitoring, scoring
from app.models import MatchProposal, ProposalStatus, ScoringModel, utcnow
from tests.conftest import login


def mk(w, conf, status, days_ago, *, auto=False, decided=True, shadow=None, shadow_v=None, feats=None):
    t = w.credit(100_00 + w.n, f"NEFT-X{w.n}")
    p = MatchProposal(entity_id=w.e.id, txn_id=t.id, status=status, strategy="exact_reference", confidence=conf, model_version="v1", auto=auto,
                      decided_by=w.users["ar_analyst"].id if decided and not auto else None, features=feats or {"ref_full": 1.0},
                      shadow_confidence=shadow, shadow_model_version=shadow_v, created_by="engine:v1")
    p.created_at = utcnow() - timedelta(days=days_ago)
    w.db.add(p)
    w.db.flush()
    return p


def test_psi_is_none_without_enough_data_and_zero_for_identical_distributions():
    assert monitoring.psi([0.9] * 10, [0.9] * 10) is None
    xs = [0.5, 0.7, 0.8, 0.95, 0.99] * 10
    assert monitoring.psi(xs, xs) == pytest.approx(0, abs=1e-3)
    assert monitoring.psi([0.95] * 40, [0.5] * 40) > 0.25


def test_health_flags_a_confidence_shift_and_reversed_auto_matches(w):
    for _ in range(40):
        mk(w, 0.97, ProposalStatus.auto_approved, 40, auto=True)
    for i in range(40):
        p = mk(w, 0.5, ProposalStatus.pending_review, 3, decided=False)
    rev = mk(w, 0.96, ProposalStatus.reversed, 2, auto=True)
    h = monitoring.health(w.db, w.e.id)
    codes = {a["code"] for a in h["alerts"]}
    assert {"confidence_shift", "auto_reversed"} <= codes and h["status"] == "alert"
    assert h["confidence_psi"] > 0.25 and h["auto_reversed_recent"] == 1
    assert h["feature_shifts"] == [] or all("feature" in r for r in h["feature_shifts"])


def test_health_says_it_cannot_judge_with_little_data_instead_of_reporting_ok_silently(w):
    mk(w, 0.9, ProposalStatus.pending_review, 1, decided=False)
    h = monitoring.health(w.db, w.e.id)
    assert h["confidence_psi"] is None and h["alerts"][0]["code"] == "insufficient_data" and h["status"] == "no_data"


def test_overturn_rate_alert_requires_a_real_increase(w):
    for _ in range(30):
        mk(w, 0.9, ProposalStatus.accepted, 40)          # baseline: all confirmed
    for i in range(30):
        mk(w, 0.9, ProposalStatus.rejected if i % 2 else ProposalStatus.accepted, 3)   # recent: half overturned
    h = monitoring.health(w.db, w.e.id)
    assert "overturn_up" in {a["code"] for a in h["alerts"]}
    assert h["overturn"]["recent"]["rate"] == 0.5 and h["overturn"]["baseline"]["rate"] == 0.0


def test_calibration_reports_gap_between_stated_and_actual_accuracy(w):
    for i in range(60):    # claims 95% but only half are right
        mk(w, 0.95, ProposalStatus.accepted if i % 2 else ProposalStatus.rejected, 5)
    h = monitoring.health(w.db, w.e.id)
    assert h["calibration"]["n_labelled"] == 60 and h["calibration"]["ece"] > 0.4
    assert "miscalibrated" in {a["code"] for a in h["alerts"]}


def _shadow_world(w, better: bool, n=60):
    cand = ScoringModel(version="v9.0-cand", weights=scoring.PRIOR_WEIGHTS, active=False, shadow=True)
    w.db.add(cand)
    w.db.flush()
    for i in range(n):
        y = i % 5 != 0                                   # 80% are actually right
        active_conf = 0.93 if i % 2 else 0.6             # active is noisy: unrelated to the label
        shadow_conf = (0.97 if y else 0.2) if better else 0.93
        mk(w, active_conf, ProposalStatus.accepted if y else ProposalStatus.rejected, 2, shadow=shadow_conf, shadow_v="v9.0-cand")
    return cand


def test_shadow_report_recommends_a_clearly_better_candidate_but_never_promotes_it(w):
    cand = _shadow_world(w, better=True)
    r = monitoring.shadow_report(w.db, w.e.id)
    assert r["recommendation"] == "promote_candidate" and r["candidate"]["logloss"] < r["active"]["logloss"]
    assert r["candidate"]["precision"] >= r["active"]["precision"]
    assert w.db.get(ScoringModel, cand.id).active is False


def test_shadow_report_keeps_the_active_model_when_the_candidate_is_no_better(w):
    _shadow_world(w, better=False)
    r = monitoring.shadow_report(w.db, w.e.id)
    assert r["recommendation"] == "keep_active"


def test_shadow_report_needs_enough_labelled_data(w):
    _shadow_world(w, better=True, n=10)
    assert monitoring.shadow_report(w.db, w.e.id)["recommendation"] == "not_enough_data"


def test_engine_records_the_shadow_score_on_new_proposals_and_activation_clears_the_flag(w, client):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    active = scoring.ensure_model(w.db)
    cand = ScoringModel(version="v2.0-cand", weights={**scoring.PRIOR_WEIGHTS, "ref_full": 1.0}, active=False, shadow=True)
    w.db.add(cand)
    w.db.flush()
    inv = w.invoice(c, "INV/26-27/3001", 100_000_00)
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274000555-ACME INDUSTRIES PVT LTD-INV/26-27/3001")
    w.run(t)
    p = w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t.id)).scalar_one()
    assert p.shadow_model_version == "v2.0-cand" and p.shadow_confidence is not None and p.shadow_confidence < p.confidence
    fin = login(client, "finance_head@t.example", "pw-finance_head")
    assert client.post(f"/api/v1/models/{active.id}/shadow", json={"enabled": True}, headers=fin).status_code == 409
    assert client.get(f"/api/v1/models/shadow?entity_id={w.e.id}", headers=fin).json()["shadow"] == "v2.0-cand"
    assert client.get(f"/api/v1/models/health?entity_id={w.e.id}", headers=fin).status_code == 200
    assert client.post(f"/api/v1/models/{cand.id}/activate", headers=fin).status_code == 200
    assert w.db.get(ScoringModel, cand.id).shadow is False and w.db.get(ScoringModel, cand.id).active
    an = login(client, "ar_analyst@t.example", "pw-ar_analyst")
    assert client.post(f"/api/v1/models/{cand.id}/shadow", json={"enabled": False}, headers=an).status_code == 403
