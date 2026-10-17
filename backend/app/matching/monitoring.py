"""Model health and shadow evaluation. Read-only analytics: nothing here changes a decision.

* `health` compares the recent window with a baseline window (confidence distribution via PSI, feature means, reviewer overturn rate,
  reversed auto-matches) and checks calibration on human-labelled proposals.
* `shadow_report` scores a candidate model on the same live proposals the active model decided, after humans labelled them.

Honest limits (also returned in every payload): labels only exist for proposals a human decided or reversed, so they over-represent
the hard cases; shadow scores are computed on the candidate the ACTIVE model chose, so a shadow model can only be compared on those
candidates, not on ones the active model never produced. Treat both as evidence for a human promotion decision, never as an auto-promote trigger.
"""
from __future__ import annotations

import math
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DEFAULT_ENTITY_CONFIG, LegalEntity, MatchProposal, ProposalStatus, utcnow

BINS = [0.0, 0.45, 0.6, 0.75, 0.9, 0.97, 1.0001]
MIN_N = 30
CAVEATS = ["Labels exist only for proposals a human decided or reversed; they over-represent hard cases.",
           "Shadow scores cover only the candidates the active model produced."]


def _bin(x: float) -> int:
    for i in range(len(BINS) - 1):
        if BINS[i] <= x < BINS[i + 1]:
            return i
    return len(BINS) - 2


def psi(base: list[float], recent: list[float]) -> float | None:
    """Population stability index on fixed confidence bins. <0.1 stable, 0.1-0.25 moderate shift, >0.25 major shift."""
    if len(base) < MIN_N or len(recent) < MIN_N:
        return None
    k = len(BINS) - 1
    b, r = [1e-4] * k, [1e-4] * k
    for x in base:
        b[_bin(x)] += 1
    for x in recent:
        r[_bin(x)] += 1
    sb, sr = sum(b), sum(r)
    return round(sum((r[i] / sr - b[i] / sb) * math.log((r[i] / sr) / (b[i] / sb)) for i in range(k)), 4)


def _label(p: MatchProposal) -> int | None:
    if p.status in (ProposalStatus.accepted, ProposalStatus.posted) and not p.auto and p.decided_by:
        return 1
    if p.status in (ProposalStatus.rejected, ProposalStatus.reversed):
        return 0
    return None


def _mean_feats(rows: list[MatchProposal]) -> dict[str, float]:
    keys = {k for p in rows for k in (p.features or {})}
    return {k: sum((p.features or {}).get(k, 0.0) for p in rows) / len(rows) for k in keys} if rows else {}


def _overturn(rows: list[MatchProposal]) -> tuple[int, int]:
    """(overturned, judged): judged = human decisions + reversals of auto-matches."""
    over = sum(1 for p in rows if p.status in (ProposalStatus.rejected, ProposalStatus.reversed))
    ok = sum(1 for p in rows if _label(p) == 1)
    return over, over + ok


def health(db: Session, entity_id: int, window_days: int = 14, baseline_days: int = 60) -> dict:
    now = utcnow()
    cut = now - timedelta(days=window_days)
    start = cut - timedelta(days=baseline_days)
    rows = db.execute(select(MatchProposal).where(MatchProposal.entity_id == entity_id, MatchProposal.created_at >= start)).scalars().all()
    recent = [p for p in rows if p.created_at >= cut]
    base = [p for p in rows if p.created_at < cut]
    out: dict = {"entity_id": entity_id, "window_days": window_days, "baseline_days": baseline_days, "n_recent": len(recent), "n_baseline": len(base),
                 "alerts": [], "caveats": CAVEATS}
    s = psi([p.confidence for p in base], [p.confidence for p in recent])
    out["confidence_psi"] = s
    if s is None:
        out["alerts"].append({"level": "info", "code": "insufficient_data", "message": f"Need at least {MIN_N} proposals in both the recent and baseline windows to test for drift."})
    elif s > 0.25:
        out["alerts"].append({"level": "high", "code": "confidence_shift", "message": f"Confidence distribution shifted a lot (PSI {s}). Inputs may have changed (new bank format, new customers, data problem)."})
    elif s > 0.1:
        out["alerts"].append({"level": "medium", "code": "confidence_shift", "message": f"Confidence distribution moved moderately (PSI {s})."})

    n_auto = sum(1 for p in recent if p.auto)
    out["rates"] = {"auto": round(n_auto / len(recent), 3) if recent else None,
                    "review": round(sum(1 for p in recent if p.status == ProposalStatus.pending_review) / len(recent), 3) if recent else None}
    bauto = sum(1 for p in base if p.auto)
    out["rates"]["auto_baseline"] = round(bauto / len(base), 3) if base else None

    ro, rj = _overturn(recent)
    bo, bj = _overturn(base)
    out["overturn"] = {"recent": {"overturned": ro, "judged": rj, "rate": round(ro / rj, 3) if rj else None},
                       "baseline": {"overturned": bo, "judged": bj, "rate": round(bo / bj, 3) if bj else None}}
    if rj >= 20 and bj >= 20 and ro / rj - bo / bj > 0.10:
        out["alerts"].append({"level": "high", "code": "overturn_up", "message": f"Reviewers are overturning {ro / rj:.0%} of judged matches, up from {bo / bj:.0%}."})
    rev_auto = [p for p in recent if p.status == ProposalStatus.reversed and p.auto]
    out["auto_reversed_recent"] = len(rev_auto)
    if rev_auto:
        out["alerts"].append({"level": "high", "code": "auto_reversed", "message": f"{len(rev_auto)} straight-through match(es) were reversed by a human in the last {window_days} days. Review them before trusting auto-posting."})

    mr, mb = _mean_feats(recent), _mean_feats(base)
    shifts = []
    if len(recent) >= MIN_N and len(base) >= MIN_N:
        for k in mr.keys() | mb.keys():
            a, b = mr.get(k, 0.0), mb.get(k, 0.0)
            shifts.append({"feature": k, "baseline": round(b, 3), "recent": round(a, 3), "delta": round(a - b, 3)})
        shifts.sort(key=lambda r: -abs(r["delta"]))
    out["feature_shifts"] = shifts[:5]

    lab = [(p.confidence, _label(p)) for p in rows if _label(p) is not None]
    cal = []
    ece = None
    if len(lab) >= 50:
        ece = 0.0
        for i in range(len(BINS) - 1):
            grp = [(c, y) for c, y in lab if _bin(c) == i]
            if not grp:
                continue
            pc, ac = sum(c for c, _ in grp) / len(grp), sum(y for _, y in grp) / len(grp)
            cal.append({"bin": f"{BINS[i]:.2f}-{min(BINS[i + 1], 1.0):.2f}", "n": len(grp), "predicted": round(pc, 3), "actual": round(ac, 3)})
            ece += len(grp) / len(lab) * abs(pc - ac)
        ece = round(ece, 4)
        if ece > 0.1:
            out["alerts"].append({"level": "medium", "code": "miscalibrated", "message": f"Stated confidence is off from observed accuracy by {ece:.0%} on average (expected calibration error)."})
    out["calibration"] = {"n_labelled": len(lab), "ece": ece, "bins": cal}
    out["status"] = ("alert" if any(a["level"] == "high" for a in out["alerts"]) else "watch" if any(a["level"] == "medium" for a in out["alerts"])
                     else "no_data" if s is None else "ok")
    return out


def shadow_report(db: Session, entity_id: int) -> dict:
    from .scoring import shadow_model

    sm = shadow_model(db)
    e = db.get(LegalEntity, entity_id)
    thr = {**DEFAULT_ENTITY_CONFIG, **((e.config if e else None) or {})}["auto_approve_threshold"]
    if not sm:
        return {"shadow": None, "message": "No shadow model is running. Start one from a trained candidate.", "caveats": CAVEATS}
    rows = db.execute(select(MatchProposal).where(MatchProposal.entity_id == entity_id, MatchProposal.shadow_model_version == sm.version,
                                                  MatchProposal.shadow_confidence.is_not(None))).scalars().all()
    lab = [(p.confidence, p.shadow_confidence, _label(p)) for p in rows if _label(p) is not None]
    out: dict = {"shadow": sm.version, "scored": len(rows), "labelled": len(lab), "threshold": thr, "caveats": CAVEATS}
    if len(lab) < 20:
        out.update(recommendation="not_enough_data", message=f"{len(lab)} human-labelled proposals so far; at least 50 are needed for a meaningful comparison.")
        return out

    def ll(idx):
        eps = 1e-9
        return -sum(y * math.log(r[idx] + eps) + (1 - y) * math.log(1 - r[idx] + eps) for r in lab for y in [r[2]]) / len(lab)

    def brier(idx):
        return sum((r[idx] - r[2]) ** 2 for r in lab) / len(lab)

    def auto_stats(idx):
        sel = [r for r in lab if r[idx] >= thr]
        return {"would_auto": len(sel), "precision": round(sum(r[2] for r in sel) / len(sel), 4) if sel else None}

    a, s = auto_stats(0), auto_stats(1)
    out["active"] = {"logloss": round(ll(0), 4), "brier": round(brier(0), 4), **a}
    out["candidate"] = {"logloss": round(ll(1), 4), "brier": round(brier(1), 4), **s}
    out["disagreements"] = {"candidate_would_auto_active_would_not": sum(1 for r in lab if r[1] >= thr > r[0]),
                            "active_would_auto_candidate_would_not": sum(1 for r in lab if r[0] >= thr > r[1])}
    better_loss = out["candidate"]["logloss"] <= out["active"]["logloss"] * 0.98
    precise = (s["precision"] or 0) >= (a["precision"] or 0) - 0.01 if s["would_auto"] else False
    volume = s["would_auto"] >= 0.8 * a["would_auto"] if a["would_auto"] else True
    if len(lab) < 50:
        rec, msg = "not_enough_data", f"Only {len(lab)} labelled proposals; wait for at least 50."
    elif better_loss and precise and volume:
        rec, msg = "promote_candidate", "Candidate has lower log-loss, equal-or-better precision at the auto-approve threshold and does not cut straight-through volume by more than 20%. A human still decides."
    elif better_loss and not precise:
        rec, msg = "keep_active", "Candidate calibrates better overall but would auto-approve less precisely at your threshold."
    else:
        rec, msg = "keep_active", "Candidate is not clearly better than the active model on live decisions."
    out.update(recommendation=rec, message=msg)
    return out
