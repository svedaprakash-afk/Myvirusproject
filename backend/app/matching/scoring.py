"""Versioned logistic scorer with online learning from analyst feedback.

Why logistic regression and not a black box: every confidence is a sum of named, signed contributions that
we store and show to analysts and auditors. Weights are versioned; a retrained model is only a *candidate*
until an authorised user activates it, and any earlier version can be re-activated (rollback).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MatchFeedback, ScoringModel

FEATURES = [
    "customer_conf", "name_agree", "ref_full", "ref_serial", "remit", "amount_exact", "amount_tol", "tds_explained",
    "tds_usual", "unique", "fifo", "history", "ambiguity", "unexplained", "date_anomaly", "partial_pay", "bundle_size",
]
PRIOR_WEIGHTS = {
    "bias": -3.2, "customer_conf": 2.0, "name_agree": 0.6, "ref_full": 3.2, "ref_serial": 1.4, "remit": 2.6,
    "amount_exact": 2.1, "amount_tol": 1.1, "tds_explained": 0.7, "tds_usual": 0.9, "unique": 0.9, "fifo": 0.5,
    "history": 0.6, "ambiguity": -2.2, "unexplained": -3.4, "date_anomaly": -1.2, "partial_pay": -0.8, "bundle_size": -0.5,
}
LABELS = {
    "customer_conf": "Payer identified", "name_agree": "Payer name similarity", "ref_full": "Invoice number in reference",
    "ref_serial": "Invoice serial in reference", "remit": "Remittance advice", "amount_exact": "Amount matches exactly",
    "amount_tol": "Amount within tolerance", "tds_explained": "Shortfall explained by TDS", "tds_usual": "TDS rate is customer's usual",
    "unique": "Only one plausible combination", "fifo": "Oldest invoices first", "history": "Fits customer's payment history",
    "ambiguity": "Competing combinations", "unexplained": "Unexplained difference", "date_anomaly": "Payment predates invoice",
    "partial_pay": "Partial payment", "bundle_size": "Many invoices in one payment", "bias": "Baseline",
}


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(min(x, 30), -30)))


def predict(weights: dict, feats: dict) -> float:
    return sigmoid(weights.get("bias", 0.0) + sum(weights.get(k, 0.0) * feats.get(k, 0.0) for k in FEATURES))


def contributions(weights: dict, feats: dict) -> list[dict]:
    rows = [{"feature": k, "label": LABELS[k], "value": round(feats.get(k, 0.0), 3), "weight": weights.get(k, 0.0),
             "contribution": round(weights.get(k, 0.0) * feats.get(k, 0.0), 3)} for k in FEATURES if feats.get(k, 0.0)]
    rows.sort(key=lambda r: -abs(r["contribution"]))
    return rows


def ensure_model(db: Session) -> ScoringModel:
    m = db.execute(select(ScoringModel).where(ScoringModel.active.is_(True))).scalar_one_or_none()
    if m:
        return m
    m = ScoringModel(version="v1.0-prior", weights=PRIOR_WEIGHTS, active=True, note="Hand-calibrated prior (no training data)")
    db.add(m)
    db.flush()
    return m


def log_loss(weights: dict, samples: list[tuple[dict, int]]) -> float:
    if not samples:
        return float("nan")
    eps = 1e-9
    return -sum(y * math.log(predict(weights, f) + eps) + (1 - y) * math.log(1 - predict(weights, f) + eps) for f, y in samples) / len(samples)


def train(weights: dict, samples: list[tuple[dict, int]], epochs: int = 40, lr: float = 0.15, l2_prior: float = 0.05) -> dict:
    """SGD on log-loss with an L2 pull toward the prior (so a handful of corrections can't wreck the model)."""
    w = dict(weights)
    for _ in range(epochs):
        for f, y in samples:
            err = predict(w, f) - y
            w["bias"] = w.get("bias", 0.0) - lr * (err + l2_prior * (w.get("bias", 0.0) - PRIOR_WEIGHTS["bias"]))
            for k in FEATURES:
                x = f.get(k, 0.0)
                w[k] = w.get(k, 0.0) - lr * (err * x + l2_prior * (w.get(k, 0.0) - PRIOR_WEIGHTS[k]))
    # keep signs of the semantic features sane: penalties stay <= 0, evidence stays >= 0
    for k in FEATURES:
        if k in ("ambiguity", "unexplained", "date_anomaly", "partial_pay", "bundle_size"):
            w[k] = min(w[k], 0.0)
        else:
            w[k] = max(w[k], 0.0)
    return {k: round(v, 4) for k, v in w.items()}


def retrain_candidate(db: Session, entity_id: int | None = None) -> ScoringModel | None:
    q = select(MatchFeedback)
    if entity_id:
        q = q.where(MatchFeedback.entity_id == entity_id)
    rows = db.execute(q.order_by(MatchFeedback.id)).scalars().all()
    pos = sum(r.label for r in rows)
    if len(rows) < 20 or pos < 10 or len(rows) - pos < 5:
        return None  # too little (or one-sided) feedback: a model trained on it would only learn the base rate
    samples = [(r.features, r.label) for r in rows]
    cut = int(len(samples) * 0.8)
    train_s, hold = samples[:cut], samples[cut:]
    active = ensure_model(db)
    new_w = train(active.weights, train_s)
    metrics = {"n_train": len(train_s), "n_holdout": len(hold),
               "holdout_logloss_active": round(log_loss(active.weights, hold), 4),
               "holdout_logloss_candidate": round(log_loss(new_w, hold), 4)}
    n = db.query(ScoringModel).count() + 1
    m = ScoringModel(version=f"v{n}.0-learned", weights=new_w, active=False, metrics=metrics,
                     note=f"Retrained on {len(samples)} analyst decisions; inactive until promoted")
    db.add(m)
    db.flush()
    return m
