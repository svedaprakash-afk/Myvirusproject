"""Layouts and narrations below are WRITTEN FROM MEMORY of how major Indian banks typically format exports. They prove the parser is
consistent with those recollections, not that any bank still exports this way. Verify with a real sample via Preview."""
import pytest

from app.indian import parse_narration
from app.ingestion.bank_templates import BANK_TEMPLATES
from app.ingestion.statements import parse_csv

L = {  # bank -> (preamble rows, header, a credit row, a debit row, expected credit paise, template name)
 "hdfc": (["Account Statement", "Customer: X"], "Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance",
          "01/10/26,NEFT CR-HDFC0000123-ACME-INV 1-HDFCN52026100112345,HDFCN52026100112345,01/10/26,,\"1,18,000.00\",\"9,00,000.00\"", "02/10/26,CHARGES,,02/10/26,23.60,,\"8,99,976.40\"", 11800000, "HDFC Bank (current account CSV)"),
 "icici": (["Account Number: 000405012345", "Transactions List - ACME PVT LTD", ""], "S No.,Value Date,Transaction Date,Cheque Number,Transaction Remarks,Withdrawal Amount (INR ),Deposit Amount (INR ),Balance (INR )",
           "1,01/10/2026,01/10/2026,,NEFT-ICICN52026100112345-ACME-INV 1,0.00,118000.00,900000.00", "2,02/10/2026,02/10/2026,,CHARGES,23.60,0.00,899976.40", 11800000, "ICICI Bank (CorpNet / current account)"),
 "sbi": (["Account Name : ACME"], "Txn Date,Value Date,Description,Ref No./Cheque No.,Debit,Credit,Balance",
         "01 Oct 2026,01 Oct 2026,BY TRANSFER-NEFT*SBIN0001234*SBINN52026100112345*ACME--,SBINN52026100112345,,\"1,18,000.00\",\"9,00,000.00\"", "02 Oct 2026,02 Oct 2026,SMS CHARGES,,23.60,,\"8,99,976.40\"", 11800000, "State Bank of India (CMP / internet banking)"),
 "axis": ([], "SRL NO,Tran Date,CHQNO,PARTICULARS,DR,CR,BAL,SOL",
          "1,01-10-2026,,NEFT/UTIBN52026100112345/ACME/INV 1,,118000.00,900000.00,123", "2,02-10-2026,,CHARGES,23.60,,899976.40,123", 11800000, "Axis Bank"),
 "kotak": (["Statement"], "Transaction Date,Value Date,Description,Chq/Ref No.,Debit,Credit,Balance,Dr / Cr",
           "01-10-2026,01-10-2026,NEFT-KKBKN52026100112345-ACME-INV 1,,,\"1,18,000.00\",\"9,00,000.00\",CR", "02-10-2026,02-10-2026,CHARGES,,23.60,,\"8,99,976.40\",CR", 11800000, "Kotak Mahindra Bank"),
 "yes": ([], "Transaction Date,Value Date,Narration,Cheque No/Reference No,Withdrawals,Deposits,Running Balance",
         "01-10-2026,01-10-2026,NEFT CR-YESBN52026100112345-ACME-INV 1,,,118000.00,900000.00", "02-10-2026,02-10-2026,CHARGES,,23.60,,899976.40", 11800000, "Yes Bank"),
 "idfc": ([], "Transaction Date,Value Date,Particulars,Cheque No.,Debit,Credit,Balance",
          "01-Oct-2026,01-Oct-2026,NEFT-IDFBN52026100112345-ACME-INV 1,,,118000.00,900000.00", "02-Oct-2026,02-Oct-2026,CHARGES,,23.60,,899976.40", 11800000, "IDFC FIRST Bank"),
 "pnb": ([], "Txn Date,Description/Narration,Branch Name,Cheque Number,Dr Amount,Cr Amount,Balance",
         "01/10/2026,NEFT-PUNBN52026100112345-ACME-INV 1,MUMBAI,,,118000.00,900000.00", "02/10/2026,CHARGES,MUMBAI,,23.60,,899976.40", 11800000, "Punjab National Bank"),
}


@pytest.mark.parametrize("bank", list(L))
@pytest.mark.parametrize("use_template", [False, True])
def test_bank_layout_is_read(bank, use_template):
    pre, header, credit, debit, amt, tname = L[bank]
    data = "\n".join([*pre, header, credit, debit]).encode()
    prof = BANK_TEMPLATES[tname]["profile"] if use_template else {}
    rows = parse_csv(data, prof)
    assert len(rows) == 2, rows
    cr = next(r for r in rows if r.direction == "credit")
    assert cr.amount_minor == amt and cr.txn_date.year == 2026 and cr.txn_date.month == 10 and cr.balance_minor
    assert next(r for r in rows if r.direction == "debit").amount_minor == 2360


MORE = [  # (narration, channel, utr, payer-substring)
 ("RTGS CR-UTIB0000789-GUJARAT ALLOYS LTD-RTGS PAYMENT-UTIBR52026100800123456", "RTGS", "UTIBR52026100800123456", "GUJARAT ALLOYS"),
 ("NEFT CR-ICIC0000456-KAVERI TOOLS-SALES-ICICN52026100112350", "NEFT", "ICICN52026100112350", "KAVERI TOOLS"),
 ("BIL/NEFT/001234567890/ACME INDUSTRIES/INV 1023", "NEFT", "001234567890", None),
 ("INF/INFT/012345678901/ACME INDUSTRIES", "OTHER", None, None),
 ("MMT/IMPS/427812345678/INV 1023/ACME TRADERS/Axis Bank", "IMPS", "427812345678", "ACME TRADERS"),
 ("UPI/P2A/427812345678/ACME TRADERS/HDFC Bank/INV 1023", "UPI", "427812345678", "ACME TRADERS"),
 ("IMPS/P2A/427812345678/ACME TRADERS/INV 1023", "IMPS", "427812345678", "ACME TRADERS"),
 ("UPI/CR/427812345678/ACME TRADERS/acme@ybl/INV 1023", "UPI", "427812345678", "ACME TRADERS"),
 ("BY TRANSFER-UPI/CR/427812345678/ACME TRADERS/YESB/acme@ybl/Payment", "UPI", "427812345678", "ACME TRADERS"),
 ("BY CLEARING-CHQ NO 123456 ACME INDUSTRIES", "CHEQUE", "123456", None),
 ("CLG/ACME INDUSTRIES/CHQ 654321", "CHEQUE", "654321", None),
 ("NACH CR-ACME INDUSTRIES-MANDATE 99", "ACH", None, "ACME INDUSTRIES"),
 ("IMPS-427812345678-ACME TRADERS-KKBK-XXXXXXXX9876-INV 1023", "IMPS", "427812345678", "ACME TRADERS"),
 ("UPI-ACME TRADERS-acme@okaxis-UTIB0000123-427812345678-PAYMENT", "UPI", "427812345678", "ACME TRADERS"),
 ("NEFT-IDFBN52026100112345-ACME INDUSTRIES LLP-INV 1023 1024", "NEFT", "IDFBN52026100112345", "ACME INDUSTRIES"),
 ("RTGS-PUNBR52026100800123456-ACME INDUSTRIES LTD-BILL 55", "RTGS", "PUNBR52026100800123456", "ACME INDUSTRIES"),
 ("TRANSFER FROM ACME INDUSTRIES PVT LTD-INV 1023", "OTHER", None, None),
]


@pytest.mark.parametrize("nar,channel,utr,payer", MORE, ids=[m[0][:44] for m in MORE])
def test_more_narrations(nar, channel, utr, payer):
    r = parse_narration(nar)
    assert r.channel == channel, r
    if utr:
        assert r.utr == utr, r
    if payer:
        assert r.payer_name and payer in r.payer_name.upper(), r
