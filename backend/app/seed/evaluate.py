"""Measure the engine against generator ground truth.   python -m app.seed.evaluate [--seed N]"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from sqlalchemy import select

from ..db import Base, make_engine
from ..models import ExceptionCase, MatchProposal, PaymentTransaction, ProposalStatus
from sqlalchemy.orm import sessionmaker
from .demo import seed_demo
from .generator import build


def evaluate(seed: int = 42, verbose: bool = True) -> dict:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, expire_on_commit=False)()
    ds = build(seed=seed)
    summary = seed_demo(db, ds, seed=seed)
    db.commit()
    txns = db.execute(select(PaymentTransaction).where(PaymentTransaction.direction == "credit").order_by(PaymentTransaction.id)).scalars().all()
    index: dict[tuple, list[PaymentTransaction]] = defaultdict(list)
    for t in txns:
        index[(t.txn_date.isoformat(), t.receivable_minor, " ".join(t.narration.split()))].append(t)
    props: dict[int, MatchProposal] = {}
    for p in db.execute(select(MatchProposal).where(MatchProposal.status.in_([ProposalStatus.auto_approved, ProposalStatus.pending_review, ProposalStatus.accepted]))).scalars():
        props[p.txn_id] = p
    excs: dict[int, list[str]] = defaultdict(list)
    for x in db.execute(select(ExceptionCase)).scalars():
        if x.txn_id:
            excs[x.txn_id].append(x.type.value)

    res = Counter()
    by_scn: dict[str, Counter] = defaultdict(Counter)
    problems: list[str] = []
    for tr in ds.truth:
        cands = index.get((tr["date"], tr["amount_minor"], " ".join(tr["narration"].split())))
        if not cands:
            by_scn[tr["scenario"]]["missing"] += 1
            problems.append(f"MISSING {tr['scenario']} {tr['narration'][:70]}")
            continue
        t = cands[0]
        p = props.get(t.id)
        got = sorted(a.invoice.number for a in p.allocations) if p else []
        want = sorted(tr["expect_invoices"])
        either = want[0][7:].split("|") if want and want[0].startswith("EITHER:") else None
        outcome = "auto" if p and p.status == ProposalStatus.auto_approved else ("review" if p else "exception")
        sc = by_scn[tr["scenario"]]
        sc["n"] += 1
        sc[f"out_{outcome}"] += 1
        if tr["expect"] in ("review", "not_auto", "exception") and outcome == "auto":
            res["unsafe_auto"] += 1
            problems.append(f"UNSAFE AUTO {tr['scenario']:<18} conf={p.confidence:.2f} guard={p.guardrails} | {t.narration[:70]}")
        if want:
            correct = (len(got) == 1 and got[0] in either) if either else got == want
            sc["inv_correct" if correct else "inv_wrong"] += 1
            if not p:
                problems.append(f"MISSED {tr['scenario']:<18} exc={excs.get(t.id)} want={want} | {t.narration[:80]}")
            if p and not correct:
                problems.append(f"WRONG {tr['scenario']:<18} {outcome:<6} conf={p.confidence:.2f} got={got} want={want} | {t.narration[:70]}")
            if outcome == "auto":
                res["auto"] += 1
                res["auto_correct"] += correct
            if outcome in ("auto", "review"):
                res["proposed"] += 1
                res["proposed_correct"] += correct
            res["expected_matchable"] += 1
        else:
            if outcome != "exception":
                problems.append(f"UNEXPECTED MATCH {tr['scenario']} -> {got} | {t.narration[:70]}")
                res["false_match_on_nonmatchable"] += 1
            elif tr["expect_exception"] and tr["expect_exception"] not in excs.get(t.id, []):
                problems.append(f"EXC TYPE {tr['scenario']} want={tr['expect_exception']} got={excs.get(t.id)}")
    total_credit = len(txns)
    out = {"customer_credits": total_credit, "auto_approved": res["auto"], "auto_precision": res["auto_correct"] / res["auto"] if res["auto"] else None,
           "stp_rate_of_matchable": res["auto"] / res["expected_matchable"] if res["expected_matchable"] else None,
           "proposal_coverage": res["proposed"] / res["expected_matchable"] if res["expected_matchable"] else None,
           "proposal_accuracy": res["proposed_correct"] / res["proposed"] if res["proposed"] else None,
           "unsafe_auto": res["unsafe_auto"], "false_match_on_nonmatchable": res["false_match_on_nonmatchable"], "matching": summary["matching"]}
    if verbose:
        print(f"seed={seed}  credits={total_credit}  engine run={summary['matching']}")
        for k, v in out.items():
            if isinstance(v, float):
                print(f"  {k:<30} {v:.1%}")
            elif k in ("unsafe_auto", "false_match_on_nonmatchable"):
                print(f"  {k:<30} {v}")
        print(f"\n{'scenario':<20}{'n':>4}{'auto':>6}{'review':>8}{'exc':>5}{'inv✓':>6}{'inv✗':>6}")
        for k, c in sorted(by_scn.items()):
            print(f"{k:<20}{c['n']:>4}{c['out_auto']:>6}{c['out_review']:>8}{c['out_exception']:>5}{c['inv_correct']:>6}{c['inv_wrong']:>6}")
        print("\nProblems:")
        for pr in problems[:40]:
            print("  ", pr)
    out["problems"] = problems
    db.close()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    evaluate(ap.parse_args().seed)
