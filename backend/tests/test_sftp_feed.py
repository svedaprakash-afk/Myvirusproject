"""Native SFTP polling against a real in-process SFTP server (paramiko), including host-key pinning and archiving."""
import os
import socket
import threading

import paramiko
import pytest
from sqlalchemy import select

from app.ingestion import feeds
from app.models import PaymentTransaction

MT940 = """:20:STMT1
:25:50200099887766
:28C:1/1
:60F:C260901INR100000,00
:61:2610080008CR118000,00NTRFHDFCN26274009001//BANKREF1
:86:NEFT-HDFCN26274009001-ACME INDUSTRIES PVT LTD-INV/26-27/9001
:62F:C261008INR218000,00
"""


class _Stub(paramiko.SFTPServerInterface):
    ROOT = ""

    def _p(self, path):
        return os.path.join(self.ROOT, path.lstrip("/"))

    def list_folder(self, path):
        out = []
        for n in os.listdir(self._p(path)):
            a = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(self._p(path), n)))
            a.filename = n
            out.append(a)
        return out

    def stat(self, path):
        return paramiko.SFTPAttributes.from_stat(os.stat(self._p(path)))

    lstat = stat

    def open(self, path, flags, attr):
        mode = "rb" if flags == os.O_RDONLY else "wb"
        f = open(self._p(path), mode)
        h = paramiko.SFTPHandle(flags)
        h.filename, h.readfile, h.writefile = path, f if mode == "rb" else None, f if mode == "wb" else None
        return h

    def rename(self, old, new):
        os.rename(self._p(old), self._p(new))
        return paramiko.SFTP_OK

    def remove(self, path):
        os.remove(self._p(path))
        return paramiko.SFTP_OK

    def mkdir(self, path, attr):
        os.makedirs(self._p(path), exist_ok=True)
        return paramiko.SFTP_OK


class _Auth(paramiko.ServerInterface):
    def __init__(self, password, pub):
        self.password, self.pub = password, pub

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if username == "corp" and password == self.password else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if username == "corp" and self.pub and key.asbytes() == self.pub.asbytes() else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class Server:
    def __init__(self, root, password="pw-secret", client_pub=None):
        _Stub.ROOT = str(root)
        self.host_key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.password, self.client_pub, self.stop = password, client_pub, False
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def fingerprint(self):
        return feeds._fingerprint(self.host_key)

    def _loop(self):
        self.sock.settimeout(0.5)
        while not self.stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                continue
            t = paramiko.Transport(c)
            t.add_server_key(self.host_key)
            t.set_subsystem_handler("sftp", paramiko.SFTPServer, _Stub)
            t.start_server(server=_Auth(self.password, self.client_pub))

    def close(self):
        self.stop = True
        self.sock.close()


@pytest.fixture()
def srv(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "processed").mkdir()
    (tmp_path / "out" / "stmt1.sta").write_text(MT940)
    (tmp_path / "out" / "notes.txt").write_text("ignore me")
    s = Server(tmp_path)
    yield s
    s.close()


def cfg(srv, **over):
    return {"type": "sftp_native", "host": "127.0.0.1", "port": srv.port, "username": "corp", "remote_dir": "/out", "pattern": "*.sta", "host_key_sha256": srv.fingerprint,
            "auth": {"password_env": "BANK_PW"}, **over}


def test_config_validation_demands_a_pinned_key_and_env_named_secrets():
    errs = feeds.validate_feed({"type": "sftp_native", "host": "h", "username": "u", "remote_dir": "/o", "auth": {"password_env": "bad name"}})
    assert any("host_key_sha256" in e for e in errs) and any("environment variable name" in e for e in errs)
    assert any("must look like SHA256" in e for e in feeds.validate_feed({"type": "sftp_native", "host": "h", "username": "u", "remote_dir": "/o", "host_key_sha256": "abc", "auth": {"password_env": "X"}}))
    assert any("auth.password_env or auth.key_env" in e for e in feeds.validate_feed({"type": "sftp_native", "host": "h", "username": "u", "remote_dir": "/o", "host_key_sha256": "SHA256:" + "A" * 43}))
    assert feeds.validate_feed({"type": "sftp_native", "host": "h", "username": "u", "remote_dir": "/o", "host_key_sha256": "SHA256:" + "A" * 43, "auth": {"password_env": "X"}}) == []


def test_password_auth_lists_only_matching_files_and_reads_them(srv, monkeypatch):
    monkeypatch.setenv("BANK_PW", "pw-secret")
    files = feeds.fetch_sftp(cfg(srv))
    assert [f.name for f in files] == ["stmt1.sta"] and files[0].data.decode() == MT940 and files[0].on_success is None


def test_a_changed_host_key_is_refused_before_credentials_are_sent(srv, monkeypatch):
    monkeypatch.setenv("BANK_PW", "pw-secret")
    sent = []
    orig = paramiko.Transport.auth_password
    monkeypatch.setattr(paramiko.Transport, "auth_password", lambda self, *a, **k: sent.append(a) or orig(self, *a, **k))
    with pytest.raises(feeds.FeedError, match="host key mismatch"):
        feeds.fetch_sftp(cfg(srv, host_key_sha256="SHA256:" + "B" * 43))
    assert sent == [], "the password must never be offered to an unverified server"


def test_wrong_password_and_missing_env_are_reported_without_leaking_secrets(srv, monkeypatch):
    monkeypatch.setenv("BANK_PW", "wrong")
    with pytest.raises(feeds.FeedError, match="refused the credentials"):
        feeds.fetch_sftp(cfg(srv))
    monkeypatch.delenv("BANK_PW")
    with pytest.raises(feeds.FeedError, match="BANK_PW is not set"):
        feeds.fetch_sftp(cfg(srv))


def test_key_auth_with_a_generated_ed25519_key(tmp_path, monkeypatch):
    import io

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.sta").write_text(MT940)
    k = ed25519.Ed25519PrivateKey.generate()
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode()
    pub = paramiko.Ed25519Key.from_private_key(io.StringIO(pem))
    s = Server(tmp_path, client_pub=pub)
    try:
        monkeypatch.setenv("BANK_KEY", pem)
        c = {"type": "sftp_native", "host": "127.0.0.1", "port": s.port, "username": "corp", "remote_dir": "/out", "host_key_sha256": s.fingerprint, "auth": {"key_env": "BANK_KEY"}}
        assert [f.name for f in feeds.fetch_sftp(c)] == ["a.sta"]
    finally:
        s.close()


def test_sync_bank_ingests_archives_and_a_second_poll_finds_nothing_new(srv, db, w, monkeypatch, tmp_path):
    monkeypatch.setenv("BANK_PW", "pw-secret")
    w.bank.feed = cfg(srv, archive_dir="/out/processed")
    w.db.flush()
    r = feeds.sync_bank(w.db, w.bank, "t", None)
    assert r["inserted"] == 1 and not r["errors"] and "archive_warning" not in r["results"][0]
    t = w.db.execute(select(PaymentTransaction).where(PaymentTransaction.bank_account_id == w.bank.id)).scalar_one()
    assert t.amount_minor == 118_000_00 and "HDFCN26274009001" in t.narration
    assert [f for f in os.listdir(tmp_path / "out") if f.endswith(".sta")] == [] and len(os.listdir(tmp_path / "out" / "processed")) == 1
    assert feeds.sync_bank(w.db, w.bank, "t", None)["inserted"] == 0


def test_an_archive_failure_does_not_lose_the_ingest(srv, db, w, monkeypatch):
    monkeypatch.setenv("BANK_PW", "pw-secret")
    w.bank.feed = cfg(srv, archive_dir="/does/not/exist")
    w.db.flush()
    r = feeds.sync_bank(w.db, w.bank, "t", None)
    assert r["inserted"] == 1 and "could not move to the archive folder" in r["results"][0]["archive_warning"]
    assert feeds.sync_bank(w.db, w.bank, "t", None)["inserted"] == 0, "the file is polled again but deduplicated"
