"""Find invoice references in free text (bank narration, remittance advice): exact numbers, IRN, PO, bare serials."""
from __future__ import annotations

import re

from ..indian import normalise_ref
from ..models import Invoice
from .context import Ctx

_TOKEN = re.compile(r"[A-Z0-9]+")
_HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")


def strip_ids(text: str, *ids: str | None) -> str:
    """Remove UTRs/RRNs/IFSC/VPAs so their digits are not mistaken for invoice serials."""
    t = text
    for i in ids:
        if i:
            t = t.replace(i, " ")
    t = re.sub(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", " ", t, flags=re.I)
    t = re.sub(r"[\w.]+@\w+", " ", t)
    t = re.sub(r"(?<!\d)\d{10,}(?!\d)", " ", t)
    return t


def serial_of(number: str) -> str | None:
    """Trailing digit run of an invoice number, leading zeros stripped: 'INV/24-25/0123' -> '123'."""
    m = re.search(r"(\d+)\D*$", number)
    return m.group(1).lstrip("0") or None if m else None


_RANGE = re.compile(r"(?<![\d/])(\d{3,9})\s*(?:-|–|TO|THRU|THROUGH)\s*(\d{3,9})(?![\d/])")


def expand_ranges(text: str, max_span: int = 25) -> set[str]:
    """'INV 1001-1004' / '1001 TO 1004' -> {'1001','1002','1003','1004'} (leading zeros stripped, like serial_of)."""
    out: set[str] = set()
    for a, b in _RANGE.findall(text):
        lo, hi = int(a), int(b)
        if 0 < hi - lo <= max_span and len(a) == len(b):
            out |= {str(n) for n in range(lo, hi + 1)}
    return out


def find_references(ctx: Ctx, text: str, candidate_customers: list[int] | None = None) -> dict[int, str]:
    """invoice_id -> hit kind ('exact' | 'irn' | 'po' | 'serial').

    `serial` hits only consider open invoices of `candidate_customers` and only when the serial is unambiguous
    within each customer — bare numbers are far weaker evidence than full document numbers."""
    up = text.upper()
    hits: dict[int, str] = {}
    toks = _TOKEN.findall(up)
    grams: set[str] = set()
    for i in range(len(toks)):
        acc = ""
        for j in range(i, min(i + 6, len(toks))):
            acc += toks[j]
            if len(acc) >= 4:
                grams.add(acc)
    for g in grams:
        for inv in ctx.by_norm.get(g, ()):
            # purely numeric invoice numbers need to be whole tokens to avoid matching inside other numbers
            if g.isdigit() and g not in toks:
                continue
            hits[inv.id] = "exact"
        for inv in ctx.by_po.get(g, ()):
            if inv.open_minor != 0 and inv.id not in hits:
                hits[inv.id] = "po"
    for h in _HEX64.findall(text):
        if inv := ctx.by_irn.get(h.upper()):
            hits[inv.id] = "irn"

    if candidate_customers:
        digit_toks = {t.lstrip("0") for t in toks if t.isdigit() and 3 <= len(t) <= 9}
        digit_toks |= expand_ranges(up)
        for cid in candidate_customers:
            by_serial: dict[str, list[Invoice]] = {}
            for inv in ctx.open_by_cust.get(cid, ()):
                s = serial_of(inv.number)
                if s and len(s) >= 3:
                    by_serial.setdefault(s, []).append(inv)
            for s, invs in by_serial.items():
                if s in digit_toks and len(invs) == 1 and invs[0].id not in hits:
                    hits[invs[0].id] = "serial"
    return hits
