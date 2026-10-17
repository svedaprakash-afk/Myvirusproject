"""Period locks and the auditor evidence pack."""
import hashlib
import io
import json
import zipfile
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app import audit
from app.matching import actions
from app.models import AuditLog, MatchProposal, PeriodLock, PostingBatch, ProposalStatus
from app.posting import service as posting
from tests.conftest import TODAY, login


def applied_receipt(w, d, number="INV/26-27/6001", ref="HDFCN26274006001"):
    c = w.cust.get("C1") or w.customer("C1", "Acme Industries Pvt Ltd")
    inv = w.invoice(c, number, 100_000_00)
    t = w.credit(inv.total_minor, f"NEFT-{ref}-ACME INDUSTRIES PVT LTD-{number}", d=d)
    w.run(t)
    p = w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t.id)).scalar_one()
    if p.status == ProposalStatus.pending_review:
        actions.apply_proposal(w.db, None, p, "a", w.users["ar_analyst"].id)
    return t, p


def lock(w, through, user=None):
    w.db.add(PeriodLock(entity_id=w.e.id, locked_through=through, locked_by=(user or w.users["finance_head"]).id))
    w.db.flush()


def test_receipt_in_a_closed_period_is_redated_to_the_first_open_day_and_says_so(w):
    t, p = applied_receipt(w, date(2026, 10, 8))
    lock(w, date(2026, 10, 10))
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    assert {l.posting_date for l in b.lines} == {date(2026, 10, 11)}
    assert all("period closed" in l.narration for l in b.lines if l.line_type == "bank_receipt")
    assert "receipt dated 08-Oct-2026" in next(l.narration for l in b.lines if l.line_type == "bank_receipt")
    assert sum(l.debit_minor for l in b.lines) == sum(l.credit_minor for l in b.lines)


def test_items_after_the_lock_keep_their_date(w):
    t, p = applied_receipt(w, date(2026, 10, 12))
    lock(w, date(2026, 10, 10))
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    assert {l.posting_date for l in b.lines} == {date(2026, 10, 12)} and not any("period closed" in l.narration for l in b.lines)


def test_block_policy_refuses_the_batch_and_names_the_date(w):
    t, p = applied_receipt(w, date(2026, 10, 8))
    lock(w, date(2026, 10, 10))
    w.e.config = {**w.e.config, "period_lock_policy": "block"}
    with pytest.raises(posting.PostingError, match="closed through 10 Oct 2026.*08 Oct 2026"):
        posting.build_batch(w.db, w.e, w.users["ar_manager"])


def test_adjustments_are_redated_too(w):
    from app.models import Adjustment

    c = w.customer("C1", "Acme Industries Pvt Ltd")
    inv = w.invoice(c, "INV-1", 10_000_00)
    w.db.add(Adjustment(entity_id=w.e.id, kind="write_off", invoice_id=inv.id, customer_id=c.id, amount_minor=100_00, currency="INR", debit_account="5900-WRITEOFF",
                        credit_account="1200-AR", reason="rounding", posting_date=date(2026, 9, 30), created_by=w.users["ar_manager"].id))
    w.db.flush()
    lock(w, date(2026, 10, 10))
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    assert {l.posting_date for l in b.lines} == {date(2026, 10, 11)} and all("period closed" in l.narration for l in b.lines)


def test_lock_api_permissions_future_dates_reopen_reason_and_audit(client, w):
    fin, an = login(client, "finance_head@t.example", "pw-finance_head"), login(client, "ar_analyst@t.example", "pw-ar_analyst")
    url = "/api/v1/periods/lock"
    body = lambda **k: {"entity_id": w.e.id, **k}
    assert client.post(url, json=body(locked_through="2026-09-30"), headers=an).status_code == 403
    assert client.post(url, json=body(locked_through="2999-01-01"), headers=fin).status_code == 422
    assert client.post(url, json=body(locked_through="2026-09-30"), headers=fin).status_code == 200
    assert client.get(f"{url}?entity_id={w.e.id}", headers=an).json()["locked_through"] == "2026-09-30"
    assert client.post(url, json=body(locked_through="2026-10-15"), headers=fin).status_code == 200       # moving forward needs no reason
    assert client.post(url, json=body(locked_through="2026-08-31"), headers=fin).status_code == 422       # re-opening does
    assert client.post(url, json=body(locked_through="2026-08-31", note="Audit adjustment agreed with the auditor"), headers=fin).status_code == 200
    assert client.post(url, json=body(), headers=fin).status_code == 422
    assert client.post(url, json=body(note="Year-end re-run approved by CFO"), headers=fin).status_code == 200
    assert client.get(f"{url}?entity_id={w.e.id}", headers=an).json()["locked_through"] is None
    acts = [a.action for a in w.db.execute(select(AuditLog).where(AuditLog.object_type == "LegalEntity")).scalars() if a.action.startswith("period.")]
    assert acts == ["period.locked", "period.locked", "period.reopened", "period.reopened"]


# ------------------------------------------------------------------------------------------------ evidence pack
def pack(client, w, headers, **q):
    r = client.get("/api/v1/audit/evidence-pack", params={"entity_id": w.e.id, **q}, headers=headers)
    assert r.status_code == 200, r.text
    z = zipfile.ZipFile(io.BytesIO(r.content))
    return z, json.loads(z.read("manifest.json")), r


def test_evidence_pack_contains_every_extract_and_hashes_match_the_manifest(client, w):
    t, p = applied_receipt(w, TODAY - timedelta(days=2))
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    posting.release_batch(w.db, b, w.users["treasury"])
    z, m, r = pack(client, w, login(client, "auditor@t.example", "pw-auditor"))
    names = set(z.namelist())
    assert {"README.txt", "manifest.json", "audit_log.csv", "approvals.csv", "match_decisions.csv", "auto_reversals.csv", "posting_batches.csv", "posting_lines.csv",
            "segregation_checks.csv", "exceptions.csv", "advance_applications.csv", "users_roles.csv", "config_snapshot.json", "models.csv"} <= names
    for n, meta in m["files"].items():
        assert hashlib.sha256(z.read(n)).hexdigest() == meta["sha256"], n
    assert m["audit_chain"]["ok"] is True and m["generated_by"] == "auditor@t.example" and m["segregation_findings"] == 0
    assert m["files"]["posting_lines.csv"]["rows"] == len(b.lines) and m["files"]["posting_batches.csv"]["rows"] == 1
    assert "password" not in z.read("users_roles.csv").decode().lower()
    exported = w.db.execute(select(AuditLog).where(AuditLog.action == "evidence.exported")).scalar_one()
    assert exported.detail["sha256"] == hashlib.sha256(r.content).hexdigest()


def test_evidence_pack_reports_a_broken_audit_chain_and_segregation_breaches(client, w):
    t, p = applied_receipt(w, TODAY - timedelta(days=2))
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    b.approved_by = b.created_by                                      # a breach of segregation of duties
    row = w.db.execute(select(AuditLog).order_by(AuditLog.seq)).scalars().first()
    row.detail = {"tampered": True}
    w.db.flush()
    z, m, _ = pack(client, w, login(client, "auditor@t.example", "pw-auditor"))
    assert m["audit_chain"]["ok"] is False and m["audit_chain"]["reason"] == "row content altered"
    assert m["segregation_findings"] >= 1 and "built and released by the same person" in z.read("segregation_checks.csv").decode()


def test_evidence_pack_range_roles_and_scope(client, w):
    t, p = applied_receipt(w, TODAY - timedelta(days=2))
    au, an = login(client, "auditor@t.example", "pw-auditor"), login(client, "ar_analyst@t.example", "pw-ar_analyst")
    assert client.get(f"/api/v1/audit/evidence-pack?entity_id={w.e.id}", headers=an).status_code == 403
    assert client.get(f"/api/v1/audit/evidence-pack?entity_id={w.e.id}&date_from=2026-10-10&date_to=2026-10-01", headers=au).status_code == 400
    assert client.get(f"/api/v1/audit/evidence-pack?entity_id={w.e.id}&date_from=2020-01-01&date_to=2026-10-01", headers=au).status_code == 400
    z, m, _ = pack(client, w, au, date_from="2026-01-01", date_to="2026-01-31")
    assert m["files"]["match_decisions.csv"]["rows"] == 0 and m["files"]["audit_log.csv"]["rows"] == 0, "nothing happened in that window"
    z2, m2, _ = pack(client, w, au, date_from=str(date.today() - timedelta(days=1)), date_to=str(date.today()))
    assert m2["files"]["match_decisions.csv"]["rows"] == 1 and m2["files"]["audit_log.csv"]["rows"] >= 1
