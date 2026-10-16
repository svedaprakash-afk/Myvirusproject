"""Read models for dashboards. SQL aggregation where it matters; small Python loops where clarity wins."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (ApprovalRequest, ApprovalStatus, AuditLog, BankAccount, Customer, CustomerProfile, ExceptionCase, ExceptionStatus,
                     Invoice, MatchProposal, PaymentTransaction, ProposalStatus, TdsEntry, TxnStatus)

OPEN_EXC = [ExceptionStatus.open, ExceptionStatus.in_progress, ExceptionStatus.pending_approval]


def _bucket(days: int) -> str:
    return "0-1d" if days <= 1 else "2-3d" if days <= 3 else "4-7d" if days <= 7 else "8-30d" if days <= 30 else "30d+"


def summary(db: Session, entity_id: int, today: date | None = None) -> dict:
    today = today or date.today()
    unapplied_rows = db.execute(select(PaymentTransaction).where(
        PaymentTransaction.entity_id == entity_id, PaymentTransaction.direction == "credit",
        PaymentTransaction.status.in_([TxnStatus.new, TxnStatus.review, TxnStatus.exception, TxnStatus.partially_matched]))).scalars().all()
    ageing = defaultdict(lambda: {"count": 0, "amount_minor": 0})
    by_account = defaultdict(int)
    for t in unapplied_rows:
        amt = t.receivable_minor
        b = _bucket((today - t.txn_date).days)
        ageing[b]["count"] += 1
        ageing[b]["amount_minor"] += amt
        by_account[t.bank_account_id] += amt
    banks = {b.id: b for b in db.execute(select(BankAccount).where(BankAccount.entity_id == entity_id)).scalars()}
    order = ["0-1d", "2-3d", "4-7d", "8-30d", "30d+"]

    since = today - timedelta(days=30)
    props = db.execute(select(MatchProposal).where(MatchProposal.entity_id == entity_id, MatchProposal.created_at >= since)).scalars().all()
    auto = sum(1 for p in props if p.auto and p.status != ProposalStatus.reversed)
    decided = [p for p in props if p.status in (ProposalStatus.accepted, ProposalStatus.auto_approved, ProposalStatus.posted)]
    reversed_auto = sum(1 for p in props if p.auto and p.status == ProposalStatus.reversed)
    total_credits = db.scalar(select(func.count()).select_from(PaymentTransaction).where(
        PaymentTransaction.entity_id == entity_id, PaymentTransaction.direction == "credit", PaymentTransaction.created_at >= since)) or 0

    exc = db.execute(select(ExceptionCase.type, func.count(), func.coalesce(func.sum(ExceptionCase.amount_minor), 0)).where(
        ExceptionCase.entity_id == entity_id, ExceptionCase.status.in_(OPEN_EXC)).group_by(ExceptionCase.type)).all()
    overdue_exc = db.scalar(select(func.count()).select_from(ExceptionCase).where(
        ExceptionCase.entity_id == entity_id, ExceptionCase.status.in_(OPEN_EXC), ExceptionCase.due_at < __import__("datetime").datetime.utcnow())) or 0

    open_ar = db.execute(select(Invoice).where(Invoice.entity_id == entity_id, Invoice.open_minor != 0)).scalars().all()
    ar_total = sum(i.open_minor for i in open_ar)
    overdue = sum(i.open_minor for i in open_ar if i.due_date < today)
    sales90 = db.scalar(select(func.coalesce(func.sum(Invoice.total_minor), 0)).where(
        Invoice.entity_id == entity_id, Invoice.invoice_date >= today - timedelta(days=90), Invoice.doc_type == "invoice")) or 0
    dso = round(ar_total / sales90 * 90, 1) if sales90 > 0 else None

    tds_wait = db.scalar(select(func.coalesce(func.sum(TdsEntry.amount_minor), 0)).where(
        TdsEntry.entity_id == entity_id, TdsEntry.certificate_received.is_(False), TdsEntry.reversed.is_(False))) or 0
    pending_appr = db.scalar(select(func.count()).select_from(ApprovalRequest).where(
        ApprovalRequest.entity_id == entity_id, ApprovalRequest.status == ApprovalStatus.pending)) or 0
    review = db.scalar(select(func.count()).select_from(MatchProposal).where(
        MatchProposal.entity_id == entity_id, MatchProposal.status == ProposalStatus.pending_review)) or 0

    return {
        "as_of": today.isoformat(),
        "unapplied": {"count": len(unapplied_rows), "amount_minor": sum(t.receivable_minor for t in unapplied_rows),
                      "ageing": [{"bucket": b, **ageing[b]} for b in order],
                      "by_account": [{"bank_account_id": k, "name": banks[k].bank_name if k in banks else str(k), "amount_minor": v} for k, v in by_account.items()]},
        "matching_30d": {"credits": total_credits, "proposals": len(props), "auto_applied": auto, "human_accepted": len(decided) - auto,
                         "stp_rate": round(auto / total_credits, 4) if total_credits else None,
                         "auto_reversed": reversed_auto,
                         "auto_precision_proxy": round(1 - reversed_auto / (auto + reversed_auto), 4) if (auto + reversed_auto) else None},
        "queues": {"review": review, "exceptions": sum(c for _, c, _ in exc), "exceptions_overdue_sla": overdue_exc, "approvals_pending": pending_appr},
        "exceptions_by_type": [{"type": t.value, "count": c, "amount_minor": int(a)} for t, c, a in exc],
        "receivables": {"open_minor": ar_total, "overdue_minor": overdue, "dso_days": dso,
                        "ageing": _ar_ageing(open_ar, today)},
        "tds": {"awaiting_certificate_minor": int(tds_wait)},
    }


def _ar_ageing(invs: list[Invoice], today: date) -> list[dict]:
    b = {"Not due": 0, "1-30": 0, "31-60": 0, "61-90": 0, "90+": 0}
    for i in invs:
        d = (today - i.due_date).days
        k = "Not due" if d <= 0 else "1-30" if d <= 30 else "31-60" if d <= 60 else "61-90" if d <= 90 else "90+"
        b[k] += i.open_minor
    return [{"bucket": k, "amount_minor": v} for k, v in b.items()]


def match_trend(db: Session, entity_id: int, days: int = 30) -> list[dict]:
    since = date.today() - timedelta(days=days)
    rows = db.execute(select(MatchProposal.created_at, MatchProposal.auto, MatchProposal.created_by).where(
        MatchProposal.entity_id == entity_id, MatchProposal.created_at >= since)).all()
    d: dict[str, dict] = defaultdict(lambda: {"auto": 0, "review": 0, "manual": 0})
    for ts, auto, by in rows:
        d[ts.date().isoformat()]["manual" if by.startswith("user") else "auto" if auto else "review"] += 1
    return [{"date": k, **v} for k, v in sorted(d.items())]


def cash_position(db: Session, entity_id: int) -> list[dict]:
    out = []
    for b in db.execute(select(BankAccount).where(BankAccount.entity_id == entity_id)).scalars():
        last = db.execute(select(PaymentTransaction).where(PaymentTransaction.bank_account_id == b.id, PaymentTransaction.balance_minor.is_not(None))
                          .order_by(PaymentTransaction.txn_date.desc(), PaymentTransaction.id.desc()).limit(1)).scalars().first()
        credits30 = db.scalar(select(func.coalesce(func.sum(PaymentTransaction.amount_minor), 0)).where(
            PaymentTransaction.bank_account_id == b.id, PaymentTransaction.direction == "credit",
            PaymentTransaction.txn_date >= date.today() - timedelta(days=30))) or 0
        out.append({"bank_account_id": b.id, "name": b.bank_name, "balance_minor": last.balance_minor if last else None,
                    "balance_as_of": last.txn_date.isoformat() if last else None, "credits_30d_minor": int(credits30), "currency": b.currency})
    return out


def forecast(db: Session, entity_id: int, weeks: int = 8, today: date | None = None) -> dict:
    """Behaviour-adjusted due-date projection: each open invoice is expected on due date + that customer's observed average
    lateness (from accepted matches). It is a transparent heuristic, not a statistical forecast, and says so."""
    today = today or date.today()
    prof = {p.customer_id: p for p in db.execute(select(CustomerProfile)).scalars()}
    buckets = [0] * (weeks + 1)
    for i in db.execute(select(Invoice).where(Invoice.entity_id == entity_id, Invoice.open_minor > 0, Invoice.disputed.is_(False))).scalars():
        p = prof.get(i.customer_id)
        late = int(round(p.avg_days_late)) if p and p.payments >= 3 else 0
        exp = max(i.due_date + timedelta(days=max(late, 0)), today)
        w = min((exp - today).days // 7, weeks)
        buckets[w] += i.open_minor
    return {"method": "due date + customer's historical average lateness (customers with <3 observed payments use due date)",
            "weeks": [{"week_start": (today + timedelta(days=7 * k)).isoformat(), "amount_minor": v, "overflow": k == weeks} for k, v in enumerate(buckets)]}


def productivity(db: Session, entity_id: int, days: int = 30) -> list[dict]:
    since = date.today() - timedelta(days=days)
    rows = db.execute(select(AuditLog.actor, AuditLog.action, func.count()).where(
        AuditLog.entity_id == entity_id, AuditLog.ts >= since, AuditLog.actor != "engine", AuditLog.actor != "seed",
        AuditLog.action.in_(["proposal.accepted", "proposal.rejected", "match.manual", "approval.approved", "approval.rejected"]) |
        AuditLog.action.like("exception.%")).group_by(AuditLog.actor, AuditLog.action)).all()
    d: dict[str, dict] = defaultdict(lambda: {"accepted": 0, "rejected": 0, "manual": 0, "exceptions_worked": 0, "approvals": 0})
    for actor, act, n in rows:
        r = d[actor]
        if act == "proposal.accepted":
            r["accepted"] += n
        elif act == "proposal.rejected":
            r["rejected"] += n
        elif act == "match.manual":
            r["manual"] += n
        elif act.startswith("exception."):
            r["exceptions_worked"] += n
        else:
            r["approvals"] += n
    resolved = db.execute(select(ExceptionCase).where(ExceptionCase.entity_id == entity_id, ExceptionCase.resolved_at.is_not(None),
                                                      ExceptionCase.resolved_at >= since)).scalars().all()
    sla = {"resolved": len(resolved), "within_sla": sum(1 for x in resolved if x.due_at and x.resolved_at <= x.due_at)}
    return {"users": [{"user": k, **v} for k, v in sorted(d.items())], "sla": sla}
