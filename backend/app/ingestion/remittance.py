"""Remittance advice extraction (email bodies, PDF text, portal exports).

`RuleExtractor` is deterministic and always available. `LLMExtractor` is optional, redacts PII before
anything leaves the process, and ALWAYS falls back to rules on any failure. Matching never *depends* on
an LLM: extracted values are only evidence that the scorer weighs, and every extracted invoice reference
must resolve against real open items before it counts.
"""
from __future__ import annotations

import io
import json
import re
from typing import Protocol

from ..money import parse_amount

_AMT = r"(?:₹|Rs\.?|INR)?\s*\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|\d+\.\d{1,2}"
_DATE_LIKE = re.compile(r"^\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3})[-/.]\d{2,4}$")
_UTR_LABELED = re.compile(r"(?i)\b(?:UTR|RRN|ref(?:erence)?\s*(?:no|number|#)?)\s*[:#\-]?\s*([A-Z0-9]{10,30})\b")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
_LONGNUM = re.compile(r"(?<![\w.,])\d{11,20}(?![\w.,])")


def redact(text: str) -> str:
    """Best-effort PII masking before any external call. Invoice numbers and amounts are preserved."""
    t = _PAN.sub("[PAN]", text)
    t = _EMAIL.sub("[EMAIL]", t)
    t = _PHONE.sub("[PHONE]", t)
    return _LONGNUM.sub("[ACCT]", t)


class RemittanceExtractor(Protocol):
    name: str

    def extract(self, text: str) -> dict: ...


def _empty() -> dict:
    return {"payer_name": None, "utr": None, "total_minor": None, "tds_total_minor": None, "lines": [], "confidence": 0.0}


class RuleExtractor:
    name = "rules"

    def extract(self, text: str) -> dict:
        out = _empty()
        if not text:
            return out
        if m := _UTR_LABELED.search(text):
            out["utr"] = m.group(1).upper()
        if m := re.search(r"(?im)^\s*(?:from|payer|customer|vendor|company)\s*[:\-]\s*(.+)$", text):
            out["payer_name"] = m.group(1).strip()[:200]
        if m := re.search(rf"(?i)(?:total\s+(?:payment|paid|amount)|net\s+(?:amount\s+)?paid|amount\s+paid|payment\s+amount|total\s+net)\D{{0,12}}({_AMT})", text):
            out["total_minor"] = parse_amount(m.group(1))
        if m := re.search(rf"(?i)total\s+tds\D{{0,12}}({_AMT})", text):
            out["tds_total_minor"] = parse_amount(m.group(1))

        lines = []
        for raw in text.splitlines():
            ln = raw.strip()
            if not ln or re.match(r"(?i)^(total|grand|sub\s*total|net\s+paid|amount\s+paid)", ln):
                continue
            if re.search(r"(?i)\b(utr|rrn)\b", ln) or (out["utr"] and out["utr"] in ln.upper()):
                continue
            ref = self._first_doc_ref(ln)
            if not ref:
                continue
            tds = None
            if m := re.search(rf"(?i)\bTDS\b\D{{0,10}}({_AMT})", ln):
                tds = parse_amount(m.group(1))
            ded = None
            if m := re.search(rf"(?i)\b(?:deduction|discount|short(?:age)?|penalty|rate\s+diff\w*)\b\D{{0,10}}({_AMT})", ln):
                ded = parse_amount(m.group(1))
            net = None
            if m := re.search(rf"(?i)\b(?:net|paid|payment|pay)\b\D{{0,10}}({_AMT})", ln):
                net = parse_amount(m.group(1))
            stripped = re.sub(rf"(?i)\b(?:TDS|deduction|discount|net|paid|payment|pay)\b\D{{0,10}}(?:{_AMT})", "", ln)
            stripped = stripped.replace(ref, " ")
            stripped = re.sub(r"\b\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3})[-/.]\d{2,4}\b", " ", stripped)  # drop dates
            amounts = []
            for m in re.finditer(rf"(\(\s*)?(?<![\w/-])({_AMT})(?![\w/])", stripped):
                if not re.search(r"[.,]", m.group(2)):
                    continue  # bare integers are day numbers / counts, not money
                v = parse_amount(m.group(2))
                if v:
                    amounts.append(-v if m.group(1) else v)
            gross = amounts[0] if amounts else None
            if net is None and gross is not None:
                net = gross - (tds or 0) - (ded or 0) if (tds or ded) else gross
            lines.append({"ref": ref, "gross_minor": gross, "amount_minor": net, "tds_minor": tds, "deduction_minor": ded,
                          "kind": "credit_note" if re.search(r"(?i)\b(CN|credit\s*note)\b", ln) else "invoice"})
        out["lines"] = lines
        out["confidence"] = 0.6 if lines else (0.3 if out["utr"] else 0.0)
        return out

    @staticmethod
    def _first_doc_ref(line: str) -> str | None:
        for i, tok in enumerate(re.split(r"[\s,;|]+", line)):
            tok = tok.strip("()[]:")
            if len(tok) < 4 or not any(c.isdigit() for c in tok) or _DATE_LIKE.match(tok):
                continue
            if re.fullmatch(r"[\d,]+(\.\d{1,2})?", tok) and not (i == 0 and "," not in tok and "." not in tok):
                continue  # money
            if re.fullmatch(r"\d{10,}", tok) and re.search(r"(?i)utr|rrn", line):
                continue
            if any(c.isalpha() for c in tok) or i == 0:
                return tok
        return None


class LLMExtractor:
    """Optional. Calls the Anthropic Messages API over HTTPS with redacted text. UNTESTED against the live API
    in this repo's CI (tests inject a fake transport); validate with your own key before relying on it."""

    name = "llm"

    def __init__(self, api_key: str, model: str, fallback: RemittanceExtractor | None = None, transport=None):
        self.api_key, self.model = api_key, model
        self.fallback = fallback or RuleExtractor()
        self.transport = transport

    PROMPT = (
        "Extract payment remittance details from the text. Reply with ONLY JSON: "
        '{"payer_name":str|null,"utr":str|null,"total":number|null,"tds_total":number|null,'
        '"lines":[{"ref":str,"gross":number|null,"paid":number|null,"tds":number|null,"deduction":number|null}]}. '
        "Amounts are in rupees. Never invent values; use null when absent."
    )

    def extract(self, text: str) -> dict:
        try:
            body = {"model": self.model, "max_tokens": 1500,
                    "messages": [{"role": "user", "content": f"{self.PROMPT}\n\n---\n{redact(text)[:12000]}"}]}
            if self.transport:
                raw = self.transport(body)
            else:
                import httpx

                from ..config import check_egress

                check_egress("https://api.anthropic.com")   # raises in data-residency strict mode unless explicitly allow-listed

                r = httpx.post("https://api.anthropic.com/v1/messages", json=body, timeout=30,
                               headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
                r.raise_for_status()
                raw = r.json()["content"][0]["text"]
            data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
            return self._validate(data, text)
        except Exception:
            return self.fallback.extract(text)

    @staticmethod
    def _m(v) -> int | None:
        return None if v is None else int(round(float(v) * 100))

    def _validate(self, d: dict, original: str) -> dict:
        out = _empty()
        out["payer_name"] = str(d["payer_name"])[:200] if d.get("payer_name") else None
        out["utr"] = str(d["utr"]).upper()[:30] if d.get("utr") else None
        out["total_minor"], out["tds_total_minor"] = self._m(d.get("total")), self._m(d.get("tds_total"))
        for ln in d.get("lines") or []:
            ref = str(ln.get("ref") or "").strip()
            if not ref or ref.upper() not in original.upper():  # reject hallucinated references
                continue
            out["lines"].append({"ref": ref, "gross_minor": self._m(ln.get("gross")), "amount_minor": self._m(ln.get("paid")),
                                 "tds_minor": self._m(ln.get("tds")), "deduction_minor": self._m(ln.get("deduction")), "kind": "invoice"})
        out["confidence"] = 0.75 if out["lines"] else 0.3
        return out


def get_extractor() -> RemittanceExtractor:
    from ..config import settings

    from ..config import egress_allowed

    if settings.anthropic_api_key and egress_allowed("api.anthropic.com"):
        return LLMExtractor(settings.anthropic_api_key, settings.llm_model)
    return RuleExtractor()


def text_from_upload(filename: str, data: bytes) -> str:
    n = (filename or "").lower()
    if n.endswith(".pdf"):
        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    if n.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        ws = load_workbook(io.BytesIO(data), data_only=True, read_only=True).active
        return "\n".join("  ".join("" if c is None else str(c) for c in r) for r in ws.iter_rows(values_only=True))
    if n.endswith(".eml"):
        import email
        from email import policy

        msg = email.message_from_bytes(data, policy=policy.default)
        body = msg.get_body(preferencelist=("plain", "html"))
        head = f"From: {msg.get('From', '')}\nSubject: {msg.get('Subject', '')}\n"
        return head + (body.get_content() if body else "")
    return data.decode("utf-8", errors="replace")
