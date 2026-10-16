import dataclasses
import hashlib
import hmac
import json
import os
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from app import config
from app.indian import parse_narration, validate_rules
from app.ingestion import feeds, mailbox, service as ing
from app.ingestion.remittance import LLMExtractor, get_extractor
from app.ingestion.statements import StatementError
from app.models import BatchStatus, CustomerIdentifier, PaymentTransaction, RemittanceDocument
from app.posting import mapped, service as posting
from conftest import login

# ---------------------------------------------------------------------------------------------- bank-agnostic: profiles
ODD_BANK = (b"Some Bank Ltd;Statement\n;;;;\nPosting Dt;Particulars;Debit;Credit;Bal\n"
            b"05.10.2026;TXN9981*PAYER:ACME INDUSTRIES*REF:INV/26-27/0101;;1,18,000.00;5,00,000.00\n"
            b"06.10.2026;SERVICE FEE;250.00;;4,99,750.00\n")
ODD_PROFILE = {"format": "csv", "delimiter": ";", "skip_rows": 1, "date_format": "%d.%m.%Y",
               "columns": {"date": "Posting Dt", "narration": "Particulars", "debit": "Debit", "credit": "Credit", "balance": "Bal"},
               "narration_rules": [{"name": "somebank", "pattern": r"TXN(?P<utr>\d+)\*PAYER:(?P<payer>[^*]+)\*REF:(?P<ref>\S+)", "channel": "NEFT", "utr": "utr", "payer": "payer"}]}


def test_unknown_bank_is_onboarded_by_configuration_only(w):
    w.bank.profile = ODD_PROFILE
    out = ing.ingest_statement(w.db, w.bank, "s.csv", ODD_BANK, "t", None)
    assert out["inserted"] == 2 and out["credits"] == 1
    t = w.db.execute(select(PaymentTransaction).where(PaymentTransaction.direction == "credit")).scalar_one()
    assert (t.utr, t.payer_name, t.channel, t.amount_minor) == ("9981", "ACME INDUSTRIES", "NEFT", 11800000) and t.extra["rule"] == "somebank"


def test_same_file_without_profile_fails_clearly_instead_of_guessing(w):
    w.bank.profile = {}
    with pytest.raises(StatementError):
        ing.ingest_statement(w.db, w.bank, "s.csv", ODD_BANK, "t", None)


def test_preview_is_a_dry_run_with_quality_metrics(w):
    before = w.db.query(PaymentTransaction).count()
    pv = ing.preview_statement(w.bank, "s.csv", ODD_BANK, ODD_PROFILE)
    assert w.db.query(PaymentTransaction).count() == before
    assert pv["rows_total"] == 2 and pv["utr_found_pct"] == 100.0 and pv["sample"][0]["parsed"]["payer"] == "ACME INDUSTRIES"


def test_suggest_profile_reports_recognised_and_unrecognised_headers():
    from app.ingestion.statements import suggest_profile

    s = suggest_profile(b"junk\nTxn Date,Particulars,Withdrawal,Deposit,Weird Col\n01/10/26,x,1,,z\n", "x.csv")
    assert s["header_row"] == 1 and "Weird Col" in s["unrecognised_headers"] and s["profile"]["columns"]["narration"] == "Particulars"


def test_narration_rule_validation_rejects_unsafe_and_malformed():
    assert validate_rules([{"pattern": r"UTR(?P<u>\d+)", "utr": "u"}]) == []
    assert any("nested" in e for e in validate_rules([{"pattern": r"(a+)+$"}]))
    assert any("invalid regex" in e for e in validate_rules([{"pattern": "(unclosed"}]))
    assert any("does not define" in e for e in validate_rules([{"pattern": r"(?P<x>\d+)", "utr": "nope"}]))
    assert any("at most" in e for e in validate_rules([{"pattern": "a"}] * 31))
    assert parse_narration("zzz", [{"pattern": "(unclosed"}]).utr is None   # a bad rule can never break parsing


def test_profile_api_is_validated_and_role_gated(client, db):
    from app.seed.demo import DEMO_PASSWORD, seed_demo

    e = seed_demo(db, run_matching=False)["entity_id"]
    db.commit()
    an = login(client, "analyst@meridian.example", DEMO_PASSWORD)
    bid = client.get(f"/api/v1/entities/{e}/bank-accounts", headers=an).json()[0]["id"]
    bad = client.put(f"/api/v1/bank-accounts/{bid}/profile", json={"profile": {"bogus": 1, "date_format": "%Q%Q"}}, headers=an)
    assert bad.status_code == 422 and "unknown key" in bad.text
    assert client.put(f"/api/v1/bank-accounts/{bid}/profile", json={"profile": ODD_PROFILE}, headers=an).status_code == 200
    assert client.put(f"/api/v1/bank-accounts/{bid}/feed", json={"feed": {"type": "folder_drop", "path": "x"}}, headers=an).status_code == 403   # feed = configure only
    r = client.post("/api/v1/ingest/preview", data={"bank_account_id": bid}, files={"file": ("s.csv", ODD_BANK, "text/csv")}, headers=an)
    assert r.status_code == 200 and r.json()["sample"][0]["parsed"]["utr"] == "9981"


# ---------------------------------------------------------------------------------------------- bank feeds
def test_folder_drop_ingests_archives_and_cannot_escape_root(w, tmp_path, monkeypatch):
    root = tmp_path / "feeds"
    (root / "hdfc").mkdir(parents=True)
    monkeypatch.setattr(feeds, "settings", dataclasses.replace(config.settings, feed_root=str(root)))
    good = (b"Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n01/10/26,NEFT-HDFCN26274000111-ACME-INV 1,,1000.00,5000.00\n")
    (root / "hdfc" / "a.csv").write_bytes(good)
    (root / "hdfc" / "broken.csv").write_bytes(b"not,a,statement\n1,2,3\n")
    w.bank.feed = {"type": "folder_drop", "path": "hdfc"}
    res = feeds.sync_bank(w.db, w.bank, "t", None)
    assert res["inserted"] == 1 and len(res["errors"]) == 1
    assert (root / "hdfc" / "broken.csv").exists(), "failed files stay in place so the profile can be fixed"
    assert not (root / "hdfc" / "a.csv").exists() and len(list((root / "hdfc" / "processed").iterdir())) == 1
    w.bank.feed = {"type": "folder_drop", "path": "../../etc"}
    with pytest.raises(feeds.FeedError, match="escapes"):
        feeds.sync_bank(w.db, w.bank, "t", None)


def test_rest_json_feed_maps_any_bank_api_shape(w, monkeypatch):
    monkeypatch.setenv("BANK_TOKEN", "s3cret")
    seen = {}

    def transport(url, headers, params):
        seen.update(headers=headers, params=params)
        return {"data": {"items": [{"d": "2026-10-05", "amt": "118000.00", "cr": "CR", "txt": "NEFT-HDFCN26275000555-ACME INDUSTRIES-INV 5", "bal": "500000.00", "u": "HDFCN26275000555"},
                                   {"d": "2026-10-06", "amt": "250.00", "cr": "DR", "txt": "FEE"}]}}

    w.bank.feed = {"type": "rest_json", "url": "https://api.example-bank.in/stmt", "records_path": "data.items", "auth": {"token_env": "BANK_TOKEN"},
                   "params": {"from": "{since}"}, "map": {"date": "d", "amount": "amt", "direction": "cr", "narration": "txt", "balance": "bal", "ref": "u"}}
    out = feeds.sync_bank(w.db, w.bank, "t", None, transport)
    assert out["inserted"] == 2 and seen["headers"]["Authorization"] == "Bearer s3cret" and "from" in seen["params"]
    t = w.db.execute(select(PaymentTransaction).where(PaymentTransaction.direction == "credit")).scalar_one()
    assert t.utr == "HDFCN26275000555" and t.amount_minor == 11800000


def test_feed_validation_refuses_planned_and_insecure():
    assert feeds.validate_feed({"type": "sftp_native"})[0].startswith("feed type 'sftp_native' is planned")
    assert any("https" in e for e in feeds.validate_feed({"type": "rest_json", "url": "http://x", "records_path": "a", "map": {"date": "d", "amount": "a", "narration": "n"}}))
    assert feeds.validate_feed({"type": "nonsense"})


# ---------------------------------------------------------------------------------------------- ERP-agnostic export
def _batch(w):
    from test_workflow import applied_world

    applied_world(w)
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    posting.release_batch(w.db, b, w.users["treasury"])
    return b


def _set_export(w, cfg):
    w.e.config = {**w.e.config, "integrations": {"erp_export": cfg}}


def test_mapped_csv_matches_an_arbitrary_erp_import_layout(w):
    _set_export(w, {"type": "mapped_csv", "delimiter": ";", "decimal": ",", "date_format": "%Y%m%d", "amounts": "base", "account_map": {"1100-HDFC": "BANK-001"},
                    "columns": [{"header": "DocNo", "source": "voucher_no"}, {"header": "PostDate", "source": "date"}, {"header": "GL", "source": "account"},
                                {"header": "D/C", "source": "drcr"}, {"header": "Amt", "source": "amount"}, {"header": "Company", "const": "1000"}, {"header": "Text", "source": "narration"}]})
    b = _batch(w)
    name, content, _ = posting.export_batch(w.db, b, "mapped_csv", w.users["treasury"])
    rows = content.decode().splitlines()
    assert rows[0] == "DocNo;PostDate;GL;D/C;Amt;Company;Text" and "BANK-001" in content.decode()
    first = rows[1].split(";")
    assert first[1].isdigit() and len(first[1]) == 8 and "," in first[4] and first[5] == "1000"


def test_mapped_csv_config_validation():
    assert mapped.validate_export({"type": "mapped_csv", "columns": [{"header": "x", "source": "nope"}]})
    assert mapped.validate_export({"type": "mapped_csv"})
    assert mapped.validate_export({"type": "webhook", "url": "http://x", "secret_env": "A"})
    assert mapped.validate_export({"type": "alien"})
    assert mapped.validate_export({}) == []


def test_webhook_signs_payload_acks_automatically_and_is_idempotent_keyed(w, monkeypatch):
    monkeypatch.setenv("ERP_SECRET", "topsecret")
    _set_export(w, {"type": "webhook", "url": "https://erp.example.in/hook", "secret_env": "ERP_SECRET"})
    b = _batch(w)
    sent = {}

    def transport(url, body, headers):
        sent.update(url=url, body=body, headers=headers)
        return 200, {"documents": {l.voucher_no: f"ERP-{i}" for i, l in enumerate({x.voucher_no: x for x in b.lines}.values())}}

    posting.export_batch(w.db, b, "webhook", w.users["treasury"], transport)
    expect = hmac.new(b"topsecret", sent["body"], hashlib.sha256).hexdigest()
    assert sent["headers"]["X-Signature"] == f"sha256={expect}" and sent["headers"]["Idempotency-Key"] == b.idempotency_key
    assert b.status == BatchStatus.acknowledged and all(l.erp_doc_no for l in b.lines)


def test_webhook_failure_marks_batch_failed_and_can_retry(w, monkeypatch):
    monkeypatch.setenv("ERP_SECRET", "x")
    _set_export(w, {"type": "webhook", "url": "https://erp.example.in/hook", "secret_env": "ERP_SECRET"})
    b = _batch(w)
    with pytest.raises(posting.PostingError, match="Delivery"):
        posting.export_batch(w.db, b, "webhook", w.users["treasury"], lambda u, body, h: (503, {}))
    assert b.status == BatchStatus.failed and "503" in b.error
    posting.export_batch(w.db, b, "webhook", w.users["treasury"], lambda u, body, h: (202, {}))   # accepted, no docs yet -> stays exported
    assert b.status == BatchStatus.exported


def test_unconfigured_connectors_are_refused(w):
    b = _batch(w)
    with pytest.raises(posting.PostingError, match="unconfigured"):
        posting.export_batch(w.db, b, "mapped_csv", w.users["treasury"])


def test_open_items_from_any_erp_layout_via_mapping(w):
    sap = ("Belegnr;Kunde;Kundenname;Belegdatum;Fälligkeit;Betrag;Offen;Währung;Kurs\n"
           "90001;K77;Acme Industries;31.08.2026;30.09.2026;1.000,00;1.000,00;USD;83,25\n"
           "90002;K77;Acme Industries;01.09.2026;01.10.2026;5000.00;5000.00;INR;\n").encode()
    mapping = {"delimiter": ";", "date_format": "%d.%m.%Y", "columns": {"invoice_no": "Belegnr", "customer_code": "Kunde", "customer_name": "Kundenname",
               "invoice_date": "Belegdatum", "due_date": "Fälligkeit", "total": "Betrag", "open": "Offen", "currency": "Währung", "booking_rate": "Kurs"}}
    rows = ing.csv_to_open_items(sap, mapping)
    rows[0]["total"] = rows[0]["open"] = "1000.00"      # (German number formats are the ERP export's concern; shown here as a normalised value)
    rows[0]["booking_rate"] = "83.25"
    out = ing.import_open_items(w.db, w.e, rows, "t", None)
    assert out["invoices_created"] == 2
    from app.models import Invoice

    usd = w.db.execute(select(Invoice).where(Invoice.number == "90001")).scalar_one()
    assert usd.currency == "USD" and usd.booking_rate_micro == 83_250_000 and usd.invoice_date.isoformat() == "2026-08-31"
    with pytest.raises(ValueError, match="not found"):
        ing.csv_to_open_items(sap, {**mapping, "columns": {"invoice_no": "Nope"}})


def test_foreign_invoice_without_any_rate_is_rejected_not_defaulted(w):
    out = ing.import_open_items(w.db, w.e, [{"invoice_no": "F1", "customer_code": "C", "customer_name": "C", "total": "100", "currency": "USD", "invoice_date": "2026-08-01"}], "t", None)
    assert out["rejected"] == 1 and "booking exchange rate" in out["warnings"][0]


# ---------------------------------------------------------------------------------------------- mailbox
def mail(frm="accounts@acme.example", mid="<m1@x>", auth="dkim=pass spf=pass dmarc=pass", body="From: Acme Industries\nUTR: HDFCN26274000111\nINV/26-27/0101  1,18,000.00\nTotal payment: 1,18,000.00",
         subject="Payment advice", html=False, attach=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = frm, "ar@us.example", subject, mid
    if auth:
        m["Authentication-Results"] = f"mx.us.example; {auth}"
    if html:
        m.set_content("see html")
        m.add_alternative(f"<html><body><table><tr><td>{body.replace(chr(10), '</td></tr><tr><td>')}</td></tr></table><script>evil()</script></body></html>", subtype="html")
    else:
        m.set_content(body)
    if attach:
        m.add_attachment(attach[1].encode(), maintype="text", subtype="csv", filename=attach[0])
    return m.as_bytes()


def test_authenticated_email_is_ingested_and_linked(w):
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    w.invoice(c, "INV/26-27/0101", 100_000_00)
    t = w.credit(11800000, "NEFT-HDFCN26274000111-ACME IND", utr="HDFCN26274000111")
    r = mailbox.process_email(w.db, w.e.id, mail(), {}, "t")
    assert r["status"] == "ingested" and r["linked_txn_id"] == t.id and r["lines"] == 1
    assert mailbox.process_email(w.db, w.e.id, mail(), {}, "t")["status"] == "duplicate"


def test_unauthenticated_or_off_list_email_is_quarantined_until_a_human_releases_it(w):
    spoof = mailbox.process_email(w.db, w.e.id, mail(auth="dkim=fail spf=fail dmarc=fail", mid="<m2@x>"), {}, "t")
    assert spoof["status"] == "quarantined" and "not authenticated" in spoof["reasons"][0]
    offlist = mailbox.process_email(w.db, w.e.id, mail(frm="x@evil.example", mid="<m3@x>"), {"allowed_domains": ["acme.example"]}, "t")
    assert offlist["status"] == "quarantined" and "allow-list" in offlist["reasons"][0]
    doc = w.db.get(RemittanceDocument, spoof["id"])
    assert doc.extractor == "quarantined" and doc.extracted == {"quarantined": True, "reasons": spoof["reasons"]} and doc.linked_txn_id is None
    mailbox.release_quarantined(w.db, doc, "analyst", None)
    assert doc.extractor == "rules" and doc.extracted["lines"]


def test_html_body_and_csv_attachment_are_extracted_and_scripts_dropped(w):
    raw = mail(mid="<m4@x>", html=True, body="INV/26-27/0101  1,18,000.00", attach=("advice.csv", "INV/26-27/0102,59000.00\n"))
    r = mailbox.process_email(w.db, w.e.id, raw, {}, "t")
    doc = w.db.get(RemittanceDocument, r["id"])
    assert "evil()" not in doc.raw_text and "0101" in doc.raw_text and "0102" in doc.raw_text and "attachment: advice.csv" in doc.raw_text


class FakeIMAP:
    def __init__(self, mails, fail_on=None):
        self.mails, self.seen, self.copied, self.fail_on = dict(enumerate(mails, start=1)), set(), [], fail_on
        self.logged = False

    def login(self, u, p):
        assert (u, p) == ("ar@us.example", "pw")
        self.logged = True

    def select(self, f): return ("OK", [b"1"])
    def search(self, c, q): return ("OK", [b" ".join(str(i).encode() for i in self.mails if i not in self.seen)])

    def fetch(self, i, spec):
        i = int(i)
        if i == self.fail_on:
            raise RuntimeError("boom")
        return ("OK", [(b"1 (BODY[] {n}", self.mails[i]), b")"])

    def store(self, i, a, flags): self.seen.add(int(i))
    def copy(self, i, folder): self.copied.append((int(i), folder))
    def logout(self): pass


def test_imap_poll_is_idempotent_isolates_bad_messages_and_marks_seen_only_after_storing(w, monkeypatch):
    monkeypatch.setenv("MB_PW", "pw")
    cfg = {"host": "imap.example.in", "username": "ar@us.example", "password_env": "MB_PW", "processed_folder": "Done"}
    fake = FakeIMAP([mail(mid="<a@x>"), mail(mid="<b@x>"), mail(mid="<c@x>", auth="dkim=fail")], fail_on=2)
    out = mailbox.poll_mailbox(w.db, w.e.id, cfg, "t", imap_factory=lambda h, p: fake)
    assert out["ingested"] == 1 and out["quarantined"] == 1 and len(out["errors"]) == 1
    assert fake.seen == {1, 3} and 2 not in fake.seen, "failed message stays UNSEEN for retry"
    fake.fail_on = None
    again = mailbox.poll_mailbox(w.db, w.e.id, cfg, "t", imap_factory=lambda h, p: fake)
    assert again["ingested"] == 1 and again["duplicates"] == 0


def test_poll_requires_credentials_from_environment(w, monkeypatch):
    monkeypatch.delenv("MB_PW", raising=False)
    with pytest.raises(mailbox.MailboxError, match="not set"):
        mailbox.poll_mailbox(w.db, w.e.id, {"host": "h", "username": "u", "password_env": "MB_PW"}, "t", imap_factory=lambda h, p: FakeIMAP([]))
    assert mailbox.validate_mailbox({"host": "h"})


def test_sender_is_learned_only_from_authenticated_mail(w):
    from app.matching import actions

    c = w.customer("C1", "Acme Industries Pvt Ltd")
    inv = w.invoice(c, "INV/26-27/0101", 100_000_00)
    t = w.credit(11800000, "NEFT-HDFCN26274000111-ACME INDUSTRIES-PAY", utr="HDFCN26274000111")
    mailbox.process_email(w.db, w.e.id, mail(mid="<l1@x>"), {}, "t")
    w.run(t)
    from app.models import MatchProposal

    p = w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t.id)).scalars().first()
    if p.status.value == "pending_review":
        actions.apply_proposal(w.db, None, p, "a", w.users["ar_analyst"].id)
    assert w.db.execute(select(CustomerIdentifier).where(CustomerIdentifier.kind == "email")).scalar_one().value == "accounts@acme.example"


# ---------------------------------------------------------------------------------------------- data residency
def _strict(monkeypatch, region="ap-south-1", allowed=("erp.example.in",)):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, residency_strict=True, deploy_region=region, allowed_egress=allowed))


def test_strict_mode_refuses_to_start_outside_india(monkeypatch):
    _strict(monkeypatch, region="us-east-1")
    with pytest.raises(RuntimeError, match="Indian region"):
        config.assert_safe_for_production()
    _strict(monkeypatch, region="ap-south-2")
    config.assert_safe_for_production()


def test_strict_mode_blocks_every_outbound_path_unless_allow_listed(w, monkeypatch):
    _strict(monkeypatch)
    with pytest.raises(config.EgressBlocked):
        config.check_egress("https://api.anthropic.com/v1/messages")
    config.check_egress("https://erp.example.in/hook")
    assert not config.egress_allowed("evil.example.com") and config.egress_allowed("erp.example.in")
    w.bank.feed = {"type": "rest_json", "url": "https://api.other-bank.com/x", "records_path": "a", "map": {"date": "d", "amount": "a", "narration": "n"}}
    with pytest.raises(config.EgressBlocked):
        feeds.fetch_rest_json(w.bank.feed, __import__("datetime").date.today(), lambda *a: {})
    with pytest.raises(config.EgressBlocked):
        mailbox.poll_mailbox(w.db, w.e.id, {"host": "imap.gmail.com", "username": "u", "password_env": "X"}, "t")


def test_llm_extractor_is_disabled_in_strict_mode_even_with_a_key(monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, anthropic_api_key="k"))
    import app.config as c

    assert isinstance(get_extractor(), LLMExtractor)
    _strict(monkeypatch)
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, anthropic_api_key="k"))
    assert not isinstance(get_extractor(), LLMExtractor), "remittance text must never leave India by default"


def test_webhook_to_unlisted_host_cannot_be_configured_or_called(client, db, w, monkeypatch):
    from app.seed.demo import DEMO_PASSWORD, seed_demo

    e = seed_demo(db, run_matching=False)["entity_id"]
    db.commit()
    _strict(monkeypatch)
    h = login(client, "cfo@meridian.example", DEMO_PASSWORD)
    r = client.put(f"/api/v1/entities/{e}/integrations", json={"erp_export": {"type": "webhook", "url": "https://elsewhere.com/x", "secret_env": "S"}}, headers=h)
    assert r.status_code == 422 and "ALLOWED_EGRESS_HOSTS" in r.text
    ok = client.put(f"/api/v1/entities/{e}/integrations", json={"erp_export": {"type": "webhook", "url": "https://erp.example.in/x", "secret_env": "S"}}, headers=h)
    assert ok.status_code == 200
    assert client.get(f"/api/v1/entities/{e}/integrations", headers=h).json()["residency"]["strict"] is True
    assert client.put(f"/api/v1/entities/{e}/integrations", json={"mailbox": {"host": "x"}}, headers=login(client, "analyst@meridian.example", DEMO_PASSWORD)).status_code == 403
