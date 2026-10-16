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
    export_dir: str = field(default_factory=lambda: os.getenv("EXPORT_DIR", "./exports"))


settings = Settings()


def assert_safe_for_production() -> None:
    if settings.env == "prod" and settings.jwt_secret.startswith("dev-only"):
        raise RuntimeError("JWT_SECRET must be set in production")
    if settings.env == "prod" and settings.allow_demo:
        raise RuntimeError("ALLOW_DEMO must be 0 in production")
