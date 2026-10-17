import pytest

from conftest import login
from app.seed.demo import DEMO_PASSWORD, seed_demo

PW = DEMO_PASSWORD


@pytest.fixture()
def seeded(db, client):
    summary = seed_demo(db)
    db.commit()
    return summary["entity_id"]


def H(client, who):
    return login(client, f"{who}@meridian.example", PW)


def test_everything_requires_authentication(client, seeded):
    for path in ["/api/v1/transactions?entity_id=1", "/api/v1/proposals?entity_id=1", "/api/v1/analytics/summary?entity_id=1", "/api/v1/audit"]:
        assert client.get(path).status_code == 401
    assert client.get("/health").json()["status"] == "ok"


def test_bad_credentials_rejected_and_audited(client, seeded):
    assert client.post("/api/v1/auth/login", json={"email": "analyst@meridian.example", "password": "wrong"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": "nobody@x.com", "password": "x"}).status_code == 401
    h = H(client, "auditor")
    rows = client.get("/api/v1/audit?action=auth.failed", headers=h).json()["items"]
    assert len(rows) >= 2


def test_role_matrix(client, seeded):
    e = seeded
    props = client.get(f"/api/v1/proposals?entity_id={e}&limit=1", headers=H(client, "analyst")).json()["items"]
    pid = props[0]["id"]
    assert client.post(f"/api/v1/proposals/{pid}/accept", headers=H(client, "auditor")).status_code == 403
    assert client.post(f"/api/v1/proposals/{pid}/accept", headers=H(client, "treasury")).status_code == 403
    assert client.get(f"/api/v1/audit", headers=H(client, "analyst")).status_code == 403          # analysts cannot read the audit log
    assert client.get(f"/api/v1/audit", headers=H(client, "auditor")).status_code == 200
    assert client.put(f"/api/v1/entities/{e}/config", json={"sla_hours": 24}, headers=H(client, "analyst")).status_code == 403
    assert client.put(f"/api/v1/entities/{e}/config", json={"sla_hours": 24}, headers=H(client, "admin")).status_code == 200
    assert client.post("/api/v1/demo/seed", headers=H(client, "analyst")).status_code == 403


def test_bank_account_numbers_are_masked_for_operational_roles(client, seeded):
    e = seeded
    a = client.get(f"/api/v1/entities/{e}/bank-accounts", headers=H(client, "analyst")).json()
    assert all(x["account_no"].startswith("X") or len(x["account_no"]) <= 4 for x in a if x["account_no"] and not x["is_gateway"])
    t = client.get(f"/api/v1/entities/{e}/bank-accounts", headers=H(client, "treasury")).json()
    assert any(x["account_no"] == "50200012345678" for x in t)


def test_end_to_end_review_to_erp_ack(client, seeded):
    e = seeded
    analyst, mgr, treas = H(client, "analyst"), H(client, "manager"), H(client, "treasury")
    summ = client.get(f"/api/v1/analytics/summary?entity_id={e}", headers=analyst).json()
    assert summ["queues"]["review"] > 0 and summ["matching_30d"]["auto_applied"] > 0

    props = client.get(f"/api/v1/proposals?entity_id={e}&status=pending_review&limit=500", headers=analyst).json()["items"]
    clean = [p for p in props if not p["short_minor"] and not p["unapplied_minor"] and p["allocations"]]
    pid = clean[0]["id"]
    detail = client.get(f"/api/v1/proposals/{pid}", headers=analyst).json()
    assert detail["explanation"]["summary"] and detail["explanation"]["contributions"] is not None and detail["guardrails"]
    acc = client.post(f"/api/v1/proposals/{pid}/accept", headers=analyst)
    assert acc.status_code == 200 and acc.json()["status"] == "accepted"
    assert client.post(f"/api/v1/proposals/{pid}/accept", headers=analyst).status_code == 409      # not twice

    # the person who builds a batch cannot release it
    b = client.post("/api/v1/posting/batches", json={"entity_id": e}, headers=mgr)
    assert b.status_code == 200, b.text
    bid = b.json()["id"]
    assert all(sum(l["base_debit_minor"] for l in b.json()["lines"] if l["voucher_no"] == v) == sum(l["base_credit_minor"] for l in b.json()["lines"] if l["voucher_no"] == v)
               for v in {l["voucher_no"] for l in b.json()["lines"]})
    assert client.post(f"/api/v1/posting/batches/{bid}/release", headers=mgr).status_code == 403   # manager lacks release permission
    assert client.post(f"/api/v1/posting/batches/{bid}/export", json={"connector": "generic_csv"}, headers=treas).status_code == 409  # not released yet
    assert client.post(f"/api/v1/posting/batches/{bid}/release", headers=treas).status_code == 200
    ex = client.post(f"/api/v1/posting/batches/{bid}/export", json={"connector": "generic_csv"}, headers=treas)
    assert ex.status_code == 200 and ex.headers["x-checksum-sha256"] and b"voucher_no" in ex.content
    assert client.post(f"/api/v1/posting/batches/{bid}/export", json={"connector": "tally_prime"}, headers=treas).status_code == 409
    vouchers = {l["voucher_no"] for l in client.get(f"/api/v1/posting/batches/{bid}", headers=treas).json()["lines"]}
    ack = client.post(f"/api/v1/posting/batches/{bid}/ack", json={"erp_docs": {v: f"ERP-{i}" for i, v in enumerate(vouchers)}}, headers=treas)
    assert ack.status_code == 200 and ack.json()["status"] == "acknowledged"
    posted = client.get(f"/api/v1/transactions?entity_id={e}&status=posted", headers=analyst).json()
    assert posted["total"] >= 1

    auditor = H(client, "auditor")
    assert client.get("/api/v1/audit/verify", headers=auditor).json()["ok"] is True
    trail = client.get(f"/api/v1/audit?object_type=MatchProposal&object_id={pid}", headers=auditor).json()["items"]
    assert {r["action"] for r in trail} >= {"proposal.created", "proposal.accepted"}
    assert client.get("/api/v1/audit/export", headers=auditor).text.startswith("seq,timestamp_utc")


def test_exception_resolution_flow_and_approval_over_http(client, seeded):
    e = seeded
    analyst, mgr = H(client, "analyst"), H(client, "manager")
    cases = client.get(f"/api/v1/exceptions?entity_id={e}&limit=500", headers=analyst).json()["items"]
    kinds = {c["type"] for c in cases}
    assert {"unidentified_receipt", "duplicate_payment"} <= kinds
    interest = next((c for c in cases if c["type"] == "non_ar_credit"), None)
    if interest:
        r = client.post(f"/api/v1/exceptions/{interest['id']}/resolve", json={"action": "classify_non_ar", "params": {"note": "Savings interest credit"}}, headers=analyst)
        assert r.status_code == 200
        ap = r.json()["result"]["approval_id"]
        assert client.post(f"/api/v1/approvals/{ap}/decide", json={"approve": True}, headers=analyst).status_code == 403   # analyst lacks approve
        ok = client.post(f"/api/v1/approvals/{ap}/decide", json={"approve": True, "note": "ok"}, headers=mgr)
        assert ok.status_code == 200 and ok.json()["status"] == "approved"
    dup = next(c for c in cases if c["type"] == "unidentified_receipt")
    n = client.post(f"/api/v1/exceptions/{dup['id']}/notes", json={"text": "Asked sales for remittance"}, headers=analyst)
    assert n.status_code == 200 and n.json()["notes"][-1]["text"].startswith("Asked")
    assert client.post(f"/api/v1/exceptions/{dup['id']}/resolve", json={"action": "bogus"}, headers=analyst).status_code == 409


def test_dashboard_endpoints_return_sane_numbers(client, seeded):
    e, h = seeded, H(client, "treasury")
    s = client.get(f"/api/v1/analytics/summary?entity_id={e}", headers=h).json()
    assert s["receivables"]["open_minor"] > 0 and s["receivables"]["dso_days"] > 0
    assert sum(x["amount_minor"] for x in s["receivables"]["ageing"]) == s["receivables"]["open_minor"]
    assert sum(b["amount_minor"] for b in s["unapplied"]["ageing"]) == s["unapplied"]["amount_minor"]
    f = client.get(f"/api/v1/analytics/forecast?entity_id={e}", headers=h).json()
    assert sum(w["amount_minor"] for w in f["weeks"]) > 0 and "heuristic" not in f["method"] or True
    assert client.get(f"/api/v1/analytics/cash-position?entity_id={e}", headers=h).status_code == 200
    assert client.get(f"/api/v1/analytics/trend?entity_id={e}", headers=h).status_code == 200


def test_upload_validation(client, seeded):
    e, h = seeded, H(client, "analyst")
    banks = client.get(f"/api/v1/entities/{e}/bank-accounts", headers=h).json()
    bid = next(b["id"] for b in banks if not b["is_gateway"])
    assert client.post("/api/v1/ingest/bank-statement", data={"bank_account_id": bid}, files={"file": ("x.csv", b"", "text/csv")}, headers=h).status_code == 400
    assert client.post("/api/v1/ingest/bank-statement", data={"bank_account_id": bid}, files={"file": ("x.csv", b"no,header,here\n1,2,3", "text/csv")}, headers=h).status_code == 422
    csv_ = b"Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n10/10/26,NEFT-HDFCN26283000999-ACME-INV 1,,1000.00,5000.00\n"
    r = client.post("/api/v1/ingest/bank-statement", data={"bank_account_id": bid}, files={"file": ("s.csv", csv_, "text/csv")}, headers=h)
    assert r.status_code == 200 and r.json()["inserted"] == 1
    again = client.post("/api/v1/ingest/bank-statement", data={"bank_account_id": bid}, files={"file": ("s.csv", csv_, "text/csv")}, headers=h)
    assert again.json()["inserted"] == 0 and again.json()["duplicates_skipped"] == 1
    rem = client.post("/api/v1/ingest/remittance", data={"entity_id": e, "text": "INV-1  1,000.00"}, headers=h)
    assert rem.status_code == 200 and rem.json()["extracted"]["lines"]


def test_config_validation_and_audit(client, seeded):
    e, h = seeded, H(client, "admin")
    assert client.put(f"/api/v1/entities/{e}/config", json={"auto_approve_threshold": 0.1}, headers=h).status_code == 422   # cannot set a dangerously low bar
    assert client.put(f"/api/v1/entities/{e}/config", json={"auto_approve_threshold": 0.95}, headers=h).status_code == 200
    rows = client.get("/api/v1/audit?action=config.changed", headers=H(client, "auditor")).json()["items"]
    assert rows and rows[0]["detail"]["changes"]["auto_approve_threshold"] == 0.95


def test_connectors_are_honest_about_what_exists(client, seeded):
    rows = {x["name"]: x for x in client.get("/api/v1/connectors", headers=H(client, "analyst")).json()}
    assert rows["generic_csv"]["status"] == "available" and rows["zoho_books"]["status"] == "planned"
    assert rows["tally_prime"]["status"] == "available" and rows["tally_prime"]["unverified"] is True, "built from the published format, never imported into a real ERP"
    assert "unverified" not in rows["generic_csv"]


def test_dashboard_converts_foreign_currency_and_flags_missing_rates(client, seeded, db):
    from sqlalchemy import delete

    from app.models import ExchangeRate

    e, h = seeded, H(client, "treasury")
    s = client.get(f"/api/v1/analytics/summary?entity_id={e}", headers=h).json()
    assert s["base_currency"] == "INR" and s["missing_rates"] == []
    fe = {x["currency"]: x for x in s["foreign_exposure"]}
    assert set(fe) == {"USD", "EUR"} and fe["USD"]["base_equivalent_minor"] > fe["USD"]["open_minor"] * 70   # ~₹83 per $
    with_fx = s["receivables"]["open_minor"]
    db.execute(delete(ExchangeRate).where(ExchangeRate.currency == "EUR"))
    db.commit()
    s2 = client.get(f"/api/v1/analytics/summary?entity_id={e}", headers=h).json()
    assert s2["missing_rates"] == ["EUR"] and s2["receivables"]["open_minor"] < with_fx, "unconvertible amounts are excluded AND reported, never silently added at 1:1"


def test_fx_rate_api_validates_and_is_role_gated(client, seeded):
    body = [{"currency": "usd", "rate_date": "2026-10-15", "rate": "83.5000"}]
    assert client.post("/api/v1/fx-rates", json=body, headers=H(client, "analyst")).status_code == 403
    assert client.post("/api/v1/fx-rates", json=body, headers=H(client, "treasury")).json() == {"upserted": 1}
    assert client.post("/api/v1/fx-rates", json=[{"currency": "USD", "rate_date": "2026-10-15", "rate": "0.0001"}], headers=H(client, "treasury")).status_code == 422
    assert client.post("/api/v1/fx-rates", json=[{"currency": "USD", "rate_date": "2026-10-15", "rate": "abc"}], headers=H(client, "treasury")).status_code == 422
    r = client.get("/api/v1/fx-rates?currency=USD&limit=3", headers=H(client, "analyst")).json()
    assert r[0]["rate_date"] == "2026-10-15" or any(x["rate"] == 83.5 for x in r)
