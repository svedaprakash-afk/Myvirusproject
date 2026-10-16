"""Statement layout templates for common Indian banks, WRITTEN FROM MEMORY of their typical corporate/current-account exports.

They are starting points for the profile editor, not guarantees: banks revise layouts, and corporate exports differ by product
and by who configured them. Always run the file through *Preview* first; if a template doesn't fit, adjust the profile.
"""
from __future__ import annotations

BANK_TEMPLATES: dict[str, dict] = {
    "HDFC Bank (current account CSV)": {
        "note": "Columns: Date, Narration, Chq./Ref.No., Value Dt, Withdrawal Amt., Deposit Amt., Closing Balance. Dates dd/mm/yy.",
        "profile": {"format": "csv", "date_format": "%d/%m/%y"}},
    "ICICI Bank (CorpNet / current account)": {
        "note": "Columns: S No., Value Date, Transaction Date, Cheque Number, Transaction Remarks, Withdrawal Amount (INR ), Deposit Amount (INR ), Balance (INR ). Several preamble rows; dd/mm/yyyy.",
        "profile": {"format": "csv", "date_format": "%d/%m/%Y", "columns": {"date": "Transaction Date", "narration": "Transaction Remarks"}}},
    "State Bank of India (CMP / internet banking)": {
        "note": "Columns: Txn Date, Value Date, Description, Ref No./Cheque No., Debit, Credit, Balance. Dates like '12 Oct 2026'.",
        "profile": {"format": "csv", "date_format": "%d %b %Y"}},
    "Axis Bank": {
        "note": "Columns: SRL NO, Tran Date, CHQNO, PARTICULARS, DR, CR, BAL, SOL. dd-mm-yyyy.",
        "profile": {"format": "csv", "date_format": "%d-%m-%Y", "columns": {"date": "Tran Date", "narration": "PARTICULARS", "debit": "DR", "credit": "CR", "balance": "BAL", "ref": "CHQNO"}}},
    "Kotak Mahindra Bank": {
        "note": "Columns: Transaction Date, Value Date, Description, Chq/Ref No., Debit, Credit, Balance, Dr/Cr.",
        "profile": {"format": "csv", "date_format": "%d-%m-%Y", "columns": {"date": "Transaction Date", "narration": "Description"}}},
    "Yes Bank": {
        "note": "Columns: Transaction Date, Value Date, Narration, Cheque No/Reference No, Withdrawals, Deposits, Running Balance.",
        "profile": {"format": "csv", "date_format": "%d-%m-%Y"}},
    "IndusInd Bank": {
        "note": "Columns: Date, Particulars, Chq/Ref No, Withdrawal Amt, Deposit Amt, Balance.",
        "profile": {"format": "csv", "date_format": "%d-%b-%Y"}},
    "IDFC FIRST Bank": {
        "note": "Columns: Transaction Date, Value Date, Particulars, Cheque No., Debit, Credit, Balance.",
        "profile": {"format": "csv", "date_format": "%d-%b-%Y"}},
    "Punjab National Bank": {
        "note": "Columns: Txn Date, Description/Narration, Branch Name, Cheque Number, Dr Amount, Cr Amount, Balance.",
        "profile": {"format": "csv", "date_format": "%d/%m/%Y", "columns": {"debit": "Dr Amount", "credit": "Cr Amount"}}},
    "Bank of Baroda": {
        "note": "Columns: Tran Date, Value Date, Narration, Chq No, Withdrawal, Deposit, Balance.",
        "profile": {"format": "csv", "date_format": "%d/%m/%Y"}},
    "Canara Bank": {
        "note": "Columns: Txn Date, Value Date, Cheque No., Description, Branch Code, Debit, Credit, Balance.",
        "profile": {"format": "csv", "date_format": "%d-%m-%Y"}},
    "SWIFT MT940 (any bank)": {"note": "Standard MT940 message file.", "profile": {"format": "mt940"}},
    "ISO 20022 camt.053 (any bank)": {"note": "Standard camt.053 XML.", "profile": {"format": "camt053"}},
    "USD account (any bank)": {"note": "Marks every line as USD.", "profile": {"currency": "USD"}},
}
