from datetime import date

import pytest
from sqlalchemy import func, select

from app.ingestion import service as ing
from app.ingestion.remittance import LLMExtractor, RuleExtractor, redact
from app.ingestion.statements import StatementError, detect_and_parse, parse_camt053, parse_csv, parse_gateway_csv, parse_mt940
from app.models import Customer, Invoice, PaymentTransaction

HDFC = b"""Account Statement,,,,,,
Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance
01/10/26,"NEFT-HDFCN26274000111-ACME INDUSTRIES PVT LTD-INV 1001-HDFC0000123",HDFCN26274000111,01/10/26,,"1,18,000.00","5,01,18,000.00"
02/10/26,CHARGES FOR NEFT,,02/10/26,"23.60",,"5,01,17,976.40"
Closing balance,,,,,,
"""


def test_csv_with_preamble_indian_numbers_and_debits():
    rows = parse_csv(HDFC)
    assert len(rows) == 2
    assert rows[0].amount_minor == 11800000 and rows[0].direction == "credit" and rows[0].txn_date == date(2026, 10, 1)
    assert rows[1].direction == "debit" and rows[1].amount_minor == 2360
    assert rows[0].balance_minor == 5011800000


def test_csv_alternative_layout_single_amount_with_drcr():
    data = b"Txn Date;Description;Amount;Dr/Cr;Balance\n03-Oct-2026;UPI/427812345678/CR/ACME/x@y;25,000.00;CR;1,000.00\n04-Oct-2026;VENDOR;500.00;DR;500.00\n"
    r = parse_csv(data)
    assert [(x.direction, x.amount_minor) for x in r] == [("credit", 2500000), ("debit", 50000)]


def test_csv_without_recognisable_header_is_a_clear_error():
    with pytest.raises(StatementError):
        parse_csv(b"foo,bar\n1,2\n")


MT940 = b"""{1:F01HDFCINBBAXXX0000000000}{2:O9400000000000HDFCINBBAXXX00000000000000000000N}{4:
:20:STMT001
:25:50200012345678
:28C:1/1
:60F:C261001INR100000,00
:61:2610021002C118000,00NTRFNONREF//BANKREF1
:86:NEFT-HDFCN26275000222-ACME INDUSTRIES-INV 1001
:61:2610031003D500,00NCHGNONREF
:86:CHARGES
:62F:C261003INR217500,00
-}"""


def test_mt940():
    r = parse_mt940(MT940)
    assert len(r) == 2 and r[0].amount_minor == 11800000 and r[0].direction == "credit" and r[0].currency == "INR"
    assert "ACME INDUSTRIES" in r[0].narration and r[1].direction == "debit"


CAMT = b"""<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
<Ntry><Amt Ccy="INR">59000.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-10-05</Dt></BookgDt><ValDt><Dt>2026-10-05</Dt></ValDt>
<NtryDtls><TxDtls><Refs><EndToEndId>HDFCR26278000333</EndToEndId></Refs><RmtInf><Ustrd>INV 1002 Acme</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
</Stmt></BkToCstmrStmt></Document>"""


def test_camt053_and_autodetect():
    r = parse_camt053(CAMT)
    assert r[0].amount_minor == 5900000 and r[0].utr_hint == "HDFCR26278000333" and "INV 1002" in r[0].narration
    assert detect_and_parse("x.xml", CAMT)[0] == "camt053"
    assert detect_and_parse("x.txt", MT940)[0] == "mt940"
    assert detect_and_parse("x.csv", HDFC)[0] == "csv"


def test_gateway_settlement_splits_fee_and_tax():
    csv_ = b"Payment ID,Order ID,Amount,Fee,Tax,Settled At,Settlement UTR\npay_1,INV-1,1000.00,20.00,3.60,05/10/2026,RZP1\n"
    r = parse_gateway_csv(csv_)[0]
    assert r.amount_minor == 97640 and r.fee_minor == 2360 and r.extra["gross_minor"] == 100000


def test_ingestion_is_idempotent_and_keeps_real_repeats(w):
    first = ing.ingest_statement(w.db, w.bank, "s.csv", HDFC, "t", None)
    assert first["inserted"] == 2 and first["duplicates_skipped"] == 0
    again = ing.ingest_statement(w.db, w.bank, "s.csv", HDFC, "t", None)
    assert again["inserted"] == 0 and again["duplicates_skipped"] == 2
    # two genuinely identical credits on one day (no UTR, same balance) must both survive in a single file
    twin = b"Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n01/10/26,CASH DEPOSIT CDM,,10000.00,1000.00\n01/10/26,CASH DEPOSIT CDM,,10000.00,1000.00\n"
    assert ing.ingest_statement(w.db, w.bank, "t.csv", twin, "t", None)["inserted"] == 2
    assert w.db.scalar(select(func.count()).select_from(PaymentTransaction)) == 4


def test_debits_are_stored_but_never_matchable(w):
    ing.ingest_statement(w.db, w.bank, "s.csv", HDFC, "t", None)
    deb = w.db.execute(select(PaymentTransaction).where(PaymentTransaction.direction == "debit")).scalar_one()
    assert deb.status.value == "ignored"


def test_malformed_upload_raises_clean_error(w):
    with pytest.raises(StatementError):
        ing.ingest_statement(w.db, w.bank, "x.csv", b"\x00\x01\x02garbage", "t", None)


ROWS = [{"invoice_no": "INV-1", "customer_code": "C1", "customer_name": "Acme Industries", "total": "1,18,000.00", "taxable": "100000", "gst": "18000",
         "invoice_date": "01/09/2026", "due_date": "01/10/2026", "gstin": "27AABCA1234F1Z5"},
        {"invoice_no": "CN-1", "customer_code": "C1", "customer_name": "Acme Industries", "total": "5,000.00", "doc_type": "Credit Note", "invoice_date": "05/09/2026"}]


def test_open_item_import_validates_gstin_and_signs_credit_notes(w):
    out = ing.import_open_items(w.db, w.e, ROWS, "t", None)
    assert out["customers_created"] == 1 and out["invoices_created"] == 2
    assert any("GSTIN" in x for x in out["warnings"])  # fake checksum flagged, not silently accepted
    cn = w.db.execute(select(Invoice).where(Invoice.number == "CN-1")).scalar_one()
    assert cn.total_minor == -500000 and cn.open_minor == -500000 and cn.doc_type == "credit_note"


def test_reimport_does_not_resurrect_balances_already_applied_locally(w):
    ing.import_open_items(w.db, w.e, ROWS[:1], "t", None)
    inv = w.db.execute(select(Invoice)).scalar_one()
    t = w.credit(11800000, "NEFT-HDFCN26274000111-ACME INDUSTRIES-INV-1")
    w.run(t)
    w.db.refresh(inv)
    assert inv.open_minor == 0
    ing.import_open_items(w.db, w.e, ROWS[:1], "t", None)  # ERP extract still shows it open (cash not posted back yet)
    w.db.refresh(inv)
    assert inv.open_minor == 0, "re-import must subtract cash we applied but have not posted back"


def test_rule_extractor_parses_advice_with_tds_and_credit_note():
    t = """From: Acme Industries Pvt Ltd
UTR: HDFCN26274000111
INV-1001  01-Sep-2026  1,18,000.00  TDS 1,000.00  Net 1,17,000.00
INV-1002  05-Sep-2026  59,000.00
CN-0042 credit note (5,000.00)
Total payment: 1,71,000.00"""
    x = RuleExtractor().extract(t)
    assert x["utr"] == "HDFCN26274000111" and x["total_minor"] == 17100000 and x["payer_name"].startswith("Acme")
    l = {ln["ref"]: ln for ln in x["lines"]}
    assert l["INV-1001"]["tds_minor"] == 100000 and l["INV-1001"]["amount_minor"] == 11700000 and l["INV-1001"]["gross_minor"] == 11800000
    assert l["INV-1002"]["amount_minor"] == 5900000
    assert l["CN-0042"]["amount_minor"] == -500000 and l["CN-0042"]["kind"] == "credit_note"


def test_redaction_masks_pii_but_keeps_invoice_numbers():
    r = redact("PAN AABCA1234F mail a.b@x.com ph 9876543210 acct 50200012345678 INV-1001 amt 1,18,000.00")
    assert "AABCA1234F" not in r and "x.com" not in r and "9876543210" not in r and "50200012345678" not in r
    assert "INV-1001" in r and "1,18,000.00" in r


def test_llm_extractor_rejects_hallucinated_refs_and_falls_back_on_failure():
    text = "Paid INV-1001 amount 100"
    good = LLMExtractor("k", "m", transport=lambda body: '{"payer_name":"Acme","utr":null,"total":100,"lines":[{"ref":"INV-1001","paid":100},{"ref":"INV-9999","paid":5}]}')
    out = good.extract(text)
    assert [l["ref"] for l in out["lines"]] == ["INV-1001"], "invented invoice numbers must be dropped"

    def boom(_):
        raise RuntimeError("network down")

    assert LLMExtractor("k", "m", transport=boom).extract("INV-1001  1,000.00")["lines"][0]["ref"] == "INV-1001"
    junk = LLMExtractor("k", "m", transport=lambda b: "not json at all")
    assert junk.extract("INV-1001  1,000.00")["lines"]


def test_camt053_rejects_xxe_and_entity_bombs():
    xxe = b'''<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]><Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><Ntry><Amt Ccy="INR">1</Amt><AddtlNtryInf>&x;</AddtlNtryInf></Ntry></Document>'''
    with pytest.raises(StatementError):
        parse_camt053(xxe)
    bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><Document><Ntry>&b;</Ntry></Document>'
    with pytest.raises(StatementError):
        parse_camt053(bomb)


def test_overlong_values_never_crash_an_import_or_a_statement_load(w):
    """PostgreSQL enforces VARCHAR lengths (SQLite does not): over-long inputs must be rejected/truncated by the app with a clear message."""
    rows = [{"invoice_no": "X" * 120, "customer_code": "C", "customer_name": "Name", "total": "100", "invoice_date": "2026-09-01"},
            {"invoice_no": "OK-1", "customer_code": "C" * 80, "customer_name": "Name", "total": "100", "invoice_date": "2026-09-01"},
            {"invoice_no": "OK-2", "customer_code": "C2", "customer_name": "N" * 400, "total": "100", "invoice_date": "2026-09-01"},
            {"invoice_no": "OK-3", "customer_code": "C3", "customer_name": "Fine", "total": "100", "invoice_date": "2026-09-01"}]
    out = ing.import_open_items(w.db, w.e, rows, "t", None)
    assert out["rejected"] == 3 and out["invoices_created"] == 1 and any("maximum 60" in x for x in out["warnings"])
    w.db.flush()
    big = ("Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n01/10/26,NEFT-HDFCN26274000111-" + "A" * 400 + "-INV 1,,1000.00,5000.00\n").encode()
    assert ing.ingest_statement(w.db, w.bank, "s.csv", big, "t", None)["inserted"] == 1
    w.db.flush()
    t = w.db.execute(select(PaymentTransaction)).scalar_one()
    assert len(t.payer_name or "") <= 250 and len(t.utr or "") <= 40
