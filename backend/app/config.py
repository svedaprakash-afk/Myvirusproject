"""Runtime configuration (12-factor: everything overridable by environment)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./cashapp.db"))
    jwt_secret: str = field(default_factory=lambda: os.getenv("JWT_SECRET", "dev-only-change-me-dev-only-change-me"))
    jwt_ttl_minutes: int = field(default_factory=lambda: int(os.getenv("JWT_TTL_MINUTES", "480")))
    env: str = field(default_factory=lambda: os.getenv("APP_ENV", "dev"))
    # Dev/test auto-upgrade the schema on start; production must NOT (migrations run as a separate, locked step) and refuses to start on a stale schema.
    auto_migrate: bool = field(default_factory=lambda: os.getenv("AUTO_MIGRATE", "0" if os.getenv("APP_ENV", "dev") == "prod" else "1") == "1")
    allow_demo: bool = field(default_factory=lambda: os.getenv("ALLOW_DEMO", "1") == "1")
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","))
    )
    # Optional LLM remittance extraction. Matching decisions never depend on it.
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-haiku-5-5"))
    # The LLM is consulted only when the local extractor's confidence is below this (rules are tried first, always).
    llm_fallback_below: float = field(default_factory=lambda: float(os.getenv("LLM_FALLBACK_BELOW", "0.75")))
    # Data residency. In strict mode the process refuses to start outside an Indian region and refuses ANY outbound call
    # (LLM, webhooks, bank APIs, IMAP) to a host that is not explicitly allow-listed as in-region.
    residency_strict: bool = field(default_factory=lambda: os.getenv("DATA_RESIDENCY_STRICT", "0") == "1")
    deploy_region: str = field(default_factory=lambda: os.getenv("DEPLOY_REGION", ""))
    allowed_egress: tuple[str, ...] = field(default_factory=lambda: tuple(h.strip().lower() for h in os.getenv("ALLOWED_EGRESS_HOSTS", "").split(",") if h.strip()))
    # FX reference-rate feed (FBIL publishes the RBI reference rate). The URL is configuration, not code: it could not be verified
    # from the build environment, so the operator must point it at the live publication and confirm one fetch.
    fx_feed_url: str = field(default_factory=lambda: os.getenv("FX_FEED_URL", ""))
    fx_feed_currencies: tuple[str, ...] = field(default_factory=lambda: tuple(c.strip().upper() for c in os.getenv("FX_FEED_CURRENCIES", "USD,EUR,JPY,GBP,CNY,AUD,CAD,CHF,HKD,SGD,AED").split(",") if c.strip()))
    # Optional cross-rate source for currencies FBIL does not publish (it publishes USD, EUR, GBP, JPY). The ECB's daily euro reference
    # rates are used with FBIL's EUR/INR to derive INR rates. Derived rates are NOT official reference rates and are labelled as such.
    # Currencies hard-pegged to USD by their central bank, as CCY:units-per-USD (AED has been 3.6725 since 1997). Derived from FBIL's USD/INR.
    # A peg CAN change: this is configuration so a revaluation is a config edit, and the derived rate is labelled as such.
    fx_pegs: tuple[str, ...] = field(default_factory=lambda: tuple(p.strip() for p in os.getenv("FX_PEGS", "AED:3.6725").split(",") if p.strip()))
    fx_cross_url: str = field(default_factory=lambda: os.getenv("FX_CROSS_FEED_URL", ""))
    # Object storage for posting exports and the bank-file landing zone. "local" = a directory (single VM / shared volume);
    # "s3" = any S3-compatible service (AWS S3, Oracle OCI, Ceph, MinIO, GCS interoperability, Indian providers' S3 APIs). Azure Blob is NOT S3-compatible.
    storage_backend: str = field(default_factory=lambda: os.getenv("STORAGE_BACKEND", "local").lower())
    s3_bucket: str = field(default_factory=lambda: os.getenv("S3_BUCKET", ""))
    s3_prefix: str = field(default_factory=lambda: os.getenv("S3_PREFIX", "cashapp").strip("/"))
    s3_endpoint_url: str = field(default_factory=lambda: os.getenv("S3_ENDPOINT_URL", ""))
    s3_region: str = field(default_factory=lambda: os.getenv("S3_REGION", ""))
    s3_sse: str = field(default_factory=lambda: os.getenv("S3_SSE", ""))            # AES256 | aws:kms | none (explicit: store encrypts at rest itself)
    s3_kms_key_id: str = field(default_factory=lambda: os.getenv("S3_KMS_KEY_ID", ""))
    s3_path_style: bool = field(default_factory=lambda: os.getenv("S3_PATH_STYLE", "0") == "1")   # MinIO/Ceph usually need path-style
    feed_root: str = field(default_factory=lambda: os.getenv("FEED_ROOT", "./feeds"))
    export_dir: str = field(default_factory=lambda: os.getenv("EXPORT_DIR", "./exports"))


settings = Settings()


INDIA_REGIONS = {"ap-south-1", "ap-south-2",                       # AWS Mumbai, Hyderabad
                 "centralindia", "southindia", "westindia",       # Azure
                 "asia-south1", "asia-south2",                    # GCP Mumbai, Delhi
                 "ap-mumbai-1", "ap-hyderabad-1",                   # Oracle Cloud
                 "in-west", "in-central", "on-prem-india"}        # generic / self-hosted labels


class EgressBlocked(PermissionError):
    pass


def egress_allowed_for(cfg, host: str) -> bool:
    """Is `host` reachable under `cfg`'s residency policy? (Evaluates the config it is given, so configuration can be validated before it is applied.)"""
    if not cfg.residency_strict:
        return True
    h = (host or "").lower().split(":")[0]
    return any(h == a or (a.startswith(".") and h.endswith(a)) for a in cfg.allowed_egress)


def egress_allowed(host: str) -> bool:
    return egress_allowed_for(settings, host)


def check_egress(url_or_host: str) -> None:
    from urllib.parse import urlparse

    host = urlparse(url_or_host).hostname if "://" in url_or_host else url_or_host
    if not egress_allowed(host or ""):
        raise EgressBlocked(f"Outbound connection to '{host}' blocked: data-residency strict mode allows only hosts in ALLOWED_EGRESS_HOSTS")


def assert_safe_for_production() -> None:
    if settings.residency_strict and settings.deploy_region not in INDIA_REGIONS:
        raise RuntimeError(f"DATA_RESIDENCY_STRICT=1 requires DEPLOY_REGION to be an Indian region {sorted(INDIA_REGIONS)}; got '{settings.deploy_region}'")
    if settings.env == "prod" and settings.jwt_secret.startswith("dev-only"):
        raise RuntimeError("JWT_SECRET must be set in production")
    if settings.env == "prod" and settings.allow_demo:
        raise RuntimeError("ALLOW_DEMO must be 0 in production")
