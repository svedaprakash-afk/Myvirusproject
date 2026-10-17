"""S3 behaviour is tested against moto (an in-process AWS mock). That proves the code speaks the S3 API correctly; it does NOT prove
behaviour against real AWS S3, Oracle, Ceph or MinIO, nor bucket-policy / KMS / network concerns."""
import dataclasses
import os
import subprocess
import sys

import boto3
import pytest
from moto import mock_aws

from app import config, storage
from app.ingestion import feeds
from app.models import BatchStatus, PaymentTransaction
from app.posting import service as posting
from conftest import login

BACKEND = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test", "AWS_DEFAULT_REGION": "ap-south-1"}.items():
        monkeypatch.setenv(k, v)


def cfg(**kw):
    base = dict(storage_backend="s3", s3_bucket="cash-bucket", s3_prefix="cashapp", s3_region="ap-south-1", s3_sse="aws:kms", s3_kms_key_id="alias/cashapp")
    return dataclasses.replace(config.settings, **{**base, **kw})


@pytest.fixture()
def bucket():
    with mock_aws():
        c = boto3.client("s3", region_name="ap-south-1")
        c.create_bucket(Bucket="cash-bucket", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        yield c


def use(monkeypatch, c):
    monkeypatch.setattr(config, "settings", c)
    return c


# ---------------------------------------------------------------------------------------------- keys & local backend
@pytest.mark.parametrize("bad", ["", "/abs", "../x", "a/../b", "a/./b", "a//b", "a\\b", "x" * 600, "a\x00b"])
def test_unsafe_keys_are_rejected(bad):
    with pytest.raises(storage.StorageError):
        storage.clean_key(bad)


def test_local_store_roundtrip_atomic_write_and_traversal_guard(tmp_path):
    s = storage.LocalStore(str(tmp_path / "root"))
    s.put("1/2/a.csv", b"hello", "text/csv")
    assert s.get("1/2/a.csv") == b"hello" and s.exists("1/2/a.csv") and not s.exists("1/2/b.csv")
    assert [o.key for o in s.list("1")] == ["1/2/a.csv"] and s.list("nope") == []
    s.move("1/2/a.csv", "1/processed/a.csv")
    assert [o.key for o in s.list()] == ["1/processed/a.csv"]
    s.delete("1/processed/a.csv"); s.delete("1/processed/a.csv")          # idempotent
    assert s.list() == []
    with pytest.raises(storage.StorageError):
        s.get("missing")
    (tmp_path / "secret.txt").write_text("x")
    os.symlink(tmp_path / "secret.txt", tmp_path / "root" / "link.txt")
    with pytest.raises(storage.StorageError, match="escapes"):
        s.get("link.txt")                                                  # a symlink cannot point outside the root


def test_integrity_verification_detects_corruption(tmp_path):
    s = storage.LocalStore(str(tmp_path))
    s.put("a", b"original")
    (tmp_path / "a").write_bytes(b"tampered")
    with pytest.raises(storage.StorageError, match="integrity"):
        s.get("a", verify_sha256=storage.sha256_hex(b"original"))


# ---------------------------------------------------------------------------------------------- S3 backend
def test_s3_roundtrip_with_encryption_metadata_and_prefix_isolation(bucket):
    c = cfg()
    ex, fd = storage.S3Store(c, "exports"), storage.S3Store(c, "feeds")
    ex.put("1/2/a.csv", b"data", "text/csv")
    assert ex.get("1/2/a.csv") == b"data" and ex.exists("1/2/a.csv") and not fd.exists("1/2/a.csv")
    head = bucket.head_object(Bucket="cash-bucket", Key="cashapp/exports/1/2/a.csv")
    assert head["ServerSideEncryption"] == "aws:kms" and head["SSEKMSKeyId"] == "alias/cashapp"
    assert head["Metadata"]["sha256"] == storage.sha256_hex(b"data") and head["ContentType"] == "text/csv"
    assert fd.list() == [] and [o.key for o in ex.list()] == ["1/2/a.csv"]


def test_s3_aes256_and_no_sse_variants(bucket):
    storage.S3Store(cfg(s3_sse="AES256"), "exports").put("a", b"x")
    assert bucket.head_object(Bucket="cash-bucket", Key="cashapp/exports/a")["ServerSideEncryption"] == "AES256"
    storage.S3Store(cfg(s3_sse="none"), "exports").put("b", b"x")
    assert bucket.head_object(Bucket="cash-bucket", Key="cashapp/exports/b").get("ServerSideEncryption") is None


def test_s3_listing_paginates_beyond_1000_objects(bucket):
    s = storage.S3Store(cfg(), "feeds")
    for i in range(1005):
        bucket.put_object(Bucket="cash-bucket", Key=f"cashapp/feeds/bulk/f{i:05d}.csv", Body=b"x")
    keys = [o.key for o in s.list("bulk")]
    assert len(keys) == 1005 and keys == sorted(keys)


def test_s3_move_preserves_content_and_integrity_metadata(bucket):
    s = storage.S3Store(cfg(), "feeds")
    s.put("in/a.csv", b"abc")
    s.move("in/a.csv", "in/processed/a.csv")
    assert not s.exists("in/a.csv") and s.get("in/processed/a.csv") == b"abc"
    assert bucket.head_object(Bucket="cash-bucket", Key="cashapp/feeds/in/processed/a.csv")["Metadata"]["sha256"] == storage.sha256_hex(b"abc")


def test_s3_detects_an_object_replaced_behind_our_back(bucket):
    s = storage.S3Store(cfg(), "exports")
    s.put("a", b"original")
    bucket.put_object(Bucket="cash-bucket", Key="cashapp/exports/a", Body=b"tampered", Metadata={"sha256": storage.sha256_hex(b"original")})
    with pytest.raises(storage.StorageError, match="integrity"):
        s.get("a")


def test_s3_errors_are_clear_not_stack_traces(bucket):
    with pytest.raises(storage.StorageError, match="does not exist"):
        storage.S3Store(cfg(s3_bucket="no-such-bucket"), "exports").put("a", b"x")
    s = storage.S3Store(cfg(), "exports")
    with pytest.raises(storage.StorageError, match="not found"):
        s.get("missing")
    assert s.exists("missing") is False


def test_probe_writes_reads_and_cleans_up(bucket):
    r = storage.probe(storage.S3Store(cfg(), "exports"))
    assert r["ok"] and r["backend"] == "s3" and r["bucket"] == "cash-bucket"
    assert bucket.list_objects_v2(Bucket="cash-bucket").get("KeyCount", 0) == 0


# ---------------------------------------------------------------------------------------------- residency / config validation
def test_validation_rules():
    assert storage.validate_settings(config.settings) == []                                  # local default
    assert "STORAGE_BACKEND" in storage.validate_settings(cfg(storage_backend="ftp"))[0]
    assert any("S3_BUCKET" in e for e in storage.validate_settings(cfg(s3_bucket="")))
    assert any("S3_SSE" in e for e in storage.validate_settings(cfg(s3_sse="rot13")))
    assert storage.validate_settings(cfg()) == []                                            # non-strict: region is the operator's business


def test_strict_mode_requires_indian_region_explicit_encryption_and_allow_listed_host():
    strict = dict(residency_strict=True, deploy_region="ap-south-1", allowed_egress=("s3.ap-south-1.amazonaws.com",))
    assert storage.validate_settings(cfg(**strict)) == []
    assert any("Indian region" in e for e in storage.validate_settings(cfg(s3_region="us-east-1", **strict)))
    assert any("S3_SSE" in e for e in storage.validate_settings(cfg(s3_sse="", **strict)))
    assert any("KMS_KEY_ID" in e for e in storage.validate_settings(cfg(s3_kms_key_id="", **strict)))
    assert any("ALLOWED_EGRESS_HOSTS" in e for e in storage.validate_settings(cfg(**{**strict, "allowed_egress": ("erp.example.in",)})))
    # a custom endpoint (on-prem / other provider) is judged by the allow-list, not by an AWS region name
    ok = cfg(s3_endpoint_url="https://objects.corp.example.in", s3_region="", s3_sse="none", **{**strict, "allowed_egress": ("objects.corp.example.in",)})
    assert storage.validate_settings(ok) == []
    assert any("ALLOWED_EGRESS" in e for e in storage.validate_settings(dataclasses.replace(ok, allowed_egress=())))


def test_get_store_refuses_a_bad_config(monkeypatch):
    with pytest.raises(storage.StorageError, match="S3_BUCKET"):
        storage.get_store("exports", cfg(s3_bucket=""))


def test_service_refuses_to_start_with_bad_storage(tmp_path):
    code = ("import asyncio; from app.main import app\nasync def go():\n    async with app.router.lifespan_context(app): print('STARTED')\nasyncio.run(go())")
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path}/s.db", "STORAGE_BACKEND": "s3", "DATA_RESIDENCY_STRICT": "1", "DEPLOY_REGION": "ap-south-1", "S3_REGION": "us-east-1"}
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "Storage misconfigured" in r.stderr and "STARTED" not in r.stdout


# ---------------------------------------------------------------------------------------------- integration: exports
def approved_batch(w):
    from test_integrations import _batch

    return _batch(w)


def test_export_is_stored_in_object_storage_and_downloadable_with_checksum(w, bucket, monkeypatch):
    use(monkeypatch, cfg())
    b = approved_batch(w)
    name, content, _ = posting.export_batch(w.db, b, "generic_csv", w.users["treasury"])
    assert b.status == BatchStatus.exported and b.export_key == f"{w.e.id}/{b.id}/{name}"
    obj = bucket.get_object(Bucket="cash-bucket", Key=f"cashapp/exports/{b.export_key}")
    assert obj["Body"].read() == content and obj["ServerSideEncryption"] == "aws:kms"
    fname, data = posting.download_stored(w.db, b, w.users["auditor"])
    assert fname == name and data == content


def test_download_refuses_a_file_that_no_longer_matches_its_recorded_checksum(w, bucket, monkeypatch):
    use(monkeypatch, cfg())
    b = approved_batch(w)
    posting.export_batch(w.db, b, "generic_csv", w.users["treasury"])
    bucket.put_object(Bucket="cash-bucket", Key=f"cashapp/exports/{b.export_key}", Body=b"voucher_no\nFORGED\n")     # no metadata: caught by the batch's own checksum
    with pytest.raises(posting.PostingError, match="integrity"):
        posting.download_stored(w.db, b, w.users["auditor"])


def test_a_batch_is_not_marked_exported_if_its_file_cannot_be_stored(w, monkeypatch):
    use(monkeypatch, cfg(s3_bucket="no-such-bucket"))
    with mock_aws():
        b = approved_batch(w)
        with pytest.raises(posting.PostingError, match="NOT marked exported"):
            posting.export_batch(w.db, b, "generic_csv", w.users["treasury"])
    assert b.status == BatchStatus.approved and b.export_key is None and b.checksum is None


def test_export_to_local_store_still_works_and_records_the_key(w, tmp_path, monkeypatch):
    use(monkeypatch, dataclasses.replace(config.settings, storage_backend="local", export_dir=str(tmp_path / "ex")))
    b = approved_batch(w)
    name, content, _ = posting.export_batch(w.db, b, "generic_csv", w.users["treasury"])
    assert (tmp_path / "ex" / b.export_key).read_bytes() == content
    assert posting.download_stored(w.db, b, w.users["treasury"])[1] == content


def test_unexported_batch_has_nothing_to_download(w):
    b = approved_batch(w)
    with pytest.raises(posting.PostingError, match="not been exported"):
        posting.download_stored(w.db, b, w.users["treasury"])


# ---------------------------------------------------------------------------------------------- integration: bank landing zone
GOOD = b"Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n01/10/26,NEFT-HDFCN26274000111-ACME-INV 1,,1000.00,5000.00\n"


def test_bank_landing_zone_in_a_bucket_is_ingested_archived_and_failures_stay(w, bucket, monkeypatch):
    c = use(monkeypatch, cfg())
    monkeypatch.setattr(feeds, "settings", c)
    for k, v in {"hdfc/a.csv": GOOD, "hdfc/broken.csv": b"not,a,statement\n1,2,3\n", "hdfc/sub/ignored.csv": GOOD, "hdfc/notes.txt": b"hi"}.items():
        bucket.put_object(Bucket="cash-bucket", Key=f"cashapp/feeds/{k}", Body=v)
    w.bank.feed = {"type": "folder_drop", "path": "hdfc", "pattern": "*.csv"}
    res = feeds.sync_bank(w.db, w.bank, "t", None)
    assert res["inserted"] == 1 and len(res["errors"]) == 1
    keys = {o["Key"] for o in bucket.list_objects_v2(Bucket="cash-bucket")["Contents"]}
    assert "cashapp/feeds/hdfc/broken.csv" in keys and "cashapp/feeds/hdfc/sub/ignored.csv" in keys and "cashapp/feeds/hdfc/notes.txt" in keys
    assert "cashapp/feeds/hdfc/a.csv" not in keys and any(k.startswith("cashapp/feeds/hdfc/processed/") and k.endswith("_a.csv") for k in keys)
    assert w.db.query(PaymentTransaction).count() == 1
    again = feeds.sync_bank(w.db, w.bank, "t", None)
    assert again["inserted"] == 0, "the processed file is not picked up twice"


def test_landing_zone_path_cannot_escape_the_prefix(w, bucket, monkeypatch):
    c = use(monkeypatch, cfg())
    monkeypatch.setattr(feeds, "settings", c)
    w.bank.feed = {"type": "folder_drop", "path": "../other"}
    with pytest.raises(feeds.FeedError, match="escapes"):
        feeds.sync_bank(w.db, w.bank, "t", None)


# ---------------------------------------------------------------------------------------------- API
def test_storage_api_status_probe_download_and_roles(client, db, bucket, monkeypatch):
    from app.seed.demo import DEMO_PASSWORD, seed_demo

    e = seed_demo(db)["entity_id"]
    db.commit()
    use(monkeypatch, cfg())
    an, adm, tr, mgr = (login(client, f"{u}@meridian.example", DEMO_PASSWORD) for u in ("analyst", "admin", "treasury", "manager"))
    st = client.get("/api/v1/storage", headers=an).json()
    assert st["backend"] == "s3" and st["bucket"] == "cash-bucket" and st["problems"] == [] and st["encryption"] == "aws:kms"
    assert client.post("/api/v1/storage/test", headers=an).status_code == 403
    probe = client.post("/api/v1/storage/test", headers=adm).json()
    assert probe["exports"]["ok"] and probe["feeds"]["ok"]
    b = client.post("/api/v1/posting/batches", json={"entity_id": e}, headers=mgr).json()
    assert client.get(f"/api/v1/posting/batches/{b['id']}/download", headers=tr).status_code == 409        # not exported yet
    client.post(f"/api/v1/posting/batches/{b['id']}/release", headers=tr)
    ex = client.post(f"/api/v1/posting/batches/{b['id']}/export", json={"connector": "generic_csv"}, headers=tr)
    assert ex.status_code == 200
    dl = client.get(f"/api/v1/posting/batches/{b['id']}/download", headers=tr)
    assert dl.status_code == 200 and dl.content == ex.content and dl.headers["x-checksum-sha256"] == ex.headers["x-checksum-sha256"]
    assert client.get(f"/api/v1/posting/batches/{b['id']}/download", headers=an).status_code == 403          # analysts do not download exports
    assert client.get(f"/api/v1/posting/batches/{b['id']}", headers=tr).json()["stored"] is True
    actions = [r["action"] for r in client.get("/api/v1/audit?action=batch.", headers=login(client, "auditor@meridian.example", DEMO_PASSWORD)).json()["items"]]
    assert "batch.downloaded" in actions and "batch.exported" in actions


# ---------------------------------------------------------------------------------------------- Object Lock (write-once exports)
@pytest.fixture()
def locked_bucket():
    with mock_aws():
        c = boto3.client("s3", region_name="ap-south-1")
        c.create_bucket(Bucket="cash-bucket", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"}, ObjectLockEnabledForBucket=True)
        yield c


def test_exports_are_written_with_compliance_retention_but_feeds_are_not(locked_bucket, monkeypatch):
    from datetime import datetime, timedelta, timezone

    c = use(monkeypatch, cfg(s3_object_lock_mode="COMPLIANCE", s3_retention_days=2555))
    storage.get_store("exports", c).put("1/7/batch.csv", b"data", "text/csv")
    storage.get_store("feeds", c).put("hdfc/stmt.csv", b"data", "text/csv")
    h = locked_bucket.head_object(Bucket="cash-bucket", Key="cashapp/exports/1/7/batch.csv")
    assert h["ObjectLockMode"] == "COMPLIANCE"
    assert abs((h["ObjectLockRetainUntilDate"] - datetime.now(timezone.utc)).days - 2555) <= 1
    assert "ObjectLockMode" not in locked_bucket.head_object(Bucket="cash-bucket", Key="cashapp/feeds/hdfc/stmt.csv")


def test_a_locked_export_cannot_be_deleted_before_its_retention_ends(locked_bucket, monkeypatch):
    c = use(monkeypatch, cfg(s3_object_lock_mode="COMPLIANCE", s3_retention_days=30))
    s = storage.get_store("exports", c)
    s.put("1/7/batch.csv", b"data", "text/csv")
    v = locked_bucket.list_object_versions(Bucket="cash-bucket", Prefix="cashapp/exports/1/7/batch.csv")["Versions"][0]["VersionId"]
    with pytest.raises(Exception, match="AccessDenied|locked|retention"):
        locked_bucket.delete_object(Bucket="cash-bucket", Key="cashapp/exports/1/7/batch.csv", VersionId=v)


def test_the_storage_probe_still_works_and_leaves_no_locked_residue(locked_bucket, monkeypatch):
    c = use(monkeypatch, cfg(s3_object_lock_mode="COMPLIANCE", s3_retention_days=30))
    res = storage.probe(storage.get_store("exports", c))
    assert res["ok"] is True and res["object_lock"] == {"mode": "COMPLIANCE", "retention_days": 30, "enabled": True}
    assert locked_bucket.list_objects_v2(Bucket="cash-bucket", Prefix="cashapp/exports/_probe").get("KeyCount", 0) == 0


def test_object_lock_on_a_bucket_without_it_fails_loudly_and_settings_are_validated(bucket, monkeypatch):
    c = use(monkeypatch, cfg(s3_object_lock_mode="GOVERNANCE", s3_retention_days=7))
    with pytest.raises(storage.StorageError, match="Object Lock enabled"):
        storage.get_store("exports", c).put("1/7/batch.csv", b"data")
    assert storage.get_store("exports", c).object_lock_status()["enabled"] is False
    assert any("S3_OBJECT_LOCK_MODE" in e for e in storage.validate_settings(cfg(s3_object_lock_mode="LEGAL")))
    assert any("S3_RETENTION_DAYS" in e for e in storage.validate_settings(cfg(s3_object_lock_mode="COMPLIANCE", s3_retention_days=0)))
    assert storage.validate_settings(cfg(s3_object_lock_mode="COMPLIANCE", s3_retention_days=365)) == []
