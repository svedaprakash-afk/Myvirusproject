"""Dump a frozen JSON snapshot of the synthetic demo tenant for the static, backend-less demo build.

Usage (against a freshly seeded database, API running):  python -m app.seed.export_snapshot http://localhost:8000 out.json
"""
from __future__ import annotations

import json
import sys

import httpx

BASE_USER = ("manager@meridian.example", "demo1234")


def main(base: str, out: str) -> None:
    c = httpx.Client(base_url=f"{base}/api/v1", timeout=60)
    tok = c.post("/auth/login", json={"email": BASE_USER[0], "password": BASE_USER[1]}).json()["token"]
    c.headers["Authorization"] = f"Bearer {tok}"
    admin = httpx.Client(base_url=f"{base}/api/v1", timeout=60)
    admin.headers["Authorization"] = "Bearer " + admin.post("/auth/login", json={"email": "admin@meridian.example", "password": "demo1234"}).json()["token"]

    def get(path: str, cli=c):
        r = cli.get(path)
        r.raise_for_status()
        return r.json()

    snap: dict = {"entities": get("/entities"), "connectors": get("/connectors"), "bank_templates": get("/bank-templates"), "models": get("/models", admin),
                  "fx_rates": get("/fx-rates?limit=500"), "fx_feed": get("/fx-rates/feed"), "storage": get("/storage", admin), "audit_verify": get("/audit/verify", admin), "per_entity": {}}
    for e in snap["entities"]:
        eid = e["id"]
        pe: dict = {}
        pe["summary"] = get(f"/analytics/summary?entity_id={eid}")
        pe["trend"] = get(f"/analytics/trend?entity_id={eid}&days=30")
        pe["cash"] = get(f"/analytics/cash-position?entity_id={eid}")
        pe["forecast"] = get(f"/analytics/forecast?entity_id={eid}&weeks=8")
        pe["config"] = get(f"/entities/{eid}/config", admin)
        pe["integrations"] = get(f"/entities/{eid}/integrations", admin)
        pe["remittances"] = get(f"/remittances?entity_id={eid}")
        pe["banks"] = get(f"/entities/{eid}/bank-accounts")
        pe["bank_setup"] = {b["id"]: get(f"/bank-accounts/{b['id']}/setup", admin) for b in pe["banks"]}
        pe["proposals"] = get(f"/proposals?entity_id={eid}&status=pending_review,accepted,auto_approved,rejected,posted&limit=500")["items"]
        pe["transactions"] = get(f"/transactions?entity_id={eid}&direction=&limit=500")["items"]
        pe["txn_detail"] = {t["id"]: get(f"/transactions/{t['id']}") for t in pe["transactions"]}
        pe["exceptions"] = get(f"/exceptions?entity_id={eid}&status=open,in_progress,pending_approval,resolved,written_off&limit=500")["items"]
        pe["exception_detail"] = {x["id"]: get(f"/exceptions/{x['id']}") for x in pe["exceptions"]}
        pe["approvals"] = get(f"/approvals?entity_id={eid}&status=pending,approved,rejected&limit=500")
        pe["tds"] = get(f"/tds?entity_id={eid}&limit=500")["items"]
        pe["batches"] = get(f"/posting/batches?entity_id={eid}")
        pe["batch_detail"] = {b["id"]: get(f"/posting/batches/{b['id']}") for b in pe["batches"]["items"]}
        pe["customers"] = get(f"/customers?entity_id={eid}&limit=500")["items"]
        pe["customer_detail"] = {x["id"]: get(f"/customers/{x['id']}") for x in pe["customers"]}
        pe["invoices"] = get(f"/invoices?entity_id={eid}&limit=500")["items"]
        pe["audit"] = get(f"/audit?entity_id={eid}&limit=200", admin)["items"]
        snap["per_entity"][eid] = pe
    with open(out, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    print({k: (len(v) if hasattr(v, "__len__") else v) for k, v in snap["per_entity"][snap["entities"][0]["id"]].items()})


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
