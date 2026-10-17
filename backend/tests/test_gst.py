"""GSTR-1 reporting and e-invoice register cross-checks."""
import json
from datetime import date

import pytest

from app.ingestion import gst
from tests.conftest import TODAY, login


def gstr1(fp, b2b=(), cdnr=()):
    return json.dumps({"gstin": "27AABCT1234F1Z5", "fp": fp, "b2b": list(b2b), "cdnr": list(cdnr)}).encode()


def inv_row(num, dt, val, tx, gstin="X"):
    return {"inum": num, "idt": dt, "val": val, "pos": "27", "rchrg": "N", "inv_typ": "R",
            "itms": [{"num": 1, "itm_det": {"txval": tx, "rt": 18, "iamt": 0, "camt": 0, "samt": 0, "csamt": 0}}]}


def setup(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    return c


def test_gstr1_json_parses_invoices_and_credit_notes():
    docs, fp = gst.parse_gstr1(gstr1("092026", b2b=[{"ctin": "27AABCA1234F1Z5", "inv": [inv_row("INV/26-27/0001", "01-09-2026", 118000, 100000)]}],
                                     cdnr=[{"ctin": "27AABCA1234F1Z5", "nt": [{"ntty": "C", "nt_num": "CN/26-27/0001", "nt_dt": "20-09-2026", "val": 11800,
                                                                               "itms": [{"itm_det": {"txval": 10000}}]}]}]))
    assert fp == "092026"
    assert [(d["doc_type"], d["doc_no"], d["doc_date"], d["total_minor"], d["taxable_minor"]) for d in docs] == [
        ("INV", "INV/26-27/0001", date(2026, 9, 1), 118_000_00, 100_000_00), ("CRN", "CN/26-27/0001", date(2026, 9, 20), 11_800_00, 10_000_00)]


def test_bad_gstr1_files_are_refused_with_a_reason():
    with pytest.raises(gst.GstError, match="valid JSON"):
        gst.parse_gstr1(b"nope")
    with pytest.raises(gst.GstError, match="GSTR-1 JSON"):
        gst.parse_gstr1(b'{"a": 1}')
    with pytest.raises(gst.GstError, match="filing period"):
        gst.parse_gstr1(b'{"b2b": [], "fp": "13-2026"}')
    with pytest.raises(gst.GstError, match="No invoices"):
        gst.parse_gstr1(b'{"b2b": [], "fp": "092026"}')


IRP_CSV = (b"IRN,Ack No,Ack Date,Document number,Document date,Document type,Buyer GSTIN,Total value,Status\n"
           b"aaa111,1,2026-08-02,INV/26-27/0100,2026-08-01,INV,27AABCA1234F1Z5,\"1,18,000.00\",ACT\n"
           b"bbb222,2,2026-08-06,INV/26-27/0101,2026-08-05,INV,27AABCA1234F1Z5,\"59,000.00\",CNL\n")


def test_irp_csv_register_parses_status_and_values():
    from app.ingestion.remittance import extract_upload

    docs = gst.parse_irp(extract_upload("irp.csv", IRP_CSV).text)
    assert [(d["doc_no"], d["irn"], d["status"], d["total_minor"]) for d in docs] == [("INV/26-27/0100", "aaa111", "ACT", 118_000_00), ("INV/26-27/0101", "bbb222", "CNL", 59_000_00)]
    with pytest.raises(gst.GstError, match="No e-invoices"):
        gst.parse_irp("a\tb\n1\t2")


def test_invoice_missing_from_every_imported_gstr1_is_flagged_once_the_return_was_due(w):
    c = setup(w)
    c.gstin = "27AABCA1234F1Z5"
    reported = w.invoice(c, "INV/26-27/0001", 100_000_00, days_ago=45)       # 2 Sep 2026: GSTR-1 was due 11 Oct
    missing = w.invoice(c, "INV/26-27/0002", 100_000_00, days_ago=44)
    recent = w.invoice(c, "INV/26-27/0003", 100_000_00, days_ago=2)          # 14 Oct: not due yet, and later than any imported period
    docs, fp = gst.parse_gstr1(gstr1("092026", b2b=[{"ctin": "27AABCA1234F1Z5", "inv": [inv_row("INV/26-27/0001", "02-09-2026", reported.total_minor / 100, 100000)]}]))
    gst.import_documents(w.db, w.e.id, docs, "GSTR1", "t", None)
    r = gst.crosscheck(w.db, w.e, TODAY)
    flagged = {x["number"]: x["flags"] for x in r["rows"]}
    assert flagged == {"INV/26-27/0002": ["not_in_gstr1"]} and r["counts"] == {"not_in_gstr1": 1} and r["gstr1_periods"] == ["092026"]
    assert r["open_flagged_minor"] == missing.total_minor and r["rows"][0]["severity"] == "high"


def test_without_any_imported_filing_nothing_is_judged_missing(w):
    c = setup(w)
    c.gstin = "27AABCA1234F1Z5"
    w.invoice(c, "INV/26-27/0001", 100_000_00, days_ago=45)
    r = gst.crosscheck(w.db, w.e, TODAY)
    assert r["flagged"] == 0 and r["gstr1_periods"] == [] and r["checked"] == 1


def test_gstin_and_value_mismatches_are_reported_against_the_filing(w):
    c = setup(w)
    c.gstin = "27AABCA1234F1Z5"
    a = w.invoice(c, "INV/26-27/0001", 100_000_00, days_ago=45)
    b = w.invoice(c, "INV/26-27/0002", 100_000_00, days_ago=44)
    docs, _ = gst.parse_gstr1(gstr1("092026", b2b=[{"ctin": "29AAAAA0000A1Z5", "inv": [inv_row("INV/26-27/0001", "02-09-2026", a.total_minor / 100, 100000)]},
                                                  {"ctin": "27AABCA1234F1Z5", "inv": [inv_row("INV/26-27/0002", "03-09-2026", 100000, 84745)]}]))
    gst.import_documents(w.db, w.e.id, docs, "GSTR1", "t", None)
    flagged = {x["number"]: x["flags"] for x in gst.crosscheck(w.db, w.e, TODAY)["rows"]}
    assert flagged == {"INV/26-27/0001": ["gstin_mismatch"], "INV/26-27/0002": ["value_mismatch"]}


def test_cancelled_irn_and_irn_mismatch_and_not_on_register(w):
    from app.ingestion.remittance import extract_upload

    c = setup(w)
    c.gstin = "27AABCA1234F1Z5"
    ok = w.invoice(c, "INV/26-27/0100", 100_000_00, days_ago=75)
    ok.irn = "aaa111"
    cnl = w.invoice(c, "INV/26-27/0101", 50_000_00, days_ago=70)
    cnl.irn = "bbb222"
    bad = w.invoice(c, "INV/26-27/0102", 50_000_00, days_ago=65)
    bad.irn = "zzz999"
    gone = w.invoice(c, "INV/26-27/0103", 50_000_00, days_ago=60)
    gone.irn = "ccc333"
    csv = IRP_CSV.replace(b"1,18,000.00", b"1,18,000.00") + b"ddd444,3,2026-08-10,INV/26-27/0102,2026-08-09,INV,27AABCA1234F1Z5,\"59,000.00\",ACT\n"
    gst.import_documents(w.db, w.e.id, gst.parse_irp(extract_upload("irp.csv", csv).text), "IRP", "t", None)
    flagged = {x["number"]: x["flags"] for x in gst.crosscheck(w.db, w.e, TODAY)["rows"]}
    assert flagged == {"INV/26-27/0101": ["irn_cancelled"], "INV/26-27/0102": ["irn_mismatch"], "INV/26-27/0103": ["not_on_irp"]}


def test_missing_irn_is_only_flagged_when_e_invoicing_applies_to_the_entity(w):
    c = setup(w)             # keeps its valid generated GSTIN: an unregistered (B2C) customer needs no IRN
    w.invoice(c, "INV/26-27/0001", 100_000_00, days_ago=5)
    assert gst.crosscheck(w.db, w.e, TODAY)["flagged"] == 0
    w.e.config = {**w.e.config, "e_invoice_required": True}
    r = gst.crosscheck(w.db, w.e, TODAY)
    assert [x["flags"] for x in r["rows"]] == [["irn_missing"]]
    c.gstin = None
    assert gst.crosscheck(w.db, w.e, TODAY)["flagged"] == 0


def test_quarterly_filers_are_given_until_the_13th_after_the_quarter(w):
    assert gst._return_due(date(2026, 8, 15), quarterly=False) == date(2026, 9, 11)
    assert gst._return_due(date(2026, 8, 15), quarterly=True) == date(2026, 10, 13)
    c = setup(w)
    c.gstin = "27AABCA1234F1Z5"
    w.invoice(c, "INV/26-27/0001", 100_000_00, days_ago=45)       # 2 Sep: monthly would be overdue (11 Oct) - a QRMP filer has until 13 Oct
    docs, _ = gst.parse_gstr1(gstr1("062026", b2b=[{"ctin": "27AABCA1234F1Z5", "inv": [inv_row("INV/26-27/9999", "01-06-2026", 1000, 847)]}]))
    gst.import_documents(w.db, w.e.id, docs, "GSTR1", "t", None)
    w.e.config = {**w.e.config, "gstr1_frequency": "quarterly"}
    assert gst.crosscheck(w.db, w.e, date(2026, 10, 12))["flagged"] == 0


def test_reimport_updates_a_cancelled_irn_instead_of_duplicating(w):
    from app.ingestion.remittance import extract_upload

    docs = gst.parse_irp(extract_upload("irp.csv", IRP_CSV.replace(b",CNL", b",ACT")).text)
    assert gst.import_documents(w.db, w.e.id, docs, "IRP", "t", None)["inserted"] == 2
    docs2 = gst.parse_irp(extract_upload("irp.csv", IRP_CSV).text)
    r = gst.import_documents(w.db, w.e.id, docs2, "IRP", "t", None)
    assert (r["inserted"], r["updated"], r["unchanged"]) == (0, 1, 1)


def test_api_import_and_crosscheck_with_roles(client, w):
    c = setup(w)
    c.gstin = "27AABCA1234F1Z5"
    w.invoice(c, "INV/26-27/0001", 100_000_00, days_ago=45)
    an, au = login(client, "ar_analyst@t.example", "pw-ar_analyst"), login(client, "auditor@t.example", "pw-auditor")
    body = gstr1("092026", b2b=[{"ctin": "27AABCA1234F1Z5", "inv": [inv_row("INV/26-27/0777", "02-09-2026", 1000, 847)]}])
    assert client.post("/api/v1/tax/gst/import", data={"entity_id": w.e.id, "source": "GSTR1"}, files={"file": ("g.json", body, "application/json")}, headers=au).status_code == 403
    r = client.post("/api/v1/tax/gst/import", data={"entity_id": w.e.id, "source": "GSTR1"}, files={"file": ("g.json", body, "application/json")}, headers=an)
    assert r.status_code == 200 and r.json()["inserted"] == 1 and r.json()["period"] == "092026"
    x = client.get(f"/api/v1/tax/gst/crosscheck?entity_id={w.e.id}", headers=au).json()
    assert x["flagged"] == 1 and x["rows"][0]["flags"] == ["not_in_gstr1"]
    assert client.post("/api/v1/tax/gst/import", data={"entity_id": w.e.id, "source": "IRP"}, files={"file": ("e.json", b"{}", "application/json")}, headers=an).status_code == 422
    assert client.post("/api/v1/tax/gst/import", data={"entity_id": w.e.id, "source": "GSTR2B"}, files={"file": ("g.json", body, "application/json")}, headers=an).status_code == 422
