"""Renderers for specific ERPs, written from each vendor's published import format.

NONE OF THESE HAS BEEN IMPORTED INTO A REAL ERP. Each produces a file (or a payload for your middleware); the first import in a test company is
the real test, and the field choices below are the part most likely to need one adjustment per customer. Every connector carries the same
guarantees as the generic ones: the file is built from the already-balanced canonical journal, base-currency totals are checked to balance per
voucher before anything is written, and the export is checksummed and stored before the batch is marked exported.

Options live in the entity configuration under integrations.erp_options.<connector>.
"""
from __future__ import annotations

import csv
import io
import json
import re
from xml.sax.saxutils import escape

from ..models import LegalEntity, PostingBatch
from .types import Rendered


class RenderError(ValueError):
    pass


def _vouchers(batch: PostingBatch) -> dict[str, list]:
    out: dict[str, list] = {}
    for ln in batch.lines:
        out.setdefault(ln.voucher_no, []).append(ln)
    for vno, lines in out.items():
        if sum(x.base_debit_minor for x in lines) != sum(x.base_credit_minor for x in lines):
            raise RenderError(f"Voucher {vno} does not balance in base currency; refusing to render")
    return out


def _amt(minor: int) -> str:
    return f"{minor / 100:.2f}"


def _need(options: dict, *keys: str) -> list[str]:
    return [f"{k} is required" for k in keys if not str(options.get(k, "")).strip()]


class _Native:
    status = "available"
    unverified = True
    required: tuple[str, ...] = ()

    def __init__(self, options: dict | None = None):
        self.options = options or {}

    def with_options(self, options: dict | None):
        return type(self)(options)

    def validate(self, options: dict) -> list[str]:
        return _need(options, *self.required)


# ---------------------------------------------------------------------------------------------- Tally Prime (XML import)
class TallyPrime(_Native):
    name, label = "tally_prime", "Tally Prime (XML vouchers, import via Gateway of Tally)"
    required = ("company",)

    def render(self, batch: PostingBatch, entity: LegalEntity) -> Rendered:
        o = self.options
        company = o.get("company") or entity.name
        party_by = o.get("party_by", "name")
        lmap = o.get("ledger_map") or {}
        parts = []
        for vno, lines in _vouchers(batch).items():
            is_receipt = vno.startswith("CA-")
            vtype = "Receipt" if is_receipt else "Journal"
            first = lines[0]
            party = next((getattr(x, "_party_name" if party_by == "name" else "_party", None) for x in lines if getattr(x, "_party", None)), None)
            ref = next((x.narration for x in lines if x.line_type == "bank_receipt"), first.narration)
            entries = []
            for x in lines:
                dr, cr = x.base_debit_minor, x.base_credit_minor
                led = lmap.get(x.account, x.account)
                bills = ""
                if x.line_type in ("ar_clearance", "unapplied_cash") and getattr(x, "_party", None):
                    led = getattr(x, "_party_name" if party_by == "name" else "_party", None) or led
                    kind = "Agst Ref" if getattr(x, "_inv", None) else "On Account"
                    bill = escape(getattr(x, "_inv", None) or vno)
                    amt = (dr or cr)
                    bills = (f"<BILLALLOCATIONS.LIST><NAME>{bill}</NAME><BILLTYPE>{kind}</BILLTYPE><AMOUNT>{_amt(amt) if cr else '-' + _amt(amt)}</AMOUNT></BILLALLOCATIONS.LIST>")
                # Tally: a debit is ISDEEMEDPOSITIVE=Yes with a negative amount; a credit is No with a positive amount
                entries.append(f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{escape(led)}</LEDGERNAME><ISDEEMEDPOSITIVE>{'Yes' if dr else 'No'}</ISDEEMEDPOSITIVE>"
                               f"<AMOUNT>{'-' + _amt(dr) if dr else _amt(cr)}</AMOUNT>{bills}</ALLLEDGERENTRIES.LIST>")
            parts.append(f'<TALLYMESSAGE xmlns:UDF="TallyUDF"><VOUCHER VCHTYPE="{vtype}" ACTION="Create" OBJVIEW="Accounting Voucher View">'
                         f"<DATE>{first.posting_date:%Y%m%d}</DATE><VOUCHERTYPENAME>{vtype}</VOUCHERTYPENAME><VOUCHERNUMBER>{escape(vno)}</VOUCHERNUMBER>"
                         f"<NARRATION>{escape(ref[:250])}</NARRATION>" + (f"<PARTYLEDGERNAME>{escape(party)}</PARTYLEDGERNAME>" if party and is_receipt else "")
                         + "".join(entries) + "</VOUCHER></TALLYMESSAGE>")
        xml = ("<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME>"
               f"<STATICVARIABLES><SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY></STATICVARIABLES></REQUESTDESC><REQUESTDATA>" + "".join(parts)
               + "</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>")
        return Rendered(f"cashapp_batch_{batch.id}_tally.xml", xml.encode("utf-8"), "application/xml")

    def validate(self, options: dict) -> list[str]:
        errs = super().validate(options)
        if options.get("party_by", "name") not in ("name", "code"):
            errs.append("party_by must be name or code")
        return errs


# ---------------------------------------------------------------------------------------------- SAP (BAPI_ACC_DOCUMENT_POST-shaped JSON for middleware)
class SapBapi(_Native):
    name, label = "sap_s4", "SAP S/4HANA (BAPI_ACC_DOCUMENT_POST-shaped JSON for your middleware)"
    required = ("company_code",)

    @staticmethod
    def _pad(code: str) -> str:
        return code.zfill(10) if code.isdigit() else code

    def render(self, batch: PostingBatch, entity: LegalEntity) -> Rendered:
        o = self.options
        docs = []
        for vno, lines in _vouchers(batch).items():
            first = lines[0]
            hdr = {"COMP_CODE": o["company_code"], "DOC_DATE": f"{first.posting_date:%Y%m%d}", "PSTNG_DATE": f"{first.posting_date:%Y%m%d}", "DOC_TYPE": o.get("doc_type", "DZ"),
                   "REF_DOC_NO": vno[:16], "HEADER_TXT": first.narration[:25], "USERNAME": o.get("username", "CASHAPP")}
            gl, ar, amounts = [], [], []
            for i, x in enumerate(lines, start=1):
                item = str(i).zfill(10)
                signed = x.base_debit_minor - x.base_credit_minor
                if x.line_type in ("ar_clearance",) and getattr(x, "_party", None):
                    ar.append({"ITEMNO_ACC": item, "CUSTOMER": self._pad(x._party), "ITEM_TEXT": x.narration[:50], "ALLOC_NMBR": (getattr(x, "_inv", None) or "")[:18],
                               "REF_KEY_1": (getattr(x, "_inv", None) or "")[:12]})
                else:
                    gl.append({"ITEMNO_ACC": item, "GL_ACCOUNT": self._pad(o.get("ledger_map", {}).get(x.account, x.account)), "ITEM_TEXT": x.narration[:50]})
                amounts.append({"ITEMNO_ACC": item, "CURR_TYPE": "00", "CURRENCY": entity.base_currency, "AMT_DOCCUR": f"{signed / 100:.2f}"})   # BAPI sign: debit +, credit -
            docs.append({"voucher_no": vno, "DOCUMENTHEADER": hdr, "ACCOUNTGL": gl, "ACCOUNTRECEIVABLE": ar, "CURRENCYAMOUNT": amounts})
        body = {"schema": "sap.BAPI_ACC_DOCUMENT_POST/shape-only", "legal_entity": entity.name, "batch_id": batch.id, "idempotency_key": batch.idempotency_key,
                "note": ("Field names follow the BAPI structures. This posts the payment with its invoice reference but does NOT clear open items in SAP: use your automatic "
                         "clearing (for example F.13) or a clearing step in your middleware. Foreign-currency receipts are sent in base currency; the document amounts are in the narration."),
                "documents": docs}
        return Rendered(f"cashapp_batch_{batch.id}_sap.json", json.dumps(body, indent=2).encode(), "application/json")


# ---------------------------------------------------------------------------------------------- Dynamics 365 Business Central (journal lines JSON)
class D365BusinessCentral(_Native):
    name, label = "d365_bc", "Dynamics 365 Business Central (general journal lines JSON)"
    required = ("journal",)

    def render(self, batch: PostingBatch, entity: LegalEntity) -> Rendered:
        o = self.options
        lines_out, n = [], 0
        for vno, lines in _vouchers(batch).items():
            for x in lines:
                n += 10000
                cust = getattr(x, "_party", None) if x.line_type in ("ar_clearance", "unapplied_cash") else None
                bank = x.line_type == "bank_receipt"
                signed = x.base_debit_minor - x.base_credit_minor        # BC journals: debit +, credit -
                row = {"journalDisplayName": o["journal"], "lineNumber": n, "accountType": "Customer" if cust else "Bank Account" if bank else "G/L Account",
                       "accountNumber": cust or o.get("ledger_map", {}).get(x.account, x.account), "postingDate": x.posting_date.isoformat(), "documentNumber": vno[:20],
                       "description": x.narration[:100], "amount": float(f"{signed / 100:.2f}")}
                if cust and getattr(x, "_inv", None):
                    row |= {"appliesToDocumentType": "Invoice", "appliesToDocumentNumber": x._inv}
                lines_out.append(row)
        body = {"schema": "bc.journalLines/shape-only", "company_id": o.get("company_id"), "batch_id": batch.id, "idempotency_key": batch.idempotency_key,
                "note": "POST each element to .../companies({id})/journals({journalId})/journalLines. Field names follow the Business Central v2.0 API pages; amounts are in base currency.",
                "journalLines": lines_out}
        return Rendered(f"cashapp_batch_{batch.id}_bc.json", json.dumps(body, indent=2).encode(), "application/json")


# ---------------------------------------------------------------------------------------------- NetSuite (journal entry CSV import)
class NetSuiteJournal(_Native):
    name, label = "netsuite", "NetSuite (journal entry CSV for the Import Assistant)"
    required = ("subsidiary",)

    def render(self, batch: PostingBatch, entity: LegalEntity) -> Rendered:
        o = self.options
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["External ID", "Date", "Subsidiary", "Currency", "Exchange Rate", "Account", "Debit", "Credit", "Memo", "Name"])
        for vno, lines in _vouchers(batch).items():
            for x in lines:
                w.writerow([vno, f"{x.posting_date:%d/%m/%Y}", o["subsidiary"], entity.base_currency, "1", o.get("ledger_map", {}).get(x.account, x.account),
                            _amt(x.base_debit_minor) if x.base_debit_minor else "", _amt(x.base_credit_minor) if x.base_credit_minor else "", x.narration[:200],
                            getattr(x, "_party", "") or ""])
        return Rendered(f"cashapp_batch_{batch.id}_netsuite.csv", buf.getvalue().encode(), "text/csv")


NATIVE: dict[str, _Native] = {c.name: c for c in (TallyPrime(), SapBapi(), D365BusinessCentral(), NetSuiteJournal())}


def validate_options(options: dict) -> list[str]:
    errs = []
    for name, o in (options or {}).items():
        if name not in NATIVE:
            errs.append(f"erp_options: unknown connector '{name}'")
        elif not isinstance(o, dict):
            errs.append(f"erp_options.{name}: must be an object")
        else:
            errs += [f"erp_options.{name}: {e}" for e in NATIVE[name].validate(o)]
            lm = o.get("ledger_map")
            if lm is not None and (not isinstance(lm, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in lm.items())):
                errs.append(f"erp_options.{name}: ledger_map must map ledger codes to ledger names")
    return errs
