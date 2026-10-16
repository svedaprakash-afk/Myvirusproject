from __future__ import annotations

from ..indian import mask_account
from ..models import (Adjustment, ApprovalRequest, AuditLog, BankAccount, Customer, ExceptionCase, Invoice, MatchProposal, PaymentTransaction,
                      PostingBatch, PostingLine, TdsEntry, User)


def d(x):
    return x.isoformat() if x else None


def bank(b: BankAccount, pii: bool) -> dict:
    return {"id": b.id, "entity_id": b.entity_id, "bank_name": b.bank_name, "account_no": b.account_no if pii else mask_account(b.account_no),
            "ifsc": b.ifsc, "currency": b.currency, "ledger_code": b.ledger_code, "is_gateway": b.is_gateway, "van_prefix": b.van_prefix}


def customer(c: Customer | None, brief: bool = True) -> dict | None:
    if not c:
        return None
    out = {"id": c.id, "code": c.code, "name": c.name, "tier": c.tier}
    if not brief:
        out |= {"gstin": c.gstin, "pan": c.pan, "payment_terms_days": c.payment_terms_days, "tds_rate_bp": c.tds_rate_bp, "tds_section": c.tds_section,
                "tds_base": c.tds_base, "virtual_account_no": c.virtual_account_no, "email": c.email}
    return out


def txn(t: PaymentTransaction, pii: bool = False) -> dict:
    return {"id": t.id, "entity_id": t.entity_id, "bank_account_id": t.bank_account_id, "bank": t.bank_account.bank_name if t.bank_account else None,
            "txn_date": d(t.txn_date), "value_date": d(t.value_date), "direction": t.direction, "amount_minor": t.amount_minor, "fee_minor": t.fee_minor,
            "receivable_minor": t.receivable_minor, "currency": t.currency, "channel": t.channel, "utr": t.utr, "narration": t.narration,
            "payer_name": t.payer_name, "payer_vpa": t.payer_vpa, "payer_ifsc": t.payer_ifsc, "fx_ccy": t.fx_ccy, "fx_minor": t.fx_minor, "status": t.status.value, "kind": (t.extra or {}).get("kind"),
            "customer": customer(t.customer), "customer_confidence": t.customer_confidence, "remittance_id": t.remittance_id, "source": t.source}


def invoice(i: Invoice) -> dict:
    return {"id": i.id, "entity_id": i.entity_id, "number": i.number, "doc_type": i.doc_type, "customer": customer(i.customer),
            "invoice_date": d(i.invoice_date), "due_date": d(i.due_date), "currency": i.currency, "taxable_minor": i.taxable_minor, "gst_minor": i.gst_minor,
            "total_minor": i.total_minor, "open_minor": i.open_minor, "booking_rate_micro": i.booking_rate_micro, "status": i.status, "disputed": i.disputed, "irn": i.irn, "po_ref": i.po_ref}


def proposal(p: MatchProposal, with_txn: bool = False) -> dict:
    out = {"id": p.id, "entity_id": p.entity_id, "txn_id": p.txn_id, "customer": customer(p.customer) if hasattr(p, "customer") and p.customer else None,
           "status": p.status.value, "strategy": p.strategy, "confidence": p.confidence, "model_version": p.model_version, "explanation": p.explanation,
           "guardrails": p.guardrails, "auto": p.auto, "applied_minor": p.applied_minor, "tds_minor": p.tds_minor, "writeoff_minor": p.writeoff_minor,
           "unapplied_minor": p.unapplied_minor, "short_minor": p.short_minor, "fee_minor": p.fee_minor, "settle_currency": p.settle_currency,
           "settle_rate_micro": p.settle_rate_micro, "writeoff_kind": p.writeoff_kind, "currency": p.settle_currency or (p.txn.currency if p.txn else "INR"), "created_at": d(p.created_at),
           "decided_by": p.decided_by, "created_by": p.created_by, "batch_id": p.batch_id,
           "allocations": [{"invoice_id": a.invoice_id, "number": a.invoice.number, "applied_minor": a.applied_minor, "tds_minor": a.tds_minor,
                            "writeoff_minor": a.writeoff_minor, "tds_section": a.tds_section, "tds_rate_bp": a.tds_rate_bp, "reference_hit": a.reference_hit,
                            "invoice_open_minor": a.invoice.open_minor, "invoice_total_minor": a.invoice.total_minor, "due_date": d(a.invoice.due_date),
                            "disputed": a.invoice.disputed, "currency": a.invoice.currency} for a in p.allocations]}
    if with_txn:
        out["txn"] = txn(p.txn)
    return out


def exception(x: ExceptionCase) -> dict:
    return {"currency": x.txn_currency if hasattr(x, "txn_currency") else None, **{"id": x.id, "entity_id": x.entity_id, "type": x.type.value, "status": x.status.value, "txn_id": x.txn_id, "invoice_id": x.invoice_id,
            "customer": customer(x.customer), "amount_minor": x.amount_minor, "reason": x.reason, "suggestions": x.suggestions, "priority": x.priority,
            "assignee_id": x.assignee_id, "created_at": d(x.created_at), "due_at": d(x.due_at), "resolved_at": d(x.resolved_at), "resolution": x.resolution,
            "notes": [{"id": n.id, "user_id": n.user_id, "text": n.text, "created_at": d(n.created_at)} for n in x.notes]}}


def approval(a: ApprovalRequest, users: dict[int, User] | None = None) -> dict:
    u = users or {}
    nm = lambda i: u[i].name if i in u else None
    return {"id": a.id, "entity_id": a.entity_id, "kind": a.kind.value, "status": a.status.value, "amount_minor": a.amount_minor, "required_role": a.required_role.value,
            "requested_by": a.requested_by, "requested_by_name": nm(a.requested_by), "decided_by": a.decided_by, "decided_by_name": nm(a.decided_by) if a.decided_by else None,
            "reason": a.reason, "decision_note": a.decision_note, "payload": a.payload, "ref_type": a.ref_type, "ref_id": a.ref_id,
            "created_at": d(a.created_at), "decided_at": d(a.decided_at)}


def tds(t: TdsEntry) -> dict:
    return {"id": t.id, "customer": customer(t.customer), "invoice": t.invoice.number, "txn_date": d(t.txn_date), "fy": t.fy, "quarter": t.quarter,
            "section": t.section, "rate_bp": t.rate_bp, "amount_minor": t.amount_minor, "certificate_received": t.certificate_received, "in_26as": t.in_26as,
            "reversed": t.reversed}


def batch(b: PostingBatch, lines: bool = False) -> dict:
    out = {"id": b.id, "entity_id": b.entity_id, "status": b.status.value, "connector": b.connector, "created_by": b.created_by, "approved_by": b.approved_by,
           "created_at": d(b.created_at), "exported_at": d(b.exported_at), "acked_at": d(b.acked_at), "checksum": b.checksum, "stored": bool(b.export_key), "error": b.error,
           "total_minor": b.total_minor, "voucher_count": len({x.voucher_no for x in b.lines})}
    if lines:
        out["lines"] = [{"id": x.id, "voucher_no": x.voucher_no, "line_type": x.line_type, "account": x.account, "debit_minor": x.debit_minor,
                         "credit_minor": x.credit_minor, "currency": x.currency, "base_debit_minor": x.base_debit_minor, "base_credit_minor": x.base_credit_minor,
                         "rate_micro": x.rate_micro, "posting_date": d(x.posting_date), "narration": x.narration, "erp_doc_no": x.erp_doc_no,
                         "txn_id": x.txn_id, "invoice_id": x.invoice_id} for x in b.lines]
    return out


def audit_row(a: AuditLog) -> dict:
    return {"seq": a.seq, "ts": d(a.ts), "actor": a.actor, "action": a.action, "object_type": a.object_type, "object_id": a.object_id,
            "entity_id": a.entity_id, "detail": a.detail, "hash": a.hash, "prev_hash": a.prev_hash}
