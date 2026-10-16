"""India-specific primitives: GSTIN/PAN validation, TDS catalogue, narration parsing, name normalisation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^[0-3][0-9][A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def gstin_checksum(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        p = _CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += p // 36 + p % 36
    return _CHARS[(36 - total % 36) % 36]


def is_valid_gstin(g: str | None) -> bool:
    if not g:
        return False
    g = g.upper()
    return bool(GSTIN_RE.match(g)) and gstin_checksum(g[:14]) == g[14]


def make_gstin(state_code: str, pan: str, entity_no: str = "1") -> str:
    base = f"{state_code}{pan}{entity_no}Z"
    return base + gstin_checksum(base)


def is_valid_pan(p: str | None) -> bool:
    return bool(p and PAN_RE.match(p.upper()))


def pan_from_gstin(g: str | None) -> str | None:
    return g[2:12].upper() if g and len(g) == 15 else None


def pan_category(pan: str | None) -> str:
    """4th PAN character = holder type (C company, P individual, H HUF, F firm, T trust, ...)."""
    return pan[3].upper() if pan and len(pan) == 10 else "?"


def financial_year(d: date) -> tuple[str, str]:
    """India FY runs Apr-Mar. Returns ('2025-26', 'Q3')."""
    start = d.year if d.month >= 4 else d.year - 1
    q = {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}[d.month]
    return f"{start}-{str(start + 1)[2:]}", f"Q{q}"


# ---------------------------------------------------------------- TDS catalogue
# DEFAULTS for matching hints only; configurable per customer. Statutory rates change with each
# Finance Act, so finance must confirm them. Rates in basis points.
TDS_SECTIONS: dict[str, dict] = {
    "194C": {"label": "Contractors (company payee 2%, individual/HUF payee 1%)", "rate_bp": 200, "alt_bp": 100},
    "194J": {"label": "Professional fees", "rate_bp": 1000},
    "194J-T": {"label": "Technical services / call centre", "rate_bp": 200},
    "194H": {"label": "Commission / brokerage", "rate_bp": 200},
    "194I": {"label": "Rent (plant & machinery 2%, land & building 10%)", "rate_bp": 1000, "alt_bp": 200},
    "194Q": {"label": "Purchase of goods above threshold", "rate_bp": 10},
    "194O": {"label": "E-commerce operators", "rate_bp": 10},
    "GST-TDS": {"label": "GST TDS (government / PSU deductors)", "rate_bp": 200},
}
# Total rates tried when a shortfall might be TDS. Combos cover e.g. 194C 2% + GST-TDS 2% = 4%.
CANDIDATE_TDS_RATES_BP = [10, 100, 200, 300, 400, 500, 1000, 1200]


def default_tds_section_for_bp(bp: int) -> str | None:
    return {10: "194Q", 100: "194C", 200: "194C", 300: "194C", 400: "194C+GST-TDS", 500: "194H", 1000: "194J", 1200: "194J+GST-TDS"}.get(bp)


# ---------------------------------------------------------------- names
_STOP = {
    "PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "MS", "M", "S", "CO", "COMPANY", "INC", "CORP", "CORPORATION",
    "THE", "OF", "AND",
}


def normalise_name(name: str | None) -> str:
    if not name:
        return ""
    s = name.upper().replace("M/S", " ").replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in _STOP)


def normalise_ref(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def normalise_account(a: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (a or "").upper()).lstrip("0")


def mask_account(a: str | None) -> str | None:
    if not a:
        return a
    return "X" * max(len(a) - 4, 0) + a[-4:]


# ---------------------------------------------------------------- narration parsing
@dataclass
class ParsedNarration:
    channel: str = "OTHER"
    utr: str | None = None
    payer_name: str | None = None
    payer_ifsc: str | None = None
    payer_vpa: str | None = None
    remark: str = ""
    kind: str = "customer"      # customer | interest | charges | reversal | gateway | cash_deposit
    extra: dict = field(default_factory=dict)


_UTR_LONG = re.compile(r"\b([A-Z]{4}[A-Z0-9]{12,24})\b")
_RRN12 = re.compile(r"(?<!\d)(\d{12})(?!\d)")
_VPA = re.compile(r"(?<![A-Za-z0-9._])([A-Za-z0-9._]{2,}@[A-Za-z]{2,})\b")
_IFSC = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")
_BANKWORD = {"HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "YES", "PNB", "BOB", "CANARA", "IDFC", "INDUSIND", "UNION", "BOI"}
_GATEWAYS = ("RAZORPAY", "PAYU", "CASHFREE", "BILLDESK", "CCAVENUE", "PAYTM", "PHONEPE", "STRIPE", "INSTAMOJO")
_INTEREST = re.compile(r"\b(INT\.?\s*PD|INTEREST\s+(PAID|CREDIT)|CREDIT\s+INTEREST|SB\s+INT)\b", re.I)
_CHARGES = re.compile(r"\b(CHARGES?|CHGS?|GST\s+ON|SERVICE\s+TAX|AMC|SMS\s+ALERT)\b", re.I)
_REVERSAL = re.compile(r"\b(REVERSAL|REFUND|RETURNED|FAILED\s+TXN|RTN)\b", re.I)
_RAIL = re.compile(r"\b(NEFT|RTGS|IMPS|UPI)\b|MMT/", re.I)
_SKIP = {"NEFT", "RTGS", "IMPS", "UPI", "CR", "DR", "MMT", "ACH", "C", "NACH", "CLG", "CHQ", "DEP", "P2A", "P2M", "P2P", "IN", "BY", "TRANSFER",
         "CREDIT", "BIL", "INFT", "INF", "IFT", "INWARD", "REMITTANCE", "FROM", "TO", "FIRC", "SWIFT", "RETURN"}
_FX = re.compile(r"\b(USD|EUR|GBP|AED|SGD|JPY|CAD|AUD|CHF|HKD|CNY)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
_RETURN_START = re.compile(r"^\s*(NEFT|RTGS|IMPS|UPI)?[\s\-/]*(RETURN|RTN|REVERSAL|REV)\b", re.I)


def _is_utr_like(tok: str) -> bool:
    return sum(c.isdigit() for c in tok) >= 6 and len(tok) >= 14 and not IFSC_RE.match(tok)


def parse_narration(text: str, rules: list[dict] | None = None) -> ParsedNarration:
    """Best-effort extraction from Indian bank narrations (NEFT/RTGS/IMPS/UPI/cheque/ACH/gateway)."""
    raw = (text or "").strip()
    up = raw.upper()
    p = ParsedNarration()
    if not raw:
        return p

    rail = bool(_RAIL.search(raw))
    if _RETURN_START.match(raw):
        p.kind = "reversal"
    elif _INTEREST.search(raw):
        p.kind = "interest"
    elif _REVERSAL.search(raw) and not rail:
        p.kind = "reversal"
    elif _CHARGES.search(raw) and not rail:
        p.kind = "charges"

    if re.search(r"SETTL", up):
        for gw in _GATEWAYS:
            if gw in up:
                p.kind, p.channel, p.payer_name = "gateway", "GATEWAY", gw.title()
                break

    if p.channel != "GATEWAY":
        if re.search(r"\bUPI\b", up):
            p.channel = "UPI"
        elif re.search(r"\bRTGS\b", up):
            p.channel = "RTGS"
        elif re.search(r"\bNEFT\b", up):
            p.channel = "NEFT"
        elif re.search(r"\bIMPS\b", up) or "MMT/" in up:
            p.channel = "IMPS"
        elif re.search(r"\b(ACH|NACH|ECS)\b", up):
            p.channel = "ACH"
        elif re.search(r"\b(CHQ|CHEQUE|CLG|CLEARING)\b", up):
            p.channel = "CHEQUE"
        elif re.search(r"\b(CASH\s+DEP|CDM)\b", up):
            p.channel, p.kind = "CASH", "cash_deposit"
        elif re.search(r"\b(INWARD\s+REMITTANCE|SWIFT|FIRC|FCY\s+CR)\b", up):
            p.channel = "SWIFT"
            if m := _FX.search(raw):
                p.extra["fx_ccy"], p.extra["fx_minor"] = m.group(1).upper(), int(round(float(m.group(2).replace(",", "")) * 100))
            if m := re.search(r"\bFROM\s+([A-Za-z][A-Za-z .&]{2,60}?)(?=\s*[-/]|$)", raw):
                p.payer_name = m.group(1).strip()

    if m := _VPA.search(raw):
        p.payer_vpa = m.group(1).lower()
    if m := _IFSC.search(up):
        p.payer_ifsc = m.group(1)

    if p.channel in ("UPI", "IMPS"):
        if m := _RRN12.search(raw):
            p.utr = m.group(1)
    elif p.channel == "CHEQUE":
        if m := re.search(r"(?:CHQ|CHEQUE|CLG)\D{0,12}(\d{6})\b", up):
            p.utr = m.group(1)
    if not p.utr:
        for tok in _UTR_LONG.findall(up):
            if _is_utr_like(tok):
                p.utr = tok
                break

    if not p.utr and p.channel in ("NEFT", "RTGS"):  # SBI-style "INF/NEFT/<12-digit ref>/IFSC/NAME"
        if m := re.search(r"(?<!\d)(\d{10,22})(?!\d)", raw):
            p.utr = m.group(1)

    name, remark_parts = None, []
    for s in (x.strip() for x in re.split(r"[-/*|]|\s{2,}", raw)):
        if not s:
            continue
        u = s.upper()
        if u in _SKIP or u in _BANKWORD:
            continue
        if p.utr and p.utr in u.replace(" ", ""):
            continue
        if _IFSC.fullmatch(u) or "@" in s or re.fullmatch(r"[\d\s.,]+", s):
            continue
        s = " ".join(w for w in s.split() if w.upper() not in _SKIP)
        if not s:
            continue
        letters = sum(c.isalpha() for c in s)
        digits = sum(c.isdigit() for c in s)
        looks_ref = bool(re.search(r"\b(INV|INVOICE|BILL|PO|REF|PAYMENT|PYMT|PMT|FOR|AGAINST|ADV|ADVANCE|SETTL\w*)\b", s.upper())) or digits >= 1
        if name is None and letters >= 3 and not looks_ref:
            name = s
        else:
            if name is None and letters >= 6:
                # name and reference share one delimiter-less segment, e.g. "ACME INDUSTRIES PVT LTD BILL 0320"
                lead = []
                for w in s.split():
                    if re.search(r"\d", w) or re.fullmatch(r"(?i)INV|INVOICE|BILL|PO|REF|PAYMENT|PYMT|PMT|FOR|AGAINST|ADV|ADVANCE|SETTL\w*", w):
                        break
                    lead.append(w)
                if len(lead) >= 2 and sum(c.isalpha() for c in " ".join(lead)) >= 6:
                    name = " ".join(lead)
            remark_parts.append(s)
    if p.kind != "gateway" and not p.payer_name:
        p.payer_name = name
    p.remark = " ".join(remark_parts).strip()
    if rules:
        _apply_rules(p, raw, rules)
    return p


MAX_RULES, MAX_PATTERN = 30, 300
_NESTED_QUANT = re.compile(r"\([^)]*[+*][^)]*\)[+*{]")   # (a+)+ style catastrophic-backtracking shapes
_FIELDS = ("utr", "payer", "ifsc", "vpa", "remark")


def validate_rules(rules: list[dict]) -> list[str]:
    """Bank-specific narration rules are admin-supplied regexes. Reject anything malformed or ReDoS-shaped at save time."""
    errs: list[str] = []
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        return [f"rules must be a list of at most {MAX_RULES}"]
    for i, r in enumerate(rules):
        pat = r.get("pattern") if isinstance(r, dict) else None
        if not isinstance(pat, str) or not pat or len(pat) > MAX_PATTERN:
            errs.append(f"rule {i + 1}: pattern required (max {MAX_PATTERN} chars)")
            continue
        if _NESTED_QUANT.search(pat):
            errs.append(f"rule {i + 1}: nested quantifiers are not allowed (catastrophic backtracking risk)")
            continue
        try:
            rx = re.compile(pat, re.I)
        except re.error as e:
            errs.append(f"rule {i + 1}: invalid regex ({e})")
            continue
        for f in _FIELDS:
            g = r.get(f)
            if g and g not in rx.groupindex:
                errs.append(f"rule {i + 1}: '{f}' refers to group '{g}' which the pattern does not define")
        if r.get("kind") and r["kind"] not in ("customer", "interest", "charges", "reversal", "gateway", "cash_deposit"):
            errs.append(f"rule {i + 1}: unknown kind '{r['kind']}'")
    return errs


def _apply_rules(p: ParsedNarration, raw: str, rules: list[dict]) -> None:
    for r in rules[:MAX_RULES]:
        try:
            m = re.search(r["pattern"][:MAX_PATTERN], raw, re.I)
        except (re.error, KeyError, TypeError):
            continue
        if not m:
            continue
        g = m.groupdict()
        if r.get("channel"):
            p.channel = str(r["channel"]).upper()
        if r.get("kind"):
            p.kind = r["kind"]
        for f, attr in (("utr", "utr"), ("payer", "payer_name"), ("ifsc", "payer_ifsc"), ("vpa", "payer_vpa"), ("remark", "remark")):
            v = (g.get(r.get(f) or "") or "").strip()
            if v:
                setattr(p, attr, v.lower() if f == "vpa" else (v.upper() if f in ("utr", "ifsc") else v))
        p.extra["rule"] = r.get("name") or r["pattern"][:40]
        return   # first matching rule wins
