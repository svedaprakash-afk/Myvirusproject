"""FBIL layouts below are written from SECONDARY descriptions (date, currency pair, rate; JPY per 100). Unverified against the live site."""
import dataclasses
import io
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import config, fx, fxfeed, jobs
from app.models import ExchangeRate, JobRun
from conftest import login

CSV = b"""Date,Time,Currency Pair,Rate,Comments
16/10/2026,13:00,USD/INR,83.4521,
16/10/2026,13:00,GBP/INR,108.1234,
16/10/2026,13:00,EUR/INR,90.5678,
16/10/2026,13:00,JPY/INR(100),55.1234,per 100 JPY
"""
HTML = b"<html><table><tr><th>Date</th><th>Currency</th><th>Rate</th></tr><tr><td>16-Oct-2026</td><td>USD</td><td>83.4521</td></tr><tr><td>16-Oct-2026</td><td>EUR</td><td>90.5678</td></tr></table></html>"
TXT = b"Reference Rates 2026-10-16\nUSD/INR 83.4521\nEUR/INR 90.5678\nJPY/INR 55.12 (per 100 yen)\nnothing here\n"


def rates(p):
    return {(c, d.isoformat()): r for c, d, r in p.rates}


def test_parses_csv_pairs_and_converts_jpy_per_100():
    r = rates(fxfeed.parse_reference_rates(CSV, "x.csv"))
    assert r[("USD", "2026-10-16")] == "83.452100" and r[("GBP", "2026-10-16")] == "108.123400" and r[("JPY", "2026-10-16")] == "0.551234"


def test_parses_html_table_and_plain_text_with_date_from_header_or_fallback():
    h = rates(fxfeed.parse_reference_rates(HTML, "x.html"))
    assert h == {("USD", "2026-10-16"): "83.452100", ("EUR", "2026-10-16"): "90.567800"}
    t = fxfeed.parse_reference_rates(TXT, "x.txt", as_of=date(2026, 10, 16))
    assert rates(t)[("JPY", "2026-10-16")] == "0.551200" and rates(t)[("USD", "2026-10-16")] == "83.452100"


def test_parses_xlsx():
    from openpyxl import Workbook

    wb = Workbook(); ws = wb.active
    ws.append(["Date", "Currency Pair", "Rate"]); ws.append([datetime(2026, 10, 16), "USD/INR", 83.4521])
    buf = io.BytesIO(); wb.save(buf)
    assert rates(fxfeed.parse_reference_rates(buf.getvalue(), "x.xlsx")) == {("USD", "2026-10-16"): "83.452100"}


def test_unrecognisable_input_yields_nothing_rather_than_garbage():
    p = fxfeed.parse_reference_rates(b"hello world\n12.5\nUSD with no number or date", "x.txt")
    assert p.rates == [] and p.skipped


def test_wanted_currencies_filter():
    assert {c for c, *_ in fxfeed.parse_reference_rates(CSV, "x.csv", wanted=("USD",)).rates} == {"USD"}


def test_circuit_breaker_holds_implausible_moves_and_force_overrides(db):
    db.add(ExchangeRate(currency="USD", rate_date=date(2026, 10, 15), rate_micro=fx.parse_rate("83.40")))
    db.flush()
    bad = fxfeed.Parsed(rates=[("USD", date(2026, 10, 16), "92.0000")])      # +10%: a wrong unit or a corrupt file, not a market move
    res = fxfeed.store_rates(db, bad, "t", "t")
    assert res["stored"] == 0 and "held for review" in res["rejected"][0]
    assert db.execute(select(ExchangeRate).where(ExchangeRate.rate_date == date(2026, 10, 16))).first() is None
    assert fxfeed.store_rates(db, bad, "t", "t", force=True)["stored"] == 1
    ok = fxfeed.store_rates(db, fxfeed.Parsed(rates=[("USD", date(2026, 10, 17), "92.5000")]), "t", "t")
    assert ok["stored"] == 1
    fxfeed.store_rates(db, fxfeed.Parsed(rates=[("USD", date(2026, 10, 17), "92.6000")]), "t", "t")      # same day re-published: upsert, not duplicate
    assert db.query(ExchangeRate).filter_by(currency="USD", rate_date=date(2026, 10, 17)).count() == 1


def test_run_feed_requires_configuration_https_and_residency(monkeypatch, db):
    with pytest.raises(ValueError, match="not configured"):
        fxfeed.run_feed(db, "t")
    monkeypatch.setattr(fxfeed, "settings", dataclasses.replace(config.settings, fx_feed_url="http://insecure.example/x"))
    with pytest.raises(ValueError, match="https"):
        fxfeed.run_feed(db, "t")
    monkeypatch.setattr(fxfeed, "settings", dataclasses.replace(config.settings, fx_feed_url="https://www.fbil.org.in/rates", fx_feed_currencies=("USD", "EUR", "GBP", "JPY")))
    out = fxfeed.run_feed(db, "t", transport=lambda u: CSV)
    assert out["stored"] == 4 and out["rejected"] == []
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, residency_strict=True, deploy_region="ap-south-1", allowed_egress=("erp.example.in",)))
    with pytest.raises(config.EgressBlocked):
        fxfeed.run_feed(db, "t", transport=lambda u: CSV)


def test_empty_or_changed_layout_stores_nothing(monkeypatch, db):
    monkeypatch.setattr(fxfeed, "settings", dataclasses.replace(config.settings, fx_feed_url="https://www.fbil.org.in/rates"))
    with pytest.raises(ValueError, match="no recognisable rates"):
        fxfeed.run_feed(db, "t", transport=lambda u: b"<html>site redesigned</html>")
    assert db.query(ExchangeRate).count() == 0


def test_stale_rates_are_reported_per_currency(w):
    from conftest import TODAY

    c = w.customer("X1", "Globex")
    w.invoice(c, "EXP/1", 1_000_00, gst_pct=0, currency="USD", booking_rate=83_000_000)
    assert fxfeed.stale_currencies(w.db, w.e.id, TODAY)[0] == {"currency": "USD", "latest": None, "age_days": None}
    w.db.add(ExchangeRate(currency="USD", rate_date=TODAY - timedelta(days=9), rate_micro=83_000_000))
    w.db.flush()
    assert fxfeed.stale_currencies(w.db, w.e.id, TODAY)[0]["age_days"] == 9
    w.db.add(ExchangeRate(currency="USD", rate_date=TODAY - timedelta(days=1), rate_micro=83_100_000))
    w.db.flush()
    assert fxfeed.stale_currencies(w.db, w.e.id, TODAY) == []


# ------------------------------------------------------------------------------------------------ jobs
def factory(db):
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False)


def utc_for_ist(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi) - jobs.IST


def test_fx_job_respects_ist_window_weekdays_and_runs_once_per_day(db, monkeypatch):
    monkeypatch.setattr(fxfeed, "settings", dataclasses.replace(config.settings, fx_feed_url="https://www.fbil.org.in/rates", fx_feed_currencies=("USD",)))
    db.commit()
    F, tx = factory(db), (lambda u: CSV)
    assert jobs.run_once(F, utc_for_ist(2026, 10, 16, 12), fx_transport=tx) == []                 # Fri 12:00 IST: before publication
    assert jobs.run_once(F, utc_for_ist(2026, 10, 17, 15), fx_transport=tx) == []                 # Saturday: no publication
    r = jobs.run_once(F, utc_for_ist(2026, 10, 16, 14, 5), fx_transport=tx)
    assert r == [{"job": "fx_fetch", "status": "ok"}]
    assert jobs.run_once(F, utc_for_ist(2026, 10, 16, 15, 5), fx_transport=tx) == [], "already done today"
    assert db.query(ExchangeRate).filter_by(currency="USD").count() == 1


def test_fx_job_retries_after_failure_then_succeeds_and_caps_attempts(db, monkeypatch):
    monkeypatch.setattr(fxfeed, "settings", dataclasses.replace(config.settings, fx_feed_url="https://www.fbil.org.in/rates", fx_feed_currencies=("USD",)))
    db.commit()
    F = factory(db)
    calls = {"n": 0}

    def flaky(u):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("site down")
        return CSV

    out = [jobs.run_once(F, utc_for_ist(2026, 10, 16, 14, m), fx_transport=flaky) for m in (1, 2, 3, 4)]
    assert [o[0]["status"] if o else None for o in out] == ["failed", "failed", "ok", None]
    db2 = F()
    assert db2.query(JobRun).filter(JobRun.name == "fx_fetch", JobRun.status == "failed").count() == 2
    db2.close()
    always = lambda u: (_ for _ in ()).throw(RuntimeError("down"))
    n = sum(len(jobs.run_once(F, utc_for_ist(2026, 10, 19, 14, m), fx_transport=always)) for m in range(20))
    assert n == jobs.FX_MAX_ATTEMPTS, "retries are bounded; a persistently failing source is surfaced, not hammered"


def test_slot_claim_is_exclusive(db):
    db.commit()
    F = factory(db)
    a, b = F(), F()
    assert jobs._claim(a, "x", "slot1") is not None
    assert jobs._claim(b, "x", "slot1") is None, "second worker cannot take the same slot"
    assert jobs._claim(b, "x", "slot2") is not None
    a.close(); b.close()


def test_worker_polls_mailbox_and_syncs_feed_then_matches_once_per_interval(w, monkeypatch, tmp_path):
    from app.ingestion import feeds
    from test_integrations import FakeIMAP, mail

    c = w.customer("C1", "Acme Industries Pvt Ltd")
    w.invoice(c, "INV/26-27/0101", 100_000_00)
    monkeypatch.setenv("MB_PW", "pw")
    w.e.config = {**w.e.config, "integrations": {"mailbox": {"host": "imap.example.in", "username": "ar@us.example", "password_env": "MB_PW", "poll_minutes": 10}}}
    root = tmp_path / "feeds"; (root / "b").mkdir(parents=True)
    (root / "b" / "s.csv").write_bytes(b"Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n01/10/26,NEFT-HDFCN26274000111-ACME INDUSTRIES PVT LTD-INV/26-27/0101,,\"1,18,000.00\",5000.00\n")
    monkeypatch.setattr(feeds, "settings", dataclasses.replace(config.settings, feed_root=str(root)))
    w.bank.feed = {"type": "folder_drop", "path": "b", "interval_minutes": 30}
    w.db.commit()
    F = factory(w.db)
    fake = FakeIMAP([mail(mid="<j1@x>")])
    now = datetime(2026, 10, 16, 8, 0)
    r1 = jobs.run_once(F, now, imap_factory=lambda h, p: fake)
    names = {x["job"].split(":")[0] for x in r1}
    assert {"mailbox", "bank_sync", "match"} <= names and all(x["status"] == "ok" for x in r1)
    s = F()
    from app.models import MatchProposal, PaymentTransaction
    assert s.query(PaymentTransaction).count() == 1 and s.query(MatchProposal).count() == 1
    s.close()
    assert jobs.run_once(F, now + timedelta(minutes=3), imap_factory=lambda h, p: fake) == [], "same intervals: nothing re-runs"
    assert {x["job"].split(":")[0] for x in jobs.run_once(F, now + timedelta(minutes=11), imap_factory=lambda h, p: fake)} == {"mailbox"}


def test_failing_job_is_recorded_and_does_not_stop_others(w, monkeypatch):
    monkeypatch.setenv("MB_PW_MISSING_X", "")
    monkeypatch.delenv("MB_PW_MISSING_X")
    w.e.config = {**w.e.config, "integrations": {"mailbox": {"host": "imap.example.in", "username": "u", "password_env": "MB_PW_MISSING_X"}}}
    w.db.commit()
    F = factory(w.db)
    r = jobs.run_once(F, datetime(2026, 10, 16, 8, 0))
    assert r and r[0]["status"] == "failed"
    s = F()
    assert "not set" in s.query(JobRun).filter(JobRun.name.like("mailbox:%")).one().detail["error"]
    s.close()


# ------------------------------------------------------------------------------------------------ API
def test_fx_api_import_fetch_and_status(client, db, monkeypatch):
    from app.seed.demo import DEMO_PASSWORD, seed_demo

    seed_demo(db, run_matching=False)
    db.commit()
    tr, an = login(client, "treasury@meridian.example", DEMO_PASSWORD), login(client, "analyst@meridian.example", DEMO_PASSWORD)
    st = client.get("/api/v1/fx-rates/feed", headers=an).json()
    assert st["configured"] is False and "FBIL" in st["source"]
    assert client.post("/api/v1/fx-rates/fetch", headers=tr).status_code == 409          # not configured: clear message, no crash
    assert client.post("/api/v1/fx-rates/import", files={"file": ("r.csv", CSV, "text/csv")}, headers=an).status_code == 403
    r = client.post("/api/v1/fx-rates/import", data={"as_of": "2026-10-16"}, files={"file": ("r.csv", CSV, "text/csv")}, headers=tr)
    assert r.status_code == 200 and r.json()["stored"] >= 1
    assert client.post("/api/v1/fx-rates/import", files={"file": ("r.txt", b"garbage", "text/plain")}, headers=tr).status_code == 422
    assert client.get("/api/v1/jobs", headers=an).status_code == 200
    assert client.get("/health/ready").json() == {"ready": True}


def test_dashboard_reports_stale_rates(client, db):
    from app.models import ExchangeRate
    from sqlalchemy import delete
    from app.seed.demo import DEMO_PASSWORD, seed_demo

    e = seed_demo(db, run_matching=False)["entity_id"]
    db.execute(delete(ExchangeRate).where(ExchangeRate.rate_date > date.today() - timedelta(days=20)))
    db.commit()
    s = client.get(f"/api/v1/analytics/summary?entity_id={e}", headers=login(client, "treasury@meridian.example", DEMO_PASSWORD)).json()
    assert {x["currency"] for x in s["stale_rates"]} == {"USD", "EUR"}


# ------------------------------------------------------------------------------------------------ top-10 currencies (derived cross rates)
ECB = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
<gesmes:subject>Reference rates</gesmes:subject><Cube><Cube time="2026-10-16">
<Cube currency="USD" rate="1.0855"/><Cube currency="JPY" rate="164.20"/><Cube currency="GBP" rate="0.8380"/><Cube currency="CNY" rate="7.8100"/>
<Cube currency="AUD" rate="1.6620"/><Cube currency="CAD" rate="1.4980"/><Cube currency="CHF" rate="0.9640"/><Cube currency="HKD" rate="8.4500"/><Cube currency="SGD" rate="1.4540"/>
</Cube></Cube></gesmes:Envelope>"""
FBIL4 = b"""Date,Currency Pair,Rate
16/10/2026,USD/INR,83.4500
16/10/2026,EUR/INR,90.6000
16/10/2026,GBP/INR,108.1000
16/10/2026,JPY/INR,55.1000
"""


def test_top_ten_set_is_defined_and_fbil_covers_only_four():
    assert len(fxfeed.TOP10) == 10 and set(fxfeed.FBIL_DIRECT) < set(fxfeed.TOP10)
    assert set(config.settings.fx_feed_currencies) == set(fxfeed.TOP10)


def test_ecb_parser_reads_namespaced_xml_and_rejects_hostile_xml():
    d, r = fxfeed.parse_ecb(ECB)
    assert d == date(2026, 10, 16) and r["USD"] == fxfeed.Decimal("1.0855") and len(r) == 9
    with pytest.raises(ValueError):
        fxfeed.parse_ecb(b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]><Cube time="2026-10-16"><Cube currency="USD" rate="&x;"/></Cube>')
    with pytest.raises(ValueError, match="no dated rates"):
        fxfeed.parse_ecb(b"<root/>")


def test_cross_rates_are_eur_inr_over_ecb_and_skip_directly_published_currencies():
    _, ecb = fxfeed.parse_ecb(ECB)
    d = fxfeed.derive_cross(fxfeed.Decimal("90.6"), date(2026, 10, 16), date(2026, 10, 16), ecb, fxfeed.TOP10, fxfeed.Decimal("83.45"))
    got = {c: r for c, _, r in d.rates}
    assert set(got) == {"CNY", "AUD", "CAD", "CHF", "HKD", "SGD"}, "USD/EUR/GBP/JPY come from FBIL itself, never derived"
    assert got["SGD"] == f"{fxfeed.Decimal('90.6') / fxfeed.Decimal('1.4540'):.6f}" and abs(float(got["CNY"]) - 11.6) < 0.05


def test_cross_check_withholds_all_derived_rates_when_the_method_misprices_usd():
    _, ecb = fxfeed.parse_ecb(ECB)
    bad = fxfeed.derive_cross(fxfeed.Decimal("90.6"), date(2026, 10, 16), date(2026, 10, 16), ecb, fxfeed.TOP10, fxfeed.Decimal("80.00"))   # 4% off FBIL USD
    assert bad.rates == [] and "cross-check failed" in bad.skipped[0]
    stale = fxfeed.derive_cross(fxfeed.Decimal("90.6"), date(2026, 10, 16), date(2026, 10, 9), ecb, fxfeed.TOP10)
    assert stale.rates == [] and "too far" in stale.skipped[0]


def _cfg(monkeypatch, cross=True):
    s = dataclasses.replace(config.settings, fx_feed_url="https://www.fbil.org.in/rates", fx_cross_url="https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml" if cross else "",
                            fx_feed_currencies=fxfeed.TOP10)
    monkeypatch.setattr(fxfeed, "settings", s)
    monkeypatch.setattr(config, "settings", s)


def test_run_feed_stores_four_official_and_six_derived_with_distinct_sources(db, monkeypatch):
    _cfg(monkeypatch)
    out = fxfeed.run_feed(db, "t", transport=lambda u: FBIL4, cross_transport=lambda u: ECB)
    assert out["stored"] == 4 and out["derived"]["stored"] == 6 and out["derived"]["note"] is None
    src = {r.currency: r.source for r in db.query(ExchangeRate).all()}
    assert {c for c, s in src.items() if s == "fbil_reference"} == {"USD", "EUR", "GBP", "JPY"}
    assert {c for c, s in src.items() if s == "derived_fbil_ecb"} == {"CNY", "AUD", "CAD", "CHF", "HKD", "SGD"}


def test_without_a_cross_source_derived_currencies_are_reported_missing_not_invented(db, monkeypatch):
    _cfg(monkeypatch, cross=False)
    out = fxfeed.run_feed(db, "t", transport=lambda u: FBIL4)
    assert out["stored"] == 4 and out["derived"]["stored"] == 0 and "CNY" in out["derived"]["note"] and "manual entry" in out["derived"]["note"]
    assert db.query(ExchangeRate).filter(ExchangeRate.currency == "CNY").count() == 0


def test_cross_rate_failure_never_undoes_official_rates(db, monkeypatch):
    _cfg(monkeypatch)
    out = fxfeed.run_feed(db, "t", transport=lambda u: FBIL4, cross_transport=lambda u: b"<html>down</html>")
    assert out["stored"] == 4 and out["derived"]["stored"] == 0 and "cross-rate step failed" in out["derived"]["note"]


def test_cross_host_must_be_allow_listed_in_strict_mode(db, monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, residency_strict=True, deploy_region="ap-south-1", allowed_egress=("www.fbil.org.in",)))
    out = fxfeed.run_feed(db, "t", transport=lambda u: FBIL4, cross_transport=lambda u: ECB)
    assert out["stored"] == 4 and out["derived"]["stored"] == 0 and "blocked" in out["derived"]["note"]
