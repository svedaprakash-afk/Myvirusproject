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
    allow_demo: bool = field(default_factory=lambda: os.getenv("ALLOW_DEMO", "1") == "1")
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","))
    )
    # Optional LLM remittance extraction. Matching decisions never depend on it.
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-haiku-5-5"))
    # Data residency. In strict mode the process refuses to start outside an Indian region and refuses ANY outbound call
    # (LLM, webhooks, bank APIs, IMAP) to a host that is not explicitly allow-listed as in-region.
    residency_strict: bool = field(default_factory=lambda: os.getenv("DATA_RESIDENCY_STRICT", "0") == "1")
    deploy_region: str = field(default_factory=lambda: os.getenv("DEPLOY_REGION", ""))
    allowed_egress: tuple[str, ...] = field(default_factory=lambda: tuple(h.strip().lower() for h in os.getenv("ALLOWED_EGRESS_HOSTS", "").split(",") if h.strip()))
    # FX reference-rate feed (FBIL publishes the RBI reference rate). The URL is configuration, not code: it could not be verified
    # from the build environment, so the operator must point it at the live publication and confirm one fetch.
    fx_feed_url: str = field(default_factory=lambda: os.getenv("FX_FEED_URL", ""))
    fx_feed_currencies: tuple[str, ...] = field(default_factory=lambda: tuple(c.strip().upper() for c in os.getenv("FX_FEED_CURRENCIES", "USD,EUR,GBP,JPY").split(",") if c.strip()))
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


def egress_allowed(host: str) -> bool:
    if not settings.residency_strict:
        return True
    h = (host or "").lower().split(":")[0]
    return any(h == a or (a.startswith(".") and h.endswith(a)) for a in settings.allowed_egress)


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
