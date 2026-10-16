"""Object storage for exports and landing-zone files. Two backends behind one tiny interface so nothing else cares where bytes live.

  local : a directory (single VM, or a shared volume). Atomic writes; keys cannot escape the root.
  s3    : any S3-compatible service. Server-side encryption, SHA-256 integrity metadata verified on read, bounded retries/timeouts,
          and — in data-residency strict mode — an Indian region (or an allow-listed endpoint) plus an explicit encryption choice are REQUIRED.

Credentials: the standard AWS credential chain (instance/pod role, or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY in the environment). Never in code or config files.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .config import INDIA_REGIONS, check_egress


class StorageError(RuntimeError):
    pass


@dataclass
class Obj:
    key: str
    size: int
    modified: datetime


_BAD = re.compile(r"(^|/)\.\.?(/|$)|\\|\x00|//")


def clean_key(key: str) -> str:
    """Reject anything that could escape a prefix or confuse a backend. Keys are relative, '/'-separated, no '.'/'..' segments."""
    if not key or key.startswith("/") or len(key) > 512 or _BAD.search(key):
        raise StorageError(f"invalid storage key {key[:60]!r}")
    return key


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalStore:
    backend = "local"

    def __init__(self, root: str):
        self.root = Path(os.path.realpath(root))

    def _path(self, key: str) -> Path:
        p = Path(os.path.realpath(self.root / clean_key(key)))
        if p != self.root and self.root not in p.parents:
            raise StorageError("storage key escapes the storage root")
        return p

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream", sha256: str | None = None) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, p)           # atomic: readers never see a half-written export
        finally:
            tmp.unlink(missing_ok=True)

    def get(self, key: str, verify_sha256: str | None = None) -> bytes:
        p = self._path(key)
        if not p.is_file():
            raise StorageError(f"object not found: {key}")
        data = p.read_bytes()
        if verify_sha256 and sha256_hex(data) != verify_sha256:
            raise StorageError(f"integrity check failed for {key}")
        return data

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str = "") -> list[Obj]:
        base = self._path(prefix.rstrip("/")) if prefix.strip("/") else self.root
        if not base.is_dir():
            return []
        out = []
        for f in sorted(base.rglob("*")):
            if f.is_file() and not f.name.endswith(".tmp"):
                st = f.stat()
                out.append(Obj(f.relative_to(self.root).as_posix(), st.st_size, datetime.fromtimestamp(st.st_mtime, timezone.utc)))
        return out

    def move(self, src: str, dst: str) -> None:
        s, d = self._path(src), self._path(dst)
        if not s.is_file():
            raise StorageError(f"object not found: {src}")
        d.parent.mkdir(parents=True, exist_ok=True)
        os.replace(s, d)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def describe(self) -> dict:
        return {"backend": "local", "location": str(self.root)}


class S3Store:
    backend = "s3"

    def __init__(self, cfg, area: str, client=None):
        self.cfg, self.prefix = cfg, "/".join(x for x in (cfg.s3_prefix, area) if x) + "/"
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3", region_name=self.cfg.s3_region or None, endpoint_url=self.cfg.s3_endpoint_url or None,
                config=Config(retries={"max_attempts": 4, "mode": "standard"}, connect_timeout=5, read_timeout=30,
                              s3={"addressing_style": "path" if self.cfg.s3_path_style else "auto"}, signature_version="s3v4"))
        return self._client

    def _k(self, key: str) -> str:
        return self.prefix + clean_key(key)

    def _sse(self) -> dict:
        sse = self.cfg.s3_sse
        if sse == "aws:kms":
            out = {"ServerSideEncryption": "aws:kms"}
            if self.cfg.s3_kms_key_id:
                out["SSEKMSKeyId"] = self.cfg.s3_kms_key_id
            return out
        return {"ServerSideEncryption": "AES256"} if sse == "AES256" else {}

    def _call(self, what: str, fn, **kw):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return fn(**kw)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            hint = {"NoSuchBucket": "the bucket does not exist", "AccessDenied": "access denied (check the role/credentials and bucket policy)", "InvalidAccessKeyId": "invalid access key",
                    "SignatureDoesNotMatch": "bad credentials", "404": "not found", "NoSuchKey": "not found"}.get(code, code or "request failed")
            raise StorageError(f"object storage {what} failed: {hint}") from e
        except BotoCoreError as e:
            raise StorageError(f"object storage {what} failed: {e}") from e

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream", sha256: str | None = None) -> None:
        self._call("write", self.client.put_object, Bucket=self.cfg.s3_bucket, Key=self._k(key), Body=data, ContentType=content_type,
                   Metadata={"sha256": sha256 or sha256_hex(data)}, **self._sse())

    def get(self, key: str, verify_sha256: str | None = None) -> bytes:
        r = self._call("read", self.client.get_object, Bucket=self.cfg.s3_bucket, Key=self._k(key))
        data = r["Body"].read()
        want = verify_sha256 or (r.get("Metadata") or {}).get("sha256")
        if want and sha256_hex(data) != want:
            raise StorageError(f"integrity check failed for {key}")
        return data

    def exists(self, key: str) -> bool:
        try:
            self._call("lookup", self.client.head_object, Bucket=self.cfg.s3_bucket, Key=self._k(key))
            return True
        except StorageError as e:
            if "not found" in str(e):
                return False
            raise

    def list(self, prefix: str = "") -> list[Obj]:
        full = self.prefix + (clean_key(prefix.rstrip("/")) + "/" if prefix.strip("/") else "")
        out, token = [], None
        while True:
            kw = {"Bucket": self.cfg.s3_bucket, "Prefix": full, **({"ContinuationToken": token} if token else {})}
            r = self._call("list", self.client.list_objects_v2, **kw)
            out += [Obj(o["Key"][len(self.prefix):], o["Size"], o["LastModified"]) for o in r.get("Contents", []) if not o["Key"].endswith("/")]
            if not r.get("IsTruncated"):
                return sorted(out, key=lambda o: o.key)
            token = r["NextContinuationToken"]

    def move(self, src: str, dst: str) -> None:
        self._call("copy", self.client.copy_object, Bucket=self.cfg.s3_bucket, Key=self._k(dst), CopySource={"Bucket": self.cfg.s3_bucket, "Key": self._k(src)},
                   MetadataDirective="COPY", **self._sse())
        self._call("delete", self.client.delete_object, Bucket=self.cfg.s3_bucket, Key=self._k(src))

    def delete(self, key: str) -> None:
        self._call("delete", self.client.delete_object, Bucket=self.cfg.s3_bucket, Key=self._k(key))

    def describe(self) -> dict:
        return {"backend": "s3", "bucket": self.cfg.s3_bucket, "prefix": self.prefix, "region": self.cfg.s3_region or None, "endpoint": self.cfg.s3_endpoint_url or None,
                "encryption": self.cfg.s3_sse or "none declared"}


def s3_host(cfg) -> str:
    return urlparse(cfg.s3_endpoint_url).hostname if cfg.s3_endpoint_url else f"s3.{cfg.s3_region}.amazonaws.com"


def validate_settings(cfg) -> list[str]:
    """Problems that must stop the service from starting (empty list = fine)."""
    from . import config as _c

    if cfg.storage_backend == "local":
        return []
    if cfg.storage_backend != "s3":
        return [f"STORAGE_BACKEND must be 'local' or 's3', got '{cfg.storage_backend}'"]
    errs = []
    if not cfg.s3_bucket:
        errs.append("S3_BUCKET is required when STORAGE_BACKEND=s3")
    if cfg.s3_sse not in ("", "AES256", "aws:kms", "none"):
        errs.append("S3_SSE must be AES256, aws:kms or none")
    if cfg.s3_sse == "aws:kms" and not cfg.s3_kms_key_id and cfg.residency_strict:
        errs.append("S3_KMS_KEY_ID is required with aws:kms in strict mode (use a key created in the Indian region)")
    if cfg.residency_strict:
        if not cfg.s3_endpoint_url and cfg.s3_region not in INDIA_REGIONS:
            errs.append(f"strict data-residency: S3_REGION must be an Indian region (got '{cfg.s3_region or 'unset'}')")
        if not cfg.s3_sse:
            errs.append("strict data-residency: set S3_SSE explicitly (AES256 or aws:kms; 'none' only if the store encrypts at rest itself)")
        host = s3_host(cfg)
        if not _c.egress_allowed_for(cfg, host):
            errs.append(f"strict data-residency: object-storage host '{host}' is not in ALLOWED_EGRESS_HOSTS")
    return errs


def get_store(area: str, cfg=None, client=None):
    """area: 'exports' | 'feeds'."""
    from .config import settings

    cfg = cfg or settings
    errs = validate_settings(cfg)
    if errs:
        raise StorageError("; ".join(errs))
    if cfg.storage_backend == "s3":
        return S3Store(cfg, area, client)
    return LocalStore(cfg.export_dir if area == "exports" else cfg.feed_root)


def probe(store) -> dict:
    """Write, read back, verify and delete a throw-away object: proves credentials, bucket, encryption headers and permissions end to end."""
    import time

    key = f"_probe/{uuid.uuid4().hex}.txt"
    payload = f"cashapp probe {datetime.now(timezone.utc).isoformat()}".encode()
    t0 = time.perf_counter()
    store.put(key, payload, "text/plain", sha256_hex(payload))
    ok = store.get(key, sha256_hex(payload)) == payload
    store.delete(key)
    return {"ok": ok, "ms": round((time.perf_counter() - t0) * 1000), **store.describe()}
