"""Notification outbox: configuration, emitters, delivery, retry, worker."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import notify, workflow
from app.jobs import run_once
from app.matching import actions
from app.models import (ApprovalKind, BatchStatus, ExceptionCase, ExceptionStatus, ExceptionType, MatchProposal, Notification, ProposalStatus, utcnow)
from app.posting import service as posting
from tests.conftest import login
from tests.test_period_evidence import applied_receipt

CFG = {"email": {"host": "smtp.example.in", "port": 587, "from": "cash@example.in", "to": ["ar@example.in", "cfo@example.in"], "username_env": "SMTP_USER", "password_env": "SMTP_PASS"},
       "slack": {"webhook_env": "SLACK_URL", "events": ["approval.requested", "auto.reversed"]},
       "teams": {"webhook_env": "TEAMS_URL", "events": ["batch.failed"]}}


@pytest.fixture()
def cfg(w, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASS", "p")
    monkeypatch.setenv("SLACK_URL", "https://hooks.slack.example/T/B/x")
    monkeypatch.setenv("TEAMS_URL", "https://example.webhook.office.example/x")
    w.e.config = {**w.e.config, "notifications": CFG}
    w.db.flush()
    return w


class FakeSMTP:
    sent: list = []
    fail = False

    def __init__(self, host, port):
        self.host, self.port, self.tls, self.login_ = host, port, False, None

    def __enter__(self):
        if FakeSMTP.fail:
            raise ConnectionRefusedError("smtp down")
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, u, p):
        self.login_ = (u, p)

    def send_message(self, m):
        FakeSMTP.sent.append((self.host, self.port, self.tls, self.login_, m["Subject"], m["To"], m.get_content()))


@pytest.fixture(autouse=True)
def reset_smtp():
    FakeSMTP.sent, FakeSMTP.fail = [], False


def test_validation_rejects_unknown_things_and_missing_secrets(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    errs = notify.validate({"sms": {}, "email": {"events": ["x.y"]}, "slack": {"webhook_env": "bad name!"}, "teams": {}})
    joined = "; ".join(errs)
    assert "unknown channel 'sms'" in joined and "unknown event 'x.y'" in joined and "email: host, from" in joined and "webhook_env must name" in joined
    monkeypatch.setenv("HOOK_HTTP", "http://insecure.example/x")
    assert any("must be https" in e for e in notify.validate({"slack": {"webhook_env": "HOOK_HTTP"}}))
    assert notify.validate(CFG) == []


def test_strict_residency_blocks_hosts_that_are_not_allow_listed(monkeypatch):
    import dataclasses

    from app import config

    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, residency_strict=True, allowed_egress=("smtp.example.in",)))
    monkeypatch.setenv("SLACK_URL", "https://hooks.slack.example/x")
    errs = notify.validate({"email": CFG["email"], "slack": {"webhook_env": "SLACK_URL"}})
    assert errs == ["slack: webhook host is not in ALLOWED_EGRESS_HOSTS (data-residency strict mode)"]


def test_emit_honours_event_filters_and_is_idempotent(cfg):
    w = cfg
    assert notify.emit(w.db, w.e.id, "approval.requested", "s", "b", "k1") == 2        # email (all events) + slack
    assert notify.emit(w.db, w.e.id, "approval.requested", "s", "b", "k1") == 0
    assert notify.emit(w.db, w.e.id, "batch.failed", "s", "b", "k2") == 2              # email + teams
    assert notify.emit(w.db, w.e.id, "model.alert", "s", "b", "k3") == 1               # email only
    assert sorted((n.channel, n.event) for n in w.db.execute(select(Notification)).scalars()) == sorted(
        [("email", "approval.requested"), ("slack", "approval.requested"), ("email", "batch.failed"), ("teams", "batch.failed"), ("email", "model.alert")])


def test_nothing_is_queued_for_an_entity_without_notification_config(w):
    assert notify.emit(w.db, w.e.id, "approval.requested", "s", "b", "k") == 0


def test_dispatch_delivers_each_channel_with_credentials_from_the_environment(cfg):
    w = cfg
    notify.emit(w.db, w.e.id, "approval.requested", "Approval needed", "Reason: refund", "k1")
    posts = []
    res = notify.dispatch(w.db, smtp_factory=FakeSMTP, http=lambda url, payload: posts.append((url, payload)))
    assert res == {"sent": 2, "retrying": 0, "failed": 0}
    host, port, tls, login_, subj, to, body = FakeSMTP.sent[0]
    assert (host, port, tls, login_) == ("smtp.example.in", 587, True, ("u", "p")) and subj == "[Cash Application] Approval needed" and to == "ar@example.in, cfo@example.in"
    assert "Reason: refund" in body and posts == [("https://hooks.slack.example/T/B/x", {"text": "*Approval needed*\nReason: refund"})]
    assert {n.status for n in w.db.execute(select(Notification)).scalars()} == {"sent"}


def test_failures_retry_with_backoff_then_give_up_and_a_recovery_still_delivers(cfg):
    w = cfg
    notify.emit(w.db, w.e.id, "model.alert", "s", "b", "k1")
    FakeSMTP.fail = True
    now = utcnow()
    n = w.db.execute(select(Notification)).scalar_one()
    r = notify.dispatch(w.db, now, smtp_factory=FakeSMTP)
    assert r == {"sent": 0, "retrying": 1, "failed": 0} and n.attempts == 1 and n.next_attempt_at == now + timedelta(minutes=2) and "ConnectionRefusedError" in n.error
    assert notify.dispatch(w.db, now, smtp_factory=FakeSMTP) == {"sent": 0, "retrying": 0, "failed": 0}, "not due yet"
    FakeSMTP.fail = False
    assert notify.dispatch(w.db, now + timedelta(minutes=3), smtp_factory=FakeSMTP)["sent"] == 1 and n.status == "sent" and n.error is None
    notify.emit(w.db, w.e.id, "model.alert", "s2", "b", "k2")
    FakeSMTP.fail = True
    t = now
    for _ in range(notify.MAX_ATTEMPTS):
        t += timedelta(hours=1)
        notify.dispatch(w.db, t, smtp_factory=FakeSMTP)
    assert w.db.execute(select(Notification).where(Notification.dedupe_key == "k2")).scalar_one().status == "failed"


def test_a_missing_webhook_variable_is_reported_not_crashed(cfg, monkeypatch):
    w = cfg
    notify.emit(w.db, w.e.id, "batch.failed", "s", "b", "k")
    monkeypatch.delenv("TEAMS_URL")
    notify.dispatch(w.db, smtp_factory=FakeSMTP, http=lambda *a: None)
    n = w.db.execute(select(Notification).where(Notification.channel == "teams")).scalar_one()
    assert n.status == "pending" and "TEAMS_URL is not set" in n.error


def test_approval_request_auto_reversal_and_failed_batch_queue_alerts(cfg):
    w = cfg
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    inv = w.invoice(c, "INV-1", 100_000_00)
    t = w.credit(inv.total_minor - 500_00, "NEFT-HDFCN26274007001-ACME INDUSTRIES PVT LTD-INV-1")
    w.run(t)
    p = w.db.execute(select(MatchProposal).where(MatchProposal.txn_id == t.id)).scalar_one()
    if p.status == ProposalStatus.pending_review:
        actions.apply_proposal(w.db, None, p, "a", w.users["ar_analyst"].id)
    ex = w.db.execute(select(ExceptionCase).where(ExceptionCase.type == ExceptionType.short_pay)).scalar_one()
    workflow.resolve_exception(w.db, w.users["ar_analyst"], ex, "write_off", {"note": "Quality claim agreed with sales"})
    assert w.db.execute(select(Notification).where(Notification.event == "approval.requested", Notification.channel == "slack")).scalar_one().subject.startswith("Approval needed: write off")
    t2, p2 = applied_receipt(w, utcnow().date() - timedelta(days=3), number="INV/26-27/7002", ref="HDFCN26274007002")
    if p2.auto:
        actions.reverse_proposal(w.db, p2, "a", w.users["ar_analyst"].id, "wrong customer")
        assert w.db.execute(select(Notification).where(Notification.event == "auto.reversed")).first() is not None
    b = posting.build_batch(w.db, w.e, w.users["ar_manager"])
    posting.fail_batch(w.db, b, "ERP rejected voucher 3", w.users["treasury"])
    assert w.db.execute(select(Notification).where(Notification.event == "batch.failed", Notification.channel == "teams")).scalar_one().body.endswith("ERP rejected voucher 3")


def test_scan_alerts_once_for_sla_breaches_and_unconfirmed_exports(cfg):
    from app.models import PostingBatch

    w = cfg
    c = w.customer("C1", "Acme Industries Pvt Ltd")
    x = ExceptionCase(entity_id=w.e.id, type=ExceptionType.short_pay, status=ExceptionStatus.open, amount_minor=500_00, reason="short", priority=1, due_at=utcnow() - timedelta(hours=3))
    w.db.add(x)
    w.db.add(PostingBatch(entity_id=w.e.id, status=BatchStatus.exported, idempotency_key="k" * 64, total_minor=1_000_00, exported_at=utcnow() - timedelta(hours=30)))
    w.db.flush()
    first = notify.scan(w.db, w.e)
    again = notify.scan(w.db, w.e)
    assert first["queued"] == 2 and again["queued"] == 0, "one email each: the Slack and Teams channels subscribe to other events"
    events = {n.event for n in w.db.execute(select(Notification)).scalars()}
    assert {"exception.sla_breach", "erp.unconfirmed"} <= events


def test_api_configure_test_send_and_list(client, cfg, monkeypatch):
    w = cfg
    fin, an = login(client, "finance_head@t.example", "pw-finance_head"), login(client, "ar_analyst@t.example", "pw-ar_analyst")
    url = f"/api/v1/entities/{w.e.id}/notifications"
    assert client.put(url, json=CFG, headers=an).status_code == 403
    assert client.put(url, json={"sms": {}}, headers=fin).status_code == 422
    assert client.put(url, json={"slack": CFG["slack"]}, headers=fin).status_code == 200
    assert client.get(url, headers=an).json()["config"] == {"slack": CFG["slack"]}
    sent = []
    monkeypatch.setattr(notify, "_send_hook", lambda ch, c, n, http: sent.append(n.subject))
    r = client.post(f"{url}/test?channel=slack", headers=fin)
    assert r.json() == {"status": "sent", "error": None} and sent == ["Test alert"]
    assert client.post(f"{url}/test?channel=email", headers=fin).status_code == 422
    items = client.get(f"/api/v1/notifications?entity_id={w.e.id}", headers=an).json()
    assert items[0]["event"] == "test" and items[0]["status"] == "sent"


def test_worker_sends_pending_alerts_once_per_minute_slot(db, cfg, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    w = cfg
    notify.emit(w.db, w.e.id, "model.alert", "s", "b", "k1")
    w.db.commit()
    S = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    now = utcnow()
    ran = run_once(S, now, smtp_factory=FakeSMTP, notify_http=lambda *a: None)
    assert {"job": "notify_send", "status": "ok"} in ran and len(FakeSMTP.sent) == 1
    notify.emit(w.db, w.e.id, "model.alert", "s2", "b", "k2")
    w.db.commit()
    assert not [r for r in run_once(S, now, smtp_factory=FakeSMTP, notify_http=lambda *a: None) if r["job"] == "notify_send"], "same minute slot already claimed"
    assert len(FakeSMTP.sent) == 1
