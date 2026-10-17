"""Drive a freshly seeded demo tenant (API running) into a state that shows the newer features, for the static demo snapshot.

Everything goes through the real API or the real services, with synthetic data only. Usage:
    python -m app.seed.demo_scenario http://localhost:8000
"""
from __future__ import annotations

import csv
import io
import json
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx


def main(base: str) -> None:
    rnd = random.Random(7)

    def cl(email: str) -> httpx.Client:
        c = httpx.Client(base_url=f"{base}/api/v1", timeout=120)
        c.headers["Authorization"] = "Bearer " + c.post("/auth/login", json={"email": email, "password": "demo1234"}).json()["token"]
        return c

    an, mg, tr, adm, cfo = (cl(f"{r}@meridian.example") for r in ("analyst", "manager", "treasury", "admin", "cfo"))
    ok = lambda r: (r.raise_for_status(), r)[1]

    ok(adm.put("/entities/1/config", json={"trust_graduation": {"enabled": True, "graduate_after": 3}, "e_invoice_required": True, "gstr1_frequency": "monthly",
                                           "tds_194q_threshold_minor": 50_00_000_00}))
    ok(adm.put("/entities/1/notifications", json={"email": {"host": "smtp.meridian.example", "from": "cash-app@meridian.example", "to": ["ar-team@meridian.example"],
                                                              "username_env": "SMTP_USER", "password_env": "SMTP_PASS"},
                                                    "slack": {"webhook_env": "SLACK_WEBHOOK_URL", "events": ["approval.requested", "auto.reversed", "model.alert"]}}))
    ok(adm.put("/entities/1/integrations", json={"erp_options": {"tally_prime": {"company": "Meridian Industrial Components Pvt Ltd", "party_by": "name"}}}))

    # --- reviewers work the queue: confirmations (customers graduate), a few rejections ---------------------------------------
    ps = an.get("/proposals?entity_id=1&status=pending_review&limit=500").json()["items"]
    for p in ps[:42]:
        an.post(f"/proposals/{p['id']}/accept", json={})
    for p in ps[42:48]:
        an.post(f"/proposals/{p['id']}/reject", json={"reason": "Paid by a different group company; customer to confirm"})

    # --- overpayments held on account, then invoices raised later -> suggested applications -----------------------------------------
    ex = an.get("/exceptions?entity_id=1&type=overpayment&limit=20").json()["items"]
    held = []
    for x in ex[:3]:
        an.post(f"/exceptions/{x['id']}/resolve", json={"action": "on_account", "params": {}})
        held.append(x["customer"]["id"])
    from ..db import SessionLocal
    from ..models import Invoice, MatchProposal, Notification, PostingBatch

    db = SessionLocal()
    for n, cid in enumerate(held[:3]):
        tx = (400000 + 150000 * n) * 100
        gst = tx * 18 // 100
        db.add(Invoice(entity_id=1, customer_id=cid, number=f"MIC/26-27/9{n}01", doc_type="invoice", invoice_date=date.today() - timedelta(days=2), due_date=date.today() + timedelta(days=28),
                       taxable_minor=tx, gst_minor=gst, total_minor=tx + gst, open_minor=tx + gst, currency="INR", booking_rate_micro=1_000_000, irn=None))
    # make the model-health windows meaningful: some proposals older than the 14-day window
    for p in db.query(MatchProposal).all():
        if rnd.random() < 0.55:
            p.created_at = datetime.utcnow() - timedelta(days=rnd.randint(16, 70))
    db.commit()
    mg.post("/advances/propose?entity_id=1")
    a = mg.get("/advances?entity_id=1").json()
    if a["proposals"]:
        ok(an.post(f"/advances/{a['proposals'][0]['id']}/accept"))     # one applied already, the rest wait

    # --- approvals: an intercompany receipt (finance head) and a write-off (manager) --------------------------------------------
    for x in mg.get("/exceptions?entity_id=1&limit=60").json()["items"]:
        if x["type"] == "intercompany":
            mg.post(f"/exceptions/{x['id']}/resolve", json={"action": "classify_interco", "params": {"note": "Funding from group entity per treasury sweep instruction"}})
        if x["type"] == "short_pay" and x["amount_minor"] < 25_000_00:
            an.post(f"/exceptions/{x['id']}/resolve", json={"action": "write_off", "params": {"note": "Customer quality claim agreed by sales", "amount_minor": x["amount_minor"]}})
            break

    # --- posting: one acknowledged batch, one exported long ago with no ERP confirmation ----------------------------------------------
    b1 = ok(mg.post("/posting/batches", json={"entity_id": 1})).json()
    ok(tr.post(f"/posting/batches/{b1['id']}/release", json={}))
    ok(tr.post(f"/posting/batches/{b1['id']}/export", json={"connector": "generic_csv"}))
    docs = {l["voucher_no"]: f"JV-26-{i:05d}" for i, l in enumerate(tr.get(f"/posting/batches/{b1['id']}").json()["lines"], 1)}
    ok(tr.post(f"/posting/batches/{b1['id']}/ack", json={"erp_docs": docs}))
    # a second batch of newer matches, exported through the Tally renderer and never confirmed
    mg.post("/matching/run", json={"entity_id": 1})
    for p in an.get("/proposals?entity_id=1&status=pending_review&limit=500").json()["items"][:8]:
        an.post(f"/proposals/{p['id']}/accept", json={})
    r = mg.post("/posting/batches", json={"entity_id": 1})
    if r.status_code == 200:
        b2 = r.json()
        ok(tr.post(f"/posting/batches/{b2['id']}/release", json={}))
        ok(tr.post(f"/posting/batches/{b2['id']}/export", json={"connector": "tally_prime"}))
        row = db.get(PostingBatch, b2["id"])
        row.exported_at = datetime.utcnow() - timedelta(hours=31)
        db.commit()

    # --- tax: Form 26AS (synthetic, includes gaps and provisional lines), GSTR-1 and the e-invoice register ---------------------------
    tds = an.get("/tds?entity_id=1&limit=500").json()["items"]
    agg, tans = defaultdict(int), {}
    for t in tds:
        agg[(t["customer"]["name"], t["section"] or "194C", t["txn_date"])] += t["amount_minor"]
    rows = [["Deductor Name", "Deductor TAN", "Section", "Date of Payment/Credit", "Status of Booking", "Amount Paid / Credited", "Tax Deducted", "TDS Deposited"]]
    for i, ((name, sec, d), amt) in enumerate(sorted(agg.items())):
        tan = tans.setdefault(name, f"MUMA{10000 + len(tans):05d}B")
        if i % 4 == 3:
            continue
        v = amt - 5_000_00 if i % 5 == 0 and amt > 6_000_00 else amt
        rows.append([name, tan, sec, d, "P" if i % 6 == 0 else "F", f"{v * 50 / 100:.2f}", f"{v / 100:.2f}", f"{v / 100:.2f}"])
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    ok(an.post("/tax/26as/import", data={"entity_id": 1, "source": "26AS"}, files={"file": ("26as.csv", buf.getvalue(), "text/csv")}))
    an.post("/tax/26as/reconciliation/apply?entity_id=1&fy=2026-27")

    invs = db.query(Invoice).filter(Invoice.entity_id == 1, Invoice.currency == "INR").order_by(Invoice.id).all()
    from ..models import Customer

    custs = {c.id: c for c in db.query(Customer).all()}
    by_period: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for k, inv in enumerate(invs):
        c = custs[inv.customer_id]
        if not c.gstin or k % 7 == 3:        # a few invoices never made it into GSTR-1
            continue
        fp = f"{inv.invoice_date.month:02d}{inv.invoice_date.year}"
        gstin = c.gstin if k % 29 else "29AAAAA0000A1Z5"       # one with the wrong GSTIN
        total = inv.total_minor / 100 if k % 41 else inv.total_minor / 100 + 250        # one value mismatch
        if inv.doc_type == "credit_note":
            continue
        by_period[fp][gstin].append({"inum": inv.number, "idt": inv.invoice_date.strftime("%d-%m-%Y"), "val": total, "pos": "27", "rchrg": "N", "inv_typ": "R",
                                     "itms": [{"num": 1, "itm_det": {"txval": inv.taxable_minor / 100, "rt": 18}}]})
    for fp, parties in by_period.items():
        mth, yr = int(fp[:2]), int(fp[2:])
        if date(yr, mth, 1) > date.today() - timedelta(days=45):
            continue                         # the latest returns are not filed/imported yet
        body = {"gstin": "27AABCM4421F1ZU", "fp": fp, "b2b": [{"ctin": g, "inv": v} for g, v in parties.items()], "cdnr": []}
        ok(an.post("/tax/gst/import", data={"entity_id": 1, "source": "GSTR1"}, files={"file": (f"gstr1_{fp}.json", json.dumps(body), "application/json")}))
    irp = io.StringIO()
    w = csv.writer(irp)
    w.writerow(["IRN", "Ack No", "Ack Date", "Document number", "Document date", "Document type", "Buyer GSTIN", "Total value", "Status"])
    for k, inv in enumerate([i for i in invs if i.irn and i.doc_type != "credit_note"]):
        if k % 23 == 5:
            continue                          # carries an IRN in the ERP but is missing from the register
        irn = inv.irn if k % 31 else "f" * 64        # one IRN differs
        w.writerow([irn, k + 1, inv.invoice_date, inv.number, inv.invoice_date, "INV", custs[inv.customer_id].gstin or "", f"{inv.total_minor / 100:.2f}", "CNL" if k % 37 == 9 else "ACT"])
    ok(an.post("/tax/gst/import", data={"entity_id": 1, "source": "IRP"}, files={"file": ("irp.csv", irp.getvalue(), "text/csv")}))

    # --- model lifecycle: train a candidate and put it in shadow ---------------------------------------------------------------------
    r = cfo.post("/models/retrain")
    if r.status_code == 200:
        cfo.post(f"/models/{r.json()['id']}/shadow", json={"enabled": True})

    # --- period close ---------------------------------------------------------------------------------------------------------------
    ok(cfo.post("/periods/lock", json={"entity_id": 1, "locked_through": "2026-06-30", "note": "Q1 FY26-27 closed"}))

    # --- outbox: some delivered, one failed ---------------------------------------------------------------------------------------------
    rowsn = db.query(Notification).order_by(Notification.id).all()
    for i, n in enumerate(rowsn):
        if i % 3 == 0:
            n.status, n.sent_at = "sent", datetime.utcnow() - timedelta(minutes=5 * i)
        elif i % 7 == 1:
            n.status, n.attempts, n.error = "failed", 5, "ConnectionRefusedError: smtp.meridian.example:587"
    db.commit()
    print({"customers_auto": sum(1 for _ in an.get("/customers?entity_id=1&limit=500").json()["items"] if _.get("trust_mode") == "auto"),
           "advances": len(mg.get("/advances?entity_id=1").json()["proposals"]), "notifications": len(rowsn)})


if __name__ == "__main__":
    main(sys.argv[1])
