"""Tally, SAP, Business Central and NetSuite renderers, built from published formats."""
import csv
import io
import json
from decimal import Decimal

import pytest
from defusedxml import ElementTree as ET
from sqlalchemy import select

from app.matching import actions
from app.models import MatchProposal, ProposalStatus
from app.posting import erp_native, service as posting
from app.posting.connectors import REGISTRY
from tests.conftest import login
from tests.test_period_evidence import applied_receipt


def tds_batch(w):
    """One receipt with TDS and one overpayment: between them every journal line type appears."""
    c = w.customer("C1", "Acme Industries Pvt Ltd", tds_bp=200)
    inv = w.invoice(c, "INV/26-27/8001", 100_000_00)
    inv2 = w.invoice(c, "INV/26-27/8002", 50_000_00)
    t1 = w.credit(inv.total_minor - 2_000_00, "NEFT-HDFCN26274008001-ACME INDUSTRIES PVT LTD-INV/26-27/8001")
    t2 = w.credit(inv2.total_minor + 1_000_00, "NEFT-HDFCN26274008003-ACME INDUSTRIES PVT LTD-INV/26-27/8002")
    w.run(t1, t2)
    for p in w.db.execute(select(MatchProposal)).scalars():
        if p.status == ProposalStatus.pending_review:
            actions.apply_proposal(w.db, None, p, "a", w.users["ar_analyst"].id)
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    posting._attach_refs(w.db, b)
    assert {l.line_type for l in b.lines} >= {"bank_receipt", "tds_receivable", "ar_clearance", "unapplied_cash"}
    return b


def render(w, name, **opts):
    b = tds_batch(w)
    c = REGISTRY[name].with_options(opts)
    return b, c.render(b, w.e)


def test_tally_xml_is_well_formed_balanced_and_uses_tallys_sign_convention_and_bill_references(w):
    b, r = render(w, "tally_prime", company="Test Mfg Pvt Ltd")
    root = ET.fromstring(r.content)
    assert root.find(".//SVCURRENTCOMPANY").text == "Test Mfg Pvt Ltd"
    vs = root.findall(".//VOUCHER")
    assert len(vs) == 2
    v = next(x for x in vs if any(e.find("BILLALLOCATIONS.LIST/NAME") is not None and e.find("BILLALLOCATIONS.LIST/NAME").text == "INV/26-27/8001" for e in x.findall("ALLLEDGERENTRIES.LIST")))
    assert v.get("VCHTYPE") == "Receipt" and v.find("DATE").text == b.lines[0].posting_date.strftime("%Y%m%d") and v.find("PARTYLEDGERNAME").text == "Acme Industries Pvt Ltd"
    total = Decimal(0)
    for e in v.findall("ALLLEDGERENTRIES.LIST"):
        amt = Decimal(e.find("AMOUNT").text)
        assert (e.find("ISDEEMEDPOSITIVE").text == "Yes") == (amt < 0), "a debit is a negative amount with ISDEEMEDPOSITIVE=Yes"
        total += amt
    assert total == 0, "the voucher balances"
    party = [e for e in v.findall("ALLLEDGERENTRIES.LIST") if e.find("LEDGERNAME").text == "Acme Industries Pvt Ltd"]
    assert party and party[0].find("BILLALLOCATIONS.LIST/NAME").text == "INV/26-27/8001" and party[0].find("BILLALLOCATIONS.LIST/BILLTYPE").text == "Agst Ref"
    assert any(e.find("BILLALLOCATIONS.LIST/BILLTYPE") is not None and e.find("BILLALLOCATIONS.LIST/BILLTYPE").text == "On Account" for x in vs for e in x.findall("ALLLEDGERENTRIES.LIST"))


def test_tally_escapes_special_characters_in_names(w):
    c = w.customer("C1", "Tom & Jerry <Traders> Pvt Ltd")
    inv = w.invoice(c, "INV-1", 10_000_00)
    t = w.credit(inv.total_minor, "NEFT-HDFCN26274008002-TOM & JERRY TRADERS PVT LTD-INV-1")
    w.run(t)
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    posting._attach_refs(w.db, b)
    out = REGISTRY["tally_prime"].with_options({"company": "A&B"}).render(b, w.e).content.decode()
    assert "Tom &amp; Jerry &lt;Traders&gt; Pvt Ltd" in out and ET.fromstring(out.encode()) is not None


def test_sap_payload_has_bapi_structures_signed_amounts_padded_codes_and_the_honest_note(w):
    b, r = render(w, "sap_s4", company_code="1000", ledger_map={"1100-HDFC": "110000"})
    doc = json.loads(r.content)
    d = next(x for x in doc["documents"] if any(a["ALLOC_NMBR"] == "INV/26-27/8001" for a in x["ACCOUNTRECEIVABLE"]))
    assert d["DOCUMENTHEADER"]["COMP_CODE"] == "1000" and d["DOCUMENTHEADER"]["DOC_TYPE"] == "DZ" and len(d["DOCUMENTHEADER"]["REF_DOC_NO"]) <= 16
    amts = [Decimal(a["AMT_DOCCUR"]) for a in d["CURRENCYAMOUNT"]]
    assert sum(amts) == 0 and any(a > 0 for a in amts) and any(a < 0 for a in amts), "BAPI sign: debit positive, credit negative"
    assert d["ACCOUNTRECEIVABLE"][0]["ALLOC_NMBR"] == "INV/26-27/8001"
    assert any(g["GL_ACCOUNT"] == "0000110000" for g in d["ACCOUNTGL"]), "numeric ledgers are padded to 10 digits"
    assert "does NOT clear open items" in doc["note"]
    assert {x["ITEMNO_ACC"] for x in d["CURRENCYAMOUNT"]} == {x["ITEMNO_ACC"] for x in d["ACCOUNTGL"] + d["ACCOUNTRECEIVABLE"]}


def test_business_central_lines_use_account_types_and_apply_to_the_invoice(w):
    b, r = render(w, "d365_bc", journal="CASHREC")
    rows = json.loads(r.content)["journalLines"]
    assert {x["accountType"] for x in rows} == {"Bank Account", "G/L Account", "Customer"} and {x["journalDisplayName"] for x in rows} == {"CASHREC"}
    assert round(sum(x["amount"] for x in rows), 2) == 0
    cust = [x for x in rows if x["accountType"] == "Customer" and x.get("appliesToDocumentNumber")]
    assert cust[0]["appliesToDocumentNumber"] == "INV/26-27/8001" and cust[0]["appliesToDocumentType"] == "Invoice" and len({x["lineNumber"] for x in rows}) == len(rows)


def test_netsuite_csv_balances_per_voucher(w):
    b, r = render(w, "netsuite", subsidiary="Test Mfg")
    rows = list(csv.DictReader(io.StringIO(r.content.decode())))
    assert rows[0].keys() >= {"External ID", "Date", "Subsidiary", "Account", "Debit", "Credit"} and {x["Subsidiary"] for x in rows} == {"Test Mfg"}
    assert sum(Decimal(x["Debit"] or 0) for x in rows) == sum(Decimal(x["Credit"] or 0) for x in rows) > 0


def test_unbalanced_voucher_is_never_rendered(w):
    b = tds_batch(w)
    b.lines[0].base_debit_minor += 1
    for name, opts in (("tally_prime", {"company": "x"}), ("sap_s4", {"company_code": "1"}), ("d365_bc", {"journal": "j"}), ("netsuite", {"subsidiary": "s"})):
        with pytest.raises(erp_native.RenderError, match="does not balance"):
            REGISTRY[name].with_options(opts).render(b, w.e)


def test_option_validation_and_listing(client, w):
    assert erp_native.validate_options({"sap_s4": {}, "tally_prime": {"company": "x", "party_by": "id"}, "nope": {}}) == [
        "erp_options.sap_s4: company_code is required", "erp_options.tally_prime: party_by must be name or code", "erp_options: unknown connector 'nope'"]
    fin = login(client, "admin@t.example", "pw-admin")
    url = f"/api/v1/entities/{w.e.id}/integrations"
    assert client.put(url, json={"erp_options": {"sap_s4": {}}}, headers=fin).status_code == 422
    assert client.put(url, json={"erp_options": {"sap_s4": {"company_code": "1000"}}}, headers=fin).status_code == 200
    got = {x["name"]: x for x in client.get(url, headers=fin).json()["connectors"]}
    assert got["sap_s4"]["status"] == "available" and got["tally_prime"]["status"] == "needs options" and got["zoho_books"]["status"] == "planned"


def test_export_refuses_until_options_are_set_then_stores_and_checksums_the_file(client, w):
    t, p = applied_receipt(w, __import__("datetime").date(2026, 10, 12), number="INV/26-27/8100", ref="HDFCN26274008100")
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    posting.release_batch(w.db, b, w.users["treasury"])
    with pytest.raises(posting.PostingError, match="needs settings first.*company is required"):
        posting.export_batch(w.db, b, "tally_prime", w.users["treasury"])
    w.e.config = {**w.e.config, "integrations": {"erp_options": {"tally_prime": {"company": "Test Mfg"}}}}
    fn, content, ctype = posting.export_batch(w.db, b, "tally_prime", w.users["treasury"])
    assert fn.endswith("_tally.xml") and ET.fromstring(content) is not None and b.checksum and b.status.value == "exported"


# ------------------------------------------------------------------------------------------------ ERP confirmation
def exported_batch(w, n=1):
    from datetime import date

    for i in range(n):
        applied_receipt(w, date(2026, 10, 12), number=f"INV/26-27/82{i:02d}", ref=f"HDFCN262740082{i:02d}")
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    posting.release_batch(w.db, b, w.users["treasury"])
    posting.export_batch(w.db, b, "generic_csv", w.users["treasury"])
    return b


def test_confirmation_file_acknowledges_when_every_voucher_is_confirmed(client, w):
    b = exported_batch(w, 2)
    vouchers = sorted({l.voucher_no for l in b.lines})
    csv_ok = "voucher_no,erp_doc_no,status\n" + "\n".join(f"{v},JV-{i}00,OK" for i, v in enumerate(vouchers, 1))
    h = login(client, "treasury@t.example", "pw-treasury")
    r = client.post(f"/api/v1/posting/batches/{b.id}/ack-file", files={"file": ("erp.csv", csv_ok, "text/csv")}, headers=h)
    assert r.status_code == 200 and r.json() == {"acknowledged": True, "confirmed": 2, "errors": [], "missing": [], "unknown_in_file": [], "vouchers": 2}
    assert b.status.value == "acknowledged" and {l.erp_doc_no for l in b.lines} == {"JV-100", "JV-200"}


def test_partial_or_rejected_confirmations_change_nothing_unless_asked_to_fail_the_batch(client, w):
    b = exported_batch(w, 2)
    v1, v2 = sorted({l.voucher_no for l in b.lines})
    h = login(client, "treasury@t.example", "pw-treasury")
    partial = f"voucher_no,erp_doc_no\n{v1},JV-1\nGHOST-9,JV-9\n"
    r = client.post(f"/api/v1/posting/batches/{b.id}/ack-file", files={"file": ("e.csv", partial, "text/csv")}, headers=h).json()
    assert r["acknowledged"] is False and r["missing"] == [v2] and r["unknown_in_file"] == ["GHOST-9"] and b.status.value == "exported"
    rejected = f"voucher_no,erp_doc_no,status,error\n{v1},JV-1,OK,\n{v2},,ERROR,Customer blocked in ERP\n"
    r2 = client.post(f"/api/v1/posting/batches/{b.id}/ack-file", files={"file": ("e.csv", rejected, "text/csv")}, headers=h).json()
    assert r2["errors"] == [{"voucher_no": v2, "error": "Customer blocked in ERP"}] and b.status.value == "exported"
    r3 = client.post(f"/api/v1/posting/batches/{b.id}/ack-file", files={"file": ("e.csv", rejected, "text/csv")}, data={"fail_on_errors": "true"}, headers=h).json()
    assert r3["batch_failed"] is True and b.status.value == "failed" and "Customer blocked in ERP" in b.error


def test_confirmation_file_validation_and_roles(client, w):
    b = exported_batch(w)
    h, an = login(client, "treasury@t.example", "pw-treasury"), login(client, "ar_analyst@t.example", "pw-ar_analyst")
    assert client.post(f"/api/v1/posting/batches/{b.id}/ack-file", files={"file": ("e.csv", "a,b\n1,2\n", "text/csv")}, headers=h).status_code == 409
    assert client.post(f"/api/v1/posting/batches/{b.id}/ack-file", files={"file": ("e.csv", "voucher_no,erp_doc_no\n", "text/csv")}, headers=h).status_code == 409
    assert client.post(f"/api/v1/posting/batches/{b.id}/ack-file", files={"file": ("e.csv", "voucher_no,erp_doc_no\nX,Y\n", "text/csv")}, headers=an).status_code == 403


def test_unconfirmed_list_uses_the_configured_window(client, w):
    from datetime import timedelta

    from app.models import utcnow

    b = exported_batch(w)
    h = login(client, "treasury@t.example", "pw-treasury")
    assert client.get(f"/api/v1/posting/unconfirmed?entity_id={w.e.id}", headers=h).json()["items"] == []
    b.exported_at = utcnow() - timedelta(hours=30)
    w.db.flush()
    got = client.get(f"/api/v1/posting/unconfirmed?entity_id={w.e.id}", headers=h).json()
    assert got["hours"] == 24 and [x["id"] for x in got["items"]] == [b.id]
    w.e.config = {**w.e.config, "erp_confirm_hours": 48}
    assert client.get(f"/api/v1/posting/unconfirmed?entity_id={w.e.id}", headers=h).json()["items"] == []
