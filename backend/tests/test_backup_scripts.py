"""Runs deploy/vm/scripts/backup.sh and restore.sh for real. Needs PostgreSQL client tools and a server:
    TEST_DATABASE_URL=postgresql+psycopg://user:pw@host:port/db pytest tests/test_backup_scripts.py
The role must be allowed to CREATE DATABASE. Skipped otherwise."""
import os
import shutil
import subprocess
from urllib.parse import urlparse

import pytest

URL = os.environ.get("TEST_DATABASE_URL")
SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "vm", "scripts")
pytestmark = pytest.mark.skipif(not URL or not shutil.which("pg_dump") or not shutil.which("openssl"), reason="needs TEST_DATABASE_URL and pg_dump/openssl")


def run(script, args, env, ok=True):
    r = subprocess.run([os.path.join(SCRIPTS, script), *args], env=env, capture_output=True, text=True)
    assert (r.returncode == 0) == ok, (r.returncode, r.stdout[-300:], r.stderr[-300:])
    return r


@pytest.fixture()
def env(tmp_path):
    u = urlparse(URL.replace("+psycopg", ""))
    e = {**os.environ, "PGHOST": u.hostname, "PGPORT": str(u.port or 5432), "PGUSER": u.username, "PGPASSWORD": u.password or "", "PGDATABASE": u.path.lstrip("/"),
         "BACKUP_PASSPHRASE": "pass phrase with spaces $ and 'quotes'", "BACKUP_DIR": str(tmp_path / "bk"), "DATA_DIR": str(tmp_path / "data")}
    (tmp_path / "bk").mkdir(); (tmp_path / "data" / "exports").mkdir(parents=True); (tmp_path / "data" / "exports" / "x.csv").write_text("a,b\n")
    return e


def newest(env):
    return os.path.join(env["BACKUP_DIR"], sorted(d for d in os.listdir(env["BACKUP_DIR"]) if d.startswith("20"))[-1])


def test_backup_is_encrypted_verified_by_a_drill_and_refuses_wrong_passphrase_or_tampering(env):
    run("backup.sh", [], env)
    b = newest(env)
    assert open(os.path.join(env["BACKUP_DIR"], "LAST_OK")).read().strip() == os.path.basename(b)
    blob = open(os.path.join(b, "db.dump.enc"), "rb").read()
    assert b"PGDMP" not in blob and b"CREATE TABLE" not in blob, "must not be readable without the passphrase"
    assert "RESTORE DRILL OK" in run("restore.sh", [b, "--verify"], env).stdout
    assert "cannot decrypt" in run("restore.sh", [b, "--verify"], {**env, "BACKUP_PASSPHRASE": "wrong"}, ok=False).stderr
    with open(os.path.join(b, "db.dump.enc"), "ab") as f:
        f.write(b"x")
    assert "MANIFEST check FAILED" in run("restore.sh", [b, "--verify"], env, ok=False).stderr


def test_retention_removes_old_backups_and_stale_partials_but_never_the_newest(env):
    bk = env["BACKUP_DIR"]
    for name, age in (("20240101T000000Z", "-d '40 days ago'"), ("20260101T000000Z.partial", "-d '5 hours ago'")):
        os.makedirs(os.path.join(bk, name)); subprocess.run(f"touch {age} {os.path.join(bk, name)}", shell=True, check=True)
    run("backup.sh", [], {**env, "BACKUP_KEEP_DAYS": "14"})
    assert [d for d in os.listdir(bk) if d != "LAST_OK"] == [os.path.basename(newest(env))]
