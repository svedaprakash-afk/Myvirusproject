"""Form 26AS / AIS import and reconciliation."""
import io
from datetime import date

import pytest
from sqlalchemy import select

from app.ingestion import form26as
from app.models import Customer, Form26asEntry, TdsEntry
from tests.conftest import login
from tests.test_reports import add_tds

TXT = """Form 26AS Annual Tax Statement
PART-A - Details of Tax Deducted at Source
^Sr. No.^Name of Deductor^TAN of Deductor^Total Amount Paid / Credited(Rs.)^Total Tax Deducted(Rs.)^Total TDS Deposited(Rs.)
^1^ACME INDUSTRIES PVT LTD^MUMA12345B^5,00,000.00^10,000.00^10,000.00
^^Sr. No.^Section^Transaction Date^Status of Booking^Date of Booking^Remarks^Amount Paid / Credited(Rs.)^Tax Deducted(Rs.)^TDS Deposited(Rs.)
^^1^194C^15-Jun-2026^F^30-Jul-2026^-^3,00,000.00^6,000.00^6,000.00
^^2^194C^20-Sep-2026^P^30-Oct-2026^-^2,00,000.00^4,000.00^4,000.00
^2^BHARAT RAIL EQUIPMENTS LTD^DELB98765C^1,00,000.00^1,000.00^1,000.00
^^1^194Q^05-Aug-2026^F^30-Oct-2026^-^1,00,000.00^1,000.00^1,000.00
^3^UNKNOWN BUYER CO^KOLU55555U^50,000.00^500.00^500.00
^^1^194J^10-Aug-2026^F^30-Oct-2026^-^50,000.00^500.00^500.00
PART-B - Details of Tax Collected at Source
^1^SOME SELLER^MUMS11111A^100.00^1.00^1.00
^^1^206C(1H)^01-May-2026^F^30-Jul-2026^-^100.00^1.00^1.00
"""


def test_txt_layout_parses_deductor_blocks_and_ignores_tcs_lines():
    rows = form26as.parse_txt(TXT)
    got = [(r.tan, r.section, r.txn_date, r.status, r.deposited) for r in rows]
    assert len(rows) == 4, "the Part B (TCS) line must not be read as TDS credit"
    assert got[:3] == [("MUMA12345B", "194C", date(2026, 6, 15), "F", 6_000_00), ("MUMA12345B", "194C", date(2026, 9, 20), "P", 4_000_00),
                       ("DELB98765C", "194Q", date(2026, 8, 5), "F", 1_000_00)]
    assert rows[0].name == "ACME INDUSTRIES PVT LTD" and rows[2].name == "BHARAT RAIL EQUIPMENTS LTD" and rows[0].paid == 3_00_000_00


CSV = (b"Deductor Name,Deductor TAN,Section,Date of Payment/Credit,Status of Booking,Amount Paid / Credited,Tax Deducted,TDS Deposited\n"
       b"Acme Industries Pvt Ltd,MUMA12345B,194C,2026-06-15,F,\"300,000.00\",\"6,000.00\",\"6,000.00\"\n"
       b"Acme Industries Pvt Ltd,MUMA12345B,194C,2026-09-20,F,\"200,000.00\",\"4,000.00\",\"4,000.00\"\n")


def test_csv_and_xlsx_exports_with_named_columns_parse_too():
    rows = form26as.parse_upload("26as.csv", CSV)
    assert [(r.tan, r.deposited, r.status) for r in rows] == [("MUMA12345B", 6_000_00, "F"), ("MUMA12345B", 4_000_00, "F")]
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["AIS - TDS information"])
    ws.append(["Information source", "TAN", "Information description", "Transaction date", "Amount reported", "Tax deducted", "TDS deposited"])
    ws.append(["Acme Industries Pvt Ltd", "MUMA12345B", "194C", date(2026, 6, 15), 300000, 6000, 6000])
    b = io.BytesIO()
    wb.save(b)
    rows2 = form26as.parse_upload("ais.xlsx", b.getvalue())
    assert [(r.tan, r.section, r.txn_date, r.deposited) for r in rows2] == [("MUMA12345B", "194C", date(2026, 6, 15), 6_000_00)]


def test_unsupported_and_unrecognised_files_are_refused_with_a_reason():
    with pytest.raises(form26as.Form26asError, match="JSON"):
        form26as.parse_upload("ais.json", b"{}")
    with pytest.raises(form26as.Form26asError, match="PDF"):
        form26as.parse_upload("26as.pdf", b"%PDF")
    with pytest.raises(form26as.Form26asError, match="No TDS lines"):
        form26as.parse_upload("random.csv", b"a,b\n1,2\n")


def world(w):
    acme = w.customer("C1", "Acme Industries Pvt Ltd")
    bharat = w.customer("C2", "Bharat Rail Equipments Ltd", pan_seed="AABCB1234F")
    ia, ib = w.invoice(acme, "INV-A", 100_000_00), w.invoice(bharat, "INV-B", 100_000_00)
    return acme, bharat, ia, ib


def test_import_maps_deductors_by_name_records_the_tan_and_is_idempotent(w):
    acme, bharat, *_ = world(w)
    res = form26as.import_entries(w.db, w.e.id, form26as.parse_txt(TXT), "26AS", "t", None)
    assert res["inserted"] == 4
    assert acme.tan == "MUMA12345B" and bharat.tan == "DELB98765C" and res["mapped_notes"] == [], "exact name matches need no warning"
    assert res["unmapped_deductors"] == [{"tan": "KOLU55555U", "name": "UNKNOWN BUYER CO"}]
    again = form26as.import_entries(w.db, w.e.id, form26as.parse_txt(TXT), "26AS", "t", None)
    assert again["inserted"] == 0 and again["unchanged"] == res["inserted"]


def test_reimport_updates_a_provisional_line_to_final(w):
    world(w)
    form26as.import_entries(w.db, w.e.id, form26as.parse_txt(TXT), "26AS", "t", None)
    final = TXT.replace("20-Sep-2026^P^", "20-Sep-2026^F^")
    r = form26as.import_entries(w.db, w.e.id, form26as.parse_txt(final), "26AS", "t", None)
    assert r["updated"] == 1 and r["inserted"] == 0
    assert w.db.execute(select(Form26asEntry).where(Form26asEntry.txn_date == date(2026, 9, 20))).scalar_one().booking_status == "F"


def test_two_identical_lines_in_one_file_are_two_entries_not_a_duplicate(w):
    acme, *_ = world(w)
    rows = form26as.parse_upload("a.csv", CSV)
    r = form26as.import_entries(w.db, w.e.id, rows + rows[:1], "26AS", "t", None)
    assert r["inserted"] == 3


def test_every_reconciliation_status(w):
    acme, bharat, ia, ib = world(w)
    carol = w.customer("C3", "Carol Pumps Ltd", pan_seed="AABCC1234F")
    dan = w.customer("C4", "Dan Valves Ltd", pan_seed="AABCD1234F")
    eve = w.customer("C5", "Eve Fittings Ltd", pan_seed="AABCE1234F")
    for c, tan in ((acme, "MUMA12345B"), (bharat, "DELB98765C"), (carol, "BLRC11111C"), (dan, "CHED22222D"), (eve, "PUNE33333E")):
        c.tan = tan
    ic, idn, ie = (w.invoice(c, f"INV-{c.code}", 100_000_00) for c in (carol, dan, eve))
    add_tds(w, acme, ia, 6_000_00, date(2026, 6, 15))                # Q1 matches
    add_tds(w, acme, ia, 4_000_00, date(2026, 9, 20))                # Q2: statement has it as provisional only
    add_tds(w, bharat, ib, 1_000_00, date(2026, 6, 28))              # we booked in Q1 ...
    add_tds(w, carol, ic, 3_000_00, date(2026, 7, 2))                # nothing reported for Carol
    add_tds(w, dan, idn, 2_000_00, date(2026, 8, 1))                 # statement shows more than us
    add_tds(w, eve, ie, 2_000_00, date(2026, 8, 1))                  # statement shows less than us
    from app.models import Form26asEntry as F

    def f(c, tan, d, amt, status="F", sec="194C"):
        w.db.add(F(entity_id=w.e.id, source="26AS", fy="2026-27", quarter=("Q1" if d.month < 7 else "Q2" if d.month < 10 else "Q3"), deductor_tan=tan, deductor_name=c.name,
                   section=sec, txn_date=d, booking_status=status, amount_paid_minor=0, tax_deducted_minor=amt, tds_deposited_minor=amt, customer_id=c.id, dedupe_key=f"{tan}{d}{amt}"))

    f(acme, "MUMA12345B", date(2026, 6, 15), 6_000_00)
    f(acme, "MUMA12345B", date(2026, 9, 20), 4_000_00, status="P")
    f(bharat, "DELB98765C", date(2026, 7, 3), 1_000_00)              # ... the deductor reported it in Q2: a timing difference
    f(dan, "CHED22222D", date(2026, 8, 1), 5_000_00)
    f(eve, "PUNE33333E", date(2026, 8, 1), 500_00)
    w.db.flush()
    r = form26as.reconcile(w.db, w.e.id, "2026-27")
    st = {(x["code"], x["quarter"]): x["status"] for x in r["rows"]}
    assert st[("C1", "Q1")] == "matched"
    assert st[("C1", "Q2")] == "ours_higher" and next(x for x in r["rows"] if x["code"] == "C1" and x["quarter"] == "Q2")["unconfirmed_minor"] == 4_000_00
    assert st[("C2", "Q1")] == "timing" and st[("C2", "Q2")] == "timing"
    assert st[("C3", "Q2")] == "not_reported"
    assert st[("C4", "Q2")] == "reported_higher" and st[("C5", "Q2")] == "ours_higher"
    assert r["unclaimed_minor"] == 3_000_00 and r["at_risk_minor"] == 4_000_00 + 3_000_00 + 1_500_00


def test_apply_marks_confirmed_entries_oldest_first_and_never_clears_a_manual_flag(w):
    acme, bharat, ia, ib = world(w)
    acme.tan = "MUMA12345B"
    t1 = add_tds(w, acme, ia, 3_000_00, date(2026, 6, 10))
    t2 = add_tds(w, acme, ia, 3_000_00, date(2026, 6, 20))
    manual = add_tds(w, acme, ia, 1_000_00, date(2026, 9, 1), in26=True)
    w.db.add(Form26asEntry(entity_id=w.e.id, source="26AS", fy="2026-27", quarter="Q1", deductor_tan="MUMA12345B", deductor_name="Acme", section="194C",
                           txn_date=date(2026, 6, 15), booking_status="F", tax_deducted_minor=3_000_00, tds_deposited_minor=3_000_00, customer_id=acme.id, dedupe_key="x"))
    w.db.flush()
    r = form26as.reconcile(w.db, w.e.id, "2026-27", apply_flags=True)
    assert (t1.in_26as, t2.in_26as, manual.in_26as) == (True, False, True) and r["flagged"] == 1


def test_manual_mapping_links_existing_entries_and_refuses_a_tan_owned_by_someone_else(w):
    acme, bharat, *_ = world(w)
    form26as.import_entries(w.db, w.e.id, form26as.parse_txt(TXT.replace("ACME INDUSTRIES PVT LTD", "A C M E TRADING")), "26AS", "t", None)
    assert acme.tan is None
    n = form26as.map_deductor(w.db, w.e.id, "MUMA12345B", acme.id, "t", 1)
    assert n == 2 and acme.tan == "MUMA12345B"
    with pytest.raises(form26as.Form26asError, match="already assigned"):
        form26as.map_deductor(w.db, w.e.id, "MUMA12345B", bharat.id, "t", 1)
    with pytest.raises(form26as.Form26asError, match="TAN is"):
        form26as.map_deductor(w.db, w.e.id, "bad", acme.id, "t", 1)


def test_api_import_reconcile_and_roles(client, w):
    acme, bharat, ia, ib = world(w)
    add_tds(w, acme, ia, 6_000_00, date(2026, 6, 15))
    add_tds(w, acme, ia, 4_000_00, date(2026, 9, 20))
    an, au = login(client, "ar_analyst@t.example", "pw-ar_analyst"), login(client, "auditor@t.example", "pw-auditor")
    assert client.post("/api/v1/tax/26as/import", data={"entity_id": w.e.id}, files={"file": ("x.csv", CSV, "text/csv")}, headers=au).status_code == 403
    r = client.post("/api/v1/tax/26as/import", data={"entity_id": w.e.id, "source": "26AS"}, files={"file": ("x.csv", CSV, "text/csv")}, headers=an)
    assert r.status_code == 200 and r.json()["inserted"] == 2 and acme.tan == "MUMA12345B"
    bad = client.post("/api/v1/tax/26as/import", data={"entity_id": w.e.id}, files={"file": ("x.json", b"{}", "application/json")}, headers=an)
    assert bad.status_code == 422 and "JSON" in bad.json()["detail"]
    rec = client.get(f"/api/v1/tax/26as/reconciliation?entity_id={w.e.id}&fy=2026-27", headers=au).json()
    assert rec["summary"]["matched"] == 2 and rec["ours_minor"] == rec["reported_minor"] == 10_000_00
    assert client.post(f"/api/v1/tax/26as/reconciliation/apply?entity_id={w.e.id}&fy=2026-27", headers=an).json()["flagged"] == 2
    assert all(t.in_26as for t in w.db.execute(select(TdsEntry)).scalars())
    assert len(client.get(f"/api/v1/tax/26as/entries?entity_id={w.e.id}", headers=au).json()) == 2
