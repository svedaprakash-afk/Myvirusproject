import math

import pytest

from app.matching import scoring
from app.models import MatchFeedback
from app.seed.evaluate import evaluate


def test_prior_model_orders_evidence_sensibly():
    W = scoring.PRIOR_WEIGHTS
    strong = scoring.predict(W, {"customer_conf": 0.97, "name_agree": 1, "ref_full": 1, "amount_exact": 1, "unique": 1})
    amount_only = scoring.predict(W, {"customer_conf": 0.9, "name_agree": 0.9, "amount_exact": 1, "unique": 1})
    nothing = scoring.predict(W, {})
    ambiguous = scoring.predict(W, {"customer_conf": 0.97, "ref_full": 1, "amount_exact": 1, "ambiguity": 1})
    assert strong > 0.97 > amount_only > nothing and nothing < 0.1 and ambiguous < strong


def test_contributions_sum_to_the_logit():
    W = scoring.PRIOR_WEIGHTS
    f = {"customer_conf": 0.9, "ref_full": 1.0, "amount_exact": 1.0}
    logit = W["bias"] + sum(c["contribution"] for c in scoring.contributions(W, f))
    assert math.isclose(scoring.sigmoid(logit), scoring.predict(W, f), rel_tol=1e-6)


def test_training_reduces_loss_and_keeps_penalties_negative():
    import random

    rng = random.Random(1)
    samples = []
    for _ in range(200):   # a world where amount-only matches from this tenant are mostly WRONG
        samples.append(({"customer_conf": 0.9, "amount_exact": 1.0, "unique": 1.0}, 1 if rng.random() < 0.25 else 0))
        samples.append(({"customer_conf": 0.97, "ref_full": 1.0, "amount_exact": 1.0, "unique": 1.0}, 1 if rng.random() < 0.99 else 0))
    before = scoring.log_loss(scoring.PRIOR_WEIGHTS, samples)
    w = scoring.train(scoring.PRIOR_WEIGHTS, samples)
    assert scoring.log_loss(w, samples) < before
    assert scoring.predict(w, samples[0][0]) < scoring.predict(scoring.PRIOR_WEIGHTS, samples[0][0]), "should learn amount-only is unreliable here"
    assert all(w[k] <= 0 for k in ("ambiguity", "unexplained", "date_anomaly", "partial_pay"))


def test_retrain_needs_balanced_feedback_and_never_self_activates(w):
    db = w.db
    scoring.ensure_model(db)
    assert scoring.retrain_candidate(db) is None
    for i in range(30):
        db.add(MatchFeedback(entity_id=w.e.id, label=1 if i % 3 else 0, features={"customer_conf": 0.9, "ref_full": 1.0}))
    db.flush()
    m = scoring.retrain_candidate(db)
    assert m is not None and m.active is False and "holdout_logloss_candidate" in m.metrics
    assert db.query(scoring.ScoringModel).filter_by(active=True).one().version == "v1.0-prior"


@pytest.mark.parametrize("seed", [42, 7, 11])
def test_engine_safety_on_synthetic_ground_truth(seed):
    """Regression guard. Synthetic data authored alongside the matcher — it proves internal consistency and catches
    regressions, it does NOT prove real-world accuracy (see docs/EVALUATION.md)."""
    r = evaluate(seed, verbose=False)
    assert r["unsafe_auto"] == 0, r["problems"]
    assert r["auto_precision"] == 1.0, r["problems"]
    assert r["false_match_on_nonmatchable"] == 0
    assert r["proposal_coverage"] >= 0.97
    assert r["proposal_accuracy"] >= 0.97, r["problems"]
