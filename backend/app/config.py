"""Application configuration.

All secrets come from environment variables - never hardcode credentials.
See .env.example for the full list of supported variables.
"""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- General -----------------------------------------------------------
    app_name: str = "RouteShield"
    environment: str = "development"  # development | staging | production
    database_url: str = f"sqlite:///{BACKEND_DIR / 'data' / 'routeshield.db'}"

    # --- Auth (manager account) --------------------------------------------
    manager_username: str = Field(
        default="fleet.manager",
        validation_alias=AliasChoices("ROUTESHIELD_MANAGER_USERNAME", "MANAGER_USERNAME"),
    )
    # If unset, a random password is generated at startup and logged ONCE.
    # Never hardcode production passwords; set this via environment.
    manager_password: str = Field(
        default="",
        validation_alias=AliasChoices("ROUTESHIELD_MANAGER_PASSWORD", "MANAGER_PASSWORD"),
    )
    auth_secret: str = Field(
        default="",  # HMAC signing key for session tokens
        validation_alias=AliasChoices("ROUTESHIELD_AUTH_SECRET", "AUTH_SECRET"),
    )
    token_ttl_minutes: int = 480

    # --- NHTSA integration ---------------------------------------------------
    nhtsa_api_base: str = "https://api.nhtsa.gov"
    nhtsa_static_base: str = "https://static.nhtsa.gov"
    vpic_api_base: str = "https://vpic.nhtsa.dot.gov"
    http_timeout_seconds: float = 30.0
    # Minimum spacing between outbound vPIC VIN decode requests (rate limit).
    vpic_min_request_interval_seconds: float = 2.0
    # When live NHTSA retrieval fails, fall back to the manually reviewed
    # fixture (explicitly labelled in the UI). Disable in strict environments.
    allow_offline_fallback: bool = True
    report_cache_dir: Path = BACKEND_DIR / "data" / "reports"
    reviewed_campaigns_dir: Path = BACKEND_DIR / "data" / "campaigns"

    # --- Vultr Serverless Inference (LLM) -------------------------------------
    # The LLM only READS evidence and PRODUCES structured extraction output.
    # It never writes operational records and never classifies vehicles.
    vultr_inference_api_key: str = ""
    vultr_inference_base_url: str = "https://api.vultrinference.com/v1"
    vultr_inference_model: str = ""
    llm_enabled: bool = True

    # --- Frontend -------------------------------------------------------------
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    serve_frontend_dir: str = ""  # optional: path to built frontend dist/

    @field_validator("database_url", mode="after")
    @classmethod
    def _default_db_url(cls, v: str) -> str:
        # An empty DATABASE_URL in .env must not break startup.
        if not v or not v.strip():
            return f"sqlite:///{BACKEND_DIR / 'data' / 'routeshield.db'}"
        return v.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def resolved_auth_secret(self) -> str:
        """Return a stable auth secret, generating one if not configured.

        A generated secret lives only for this process lifetime (tokens are
        invalidated on restart). Production deployments MUST set AUTH_SECRET.
        """
        if self.auth_secret:
            return self.auth_secret
        if not hasattr(self, "_generated_secret"):
            object.__setattr__(self, "_generated_secret", secrets.token_hex(32))
        return self._generated_secret  # type: ignore[attr-defined]


@lru_cache
def get_settings() -> Settings:
    return Settings()
