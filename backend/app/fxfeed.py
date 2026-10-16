"""Reference-rate feed. Since July 2018 the "RBI reference rate" is computed and published by FBIL (Financial Benchmarks India),
on weekdays (USD/INR ~1:00 pm IST). This module turns whatever FBIL publishes (CSV/TXT, HTML table, XLSX) into stored rates.

HONEST STATUS: the real publication URL and exact layout could not be verified from the build environment. The parser is therefore
deliberately tolerant and is tested against fixtures written from secondary descriptions (date, currency pair, rate, JPY per 100).
Point FX_FEED_URL at the live publication (or upload the downloaded file) and check the first import by eye."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .config import check_egress, settings
from .fx import MICRO, parse_rate
from .models import ExchangeRate, Invoice

# Ten most-traded currencies (BIS Triennial Survey ordering, excluding INR) + AED, which matters to Indian trade.
TOP10 = ("USD", "EUR", "JPY", "GBP", "CNY", "AUD", "CAD", "CHF", "HKD", "SGD")
KNOWN = TOP10 + ("AED",)
FBIL_DIRECT = ("USD", "EUR", "GBP", "JPY")        # the RBI/FBIL reference-rate set; everything else must be derived or entered
_CCY = re.compile(r"\b(" + "|".join(KNOWN) + r")\b", re.I)
_DATES = [(re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"), (re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b"), "dmy"),
          (re.compile(r"\b(\d{1,2})[ \-]([A-Za-z]{3})[a-z]*[ ,\-]*(\d{4})\b"), "dMy")]
_RATE = re.compile(r"(?<![\d./-])(\d{1,6}\.\d{2,6})(?![\d/-])")
MAX_MOVE = Decimal("0.05")   # a day-on-day move above 5% is rejected, not stored: far more likely a parse/unit error than a real move


@dataclass
class Parsed:
    rates: list[tuple[str, date, str]] = field(default_factory=list)   # (currency, date, rate as decimal string, INR per 1 unit)
    skipped: list[str] = field(default_factory=list)


def _to_text(data: bytes, filename: str) -> str:
    n = (filename or "").lower()
    if n.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        ws = load_workbook(io.BytesIO(data), data_only=True, read_only=True).active
        return "\n".join("\t".join("" if c is None else (c.date().isoformat() if isinstance(c, datetime) else str(c)) for c in r) for r in ws.iter_rows(values_only=True))
    t = data.decode("utf-8-sig", errors="replace")
    if "<table" in t.lower() or "<tr" in t.lower():
        t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
        t = re.sub(r"</tr>|<br\s*/?>", "\n", t, flags=re.I)
        t = re.sub(r"</t[dh]>", "\t", t, flags=re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = re.sub(r"&nbsp;", " ", t)
    return t


def _find_date(line: str) -> date | None:
    for rx, kind in _DATES:
        m = rx.search(line)
        if not m:
            continue
        try:
            if kind == "ymd":
                return date(int(m[1]), int(m[2]), int(m[3]))
            if kind == "dmy":
                return date(int(m[3]), int(m[2]), int(m[1]))
            return datetime.strptime(f"{m[1]} {m[2][:3].title()} {m[3]}", "%d %b %Y").date()
        except ValueError:
            continue
    return None


def parse_reference_rates(data: bytes, filename: str = "", as_of: date | None = None, wanted: tuple[str, ...] | None = None) -> Parsed:
    """INR per 1 unit of foreign currency. Handles 'USD/INR', 'USD', 'INR / 1 USD'-style rows, tabs/commas/HTML cells, and JPY quoted per 100."""
    out, seen = Parsed(), set()
    for raw in _to_text(data, filename).splitlines():
        line = raw.strip()
        m = _CCY.search(line)
        if not m:
            continue
        ccy = m.group(1).upper()
        if wanted and ccy not in wanted:
            continue
        d = _find_date(line) or as_of
        if d is None:
            out.skipped.append(f"no date: {line[:70]}")
            continue
        stripped = _DATES[0][0].sub(" ", line)
        for rx, _ in _DATES[1:]:
            stripped = rx.sub(" ", stripped)
        nums = _RATE.findall(stripped)
        if not nums:
            out.skipped.append(f"no rate: {line[:70]}")
            continue
        v = Decimal(nums[-1] if len(nums) > 1 and not re.search(r"\bINR\b.*\b" + ccy + r"\b", line, re.I) else nums[0])
        per100 = ccy == "JPY" and v > 5 or bool(re.search(r"per\s*100|\b100\s*" + ccy, line, re.I))
        if per100:
            v = v / 100
        if v <= 0 or (ccy not in ("JPY",) and v < Decimal("0.5")):
            out.skipped.append(f"implausible: {line[:70]}")
            continue
        if (ccy, d) in seen:
            continue
        seen.add((ccy, d))
        out.rates.append((ccy, d, f"{v:.6f}"))
    return out


def fetch_feed(url: str | None = None, transport: Callable[[str], bytes] | None = None) -> tuple[bytes, str]:
    url = url or settings.fx_feed_url
    if not url:
        raise ValueError("FX_FEED_URL is not configured. Set it to the live FBIL reference-rate publication, or import a downloaded file.")
    if not url.startswith("https://"):
        raise ValueError("FX_FEED_URL must be https://")
    check_egress(url)
    if transport:
        return transport(url), url.rsplit("/", 1)[-1]
    import httpx

    r = httpx.get(url, timeout=30, follow_redirects=False, headers={"User-Agent": "cash-application/0.1 (+fx-feed)"})
    r.raise_for_status()
    if len(r.content) > 5 * 1024 * 1024:
        raise ValueError("feed response too large")
    return r.content, url.rsplit("/", 1)[-1] or "feed"


def store_rates(db: Session, parsed: Parsed, source: str, actor: str, actor_id: int | None = None, force: bool = False) -> dict:
    stored, rejected = 0, []
    for ccy, d, rate in sorted(parsed.rates, key=lambda r: (r[0], r[1])):
        micro = parse_rate(rate)
        if not (1_000 <= micro <= 100_000_000_000):
            rejected.append(f"{ccy} {d}: {rate} outside plausible range")
            continue
        prev = db.execute(select(ExchangeRate).where(ExchangeRate.currency == ccy, ExchangeRate.rate_date < d).order_by(ExchangeRate.rate_date.desc()).limit(1)).scalars().first()
        if prev and not force and abs(Decimal(micro) - prev.rate_micro) / prev.rate_micro > MAX_MOVE:
            rejected.append(f"{ccy} {d}: {rate} moves {abs(micro - prev.rate_micro) * 100 / prev.rate_micro:.1f}% from {prev.rate_micro / MICRO:.4f} on {prev.rate_date} — held for review (check units / file)")
            continue
        row = db.execute(select(ExchangeRate).where(ExchangeRate.currency == ccy, ExchangeRate.rate_date == d)).scalar_one_or_none()
        if row:
            row.rate_micro, row.source = micro, source
        else:
            db.add(ExchangeRate(currency=ccy, rate_date=d, rate_micro=micro, source=source))
        stored += 1
    db.flush()
    audit.log(db, actor, "fx.feed_imported", "ExchangeRate", "-", None, {"source": source, "stored": stored, "rejected": len(rejected), "skipped": len(parsed.skipped)}, actor_id)
    return {"stored": stored, "rejected": rejected, "skipped": parsed.skipped[:20], "parsed": len(parsed.rates)}


def parse_ecb(data: bytes) -> tuple[date, dict[str, Decimal]]:
    """ECB daily euro reference rates (eurofxref-daily.xml): <Cube time='2026-10-16'><Cube currency='USD' rate='1.0856'/>...  Units of CCY per 1 EUR."""
    from defusedxml import ElementTree as ET
    from defusedxml.common import DefusedXmlException

    try:
        root = ET.fromstring(data)
    except (ET.ParseError, DefusedXmlException) as e:
        raise ValueError(f"Invalid cross-rate XML: {e}")
    when, rates = None, {}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "Cube" and el.get("time"):
            when = date.fromisoformat(el.get("time"))
        elif tag == "Cube" and el.get("currency") and el.get("rate"):
            try:
                v = Decimal(el.get("rate"))
            except InvalidOperation:
                continue
            if v > 0:
                rates[el.get("currency").upper()] = v
    if when is None or not rates:
        raise ValueError("Cross-rate feed has no dated rates (layout changed?)")
    return when, rates


def derive_cross(eur_inr: Decimal, eur_date: date, ecb_date: date, ecb: dict[str, Decimal], wanted: tuple[str, ...], fbil_usd: Decimal | None = None) -> Parsed:
    """INR per 1 CCY = (INR per EUR) / (CCY per EUR). Indicative only: the two sources fix at different times of day, so derived rates carry
    a few tenths of a percent of timing skew. Refused if the dates are far apart or if the same method misprices USD against FBIL's own USD."""
    out = Parsed()
    if abs((eur_date - ecb_date).days) > 3:
        out.skipped.append(f"cross-rate date {ecb_date} is too far from the FBIL date {eur_date}")
        return out
    if fbil_usd is not None and "USD" in ecb:
        chk = eur_inr / ecb["USD"]
        if abs(chk - fbil_usd) / fbil_usd > Decimal("0.02"):
            out.skipped.append(f"cross-check failed: derived USD/INR {chk:.4f} vs FBIL {fbil_usd:.4f} differ by more than 2% — derived rates withheld")
            return out
    for c in wanted:
        if c in FBIL_DIRECT or c == "INR" or c not in ecb:
            continue
        out.rates.append((c, eur_date, f"{eur_inr / ecb[c]:.6f}"))
    return out


def pegged() -> dict[str, Decimal]:
    out = {}
    for item in settings.fx_pegs:
        try:
            c, v = item.split(":")
            d = Decimal(v)
            if d > 0:
                out[c.strip().upper()] = d
        except (ValueError, InvalidOperation):
            continue
    return out


def derive_pegged(usd_inr: Decimal, usd_date: date, wanted: tuple[str, ...]) -> Parsed:
    """INR per 1 CCY = (INR per USD) / (CCY per USD peg). Exact as long as the peg holds; it is configuration, not a market quote."""
    out = Parsed()
    for c, peg in pegged().items():
        if c in wanted and c not in FBIL_DIRECT:
            out.rates.append((c, usd_date, f"{usd_inr / peg:.6f}"))
    return out


def run_feed(db: Session, actor: str, actor_id: int | None = None, transport=None, url: str | None = None, cross_transport=None) -> dict:
    data, name = fetch_feed(url, transport)
    parsed = parse_reference_rates(data, name, as_of=date.today(), wanted=settings.fx_feed_currencies)
    if not parsed.rates:
        raise ValueError("The feed was fetched but no recognisable rates were found (layout changed?). Nothing stored.")
    result = store_rates(db, parsed, "fbil_reference", actor, actor_id)
    pegs = pegged()
    usd_row = max((r for r in parsed.rates if r[0] == "USD"), key=lambda r: r[1], default=None)
    result["pegged"] = {"requested": [c for c in settings.fx_feed_currencies if c in pegs], "stored": 0, "note": None}
    if result["pegged"]["requested"]:
        if usd_row is None:
            result["pegged"]["note"] = "FBIL feed carried no USD rate; cannot derive pegged currencies"
        else:
            pr = derive_pegged(Decimal(usd_row[2]), usd_row[1], settings.fx_feed_currencies)
            result["pegged"]["stored"] = store_rates(db, pr, "derived_peg_usd", actor, actor_id)["stored"]
    need = [c for c in settings.fx_feed_currencies if c not in FBIL_DIRECT and c not in pegs and c != "INR"]
    result["derived"] = {"requested": need, "stored": 0, "note": None}
    if need and settings.fx_cross_url:
        try:
            raw, _ = fetch_feed(settings.fx_cross_url, cross_transport)
            ecb_date, ecb = parse_ecb(raw)
            eur = max((r for r in parsed.rates if r[0] == "EUR"), key=lambda r: r[1], default=None)
            usd = next((Decimal(r[2]) for r in parsed.rates if r[0] == "USD" and eur and r[1] == eur[1]), None)
            if eur is None:
                result["derived"]["note"] = "FBIL feed carried no EUR rate; cannot derive cross rates"
            else:
                d = derive_cross(Decimal(eur[2]), eur[1], ecb_date, ecb, tuple(need), usd)
                if d.rates:
                    result["derived"]["stored"] = store_rates(db, d, "derived_fbil_ecb", actor, actor_id)["stored"]
                result["derived"]["note"] = "; ".join(d.skipped) or None
                result["derived"]["missing_in_source"] = [c for c in need if c not in ecb]
        except Exception as e:      # a cross-rate failure must never undo the official rates that were already stored
            result["derived"]["note"] = f"cross-rate step failed: {e}"
    elif need:
        result["derived"]["note"] = "FX_CROSS_FEED_URL not configured: " + ", ".join(need) + " are not published by FBIL and need manual entry or a cross-rate source"
    return result


def stale_currencies(db: Session, entity_id: int, today: date | None = None, max_age_days: int = 5) -> list[dict]:
    """Currencies with open foreign invoices whose newest rate is older than max_age_days (covers weekends and a holiday)."""
    today = today or date.today()
    from .models import LegalEntity

    base = db.get(LegalEntity, entity_id).base_currency
    ccys = {c for (c,) in db.execute(select(Invoice.currency).where(Invoice.entity_id == entity_id, Invoice.open_minor != 0, Invoice.currency != base).distinct())}
    out = []
    for c in sorted(ccys):
        latest = db.execute(select(ExchangeRate.rate_date).where(ExchangeRate.currency == c).order_by(ExchangeRate.rate_date.desc()).limit(1)).scalar()
        if latest is None or (today - latest).days > max_age_days:
            out.append({"currency": c, "latest": latest.isoformat() if latest else None, "age_days": (today - latest).days if latest else None})
    return out
