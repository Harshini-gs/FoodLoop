"""Application settings, loaded from environment variables.

Every external dependency has a deterministic development default, so the whole
application runs offline with no credentials. Nothing in here reads a secret
from source — only from the environment or a local .env file.
"""

from functools import lru_cache
from typing import Any, Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_empty_env_vars(cls, data: Any) -> Any:
        """Treat an empty environment variable as unset.

        `.env.example` documents optional settings as bare `SMTP_PORT=` lines,
        and dotenv reads those as the empty string rather than omitting them.
        For any non-string field that is a parse error, so drop empty values
        here and let the field's own default apply. Dropping rather than
        setting None matters: it lets `smtp_port` fall back to None while
        `cors_origins` keeps its string default.
        """
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value != ""}
        return data

    # ---------- application ----------
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    default_timezone: str = "Asia/Kolkata"

    # ---------- database ----------
    database_url: str = "postgresql+psycopg://foodloop:foodloop@localhost:5432/foodloop"
    database_url_sync: str = "postgresql+psycopg://foodloop:foodloop@localhost:5432/foodloop"
    db_echo: bool = False

    # ---------- auth ----------
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_alg: str = "HS256"
    access_token_ttl_min: int = 30

    # ---------- providers ----------
    llm_provider: Literal["mock", "anthropic", "openai", "ollama"] = "mock"
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_s: int = 20
    # Where a self-hosted model listens. Only used by the ollama provider.
    # Local inference on a CPU is slow, so raise LLM_TIMEOUT_S to ~120 with it.
    llm_base_url: str = "http://localhost:11434"

    routing_provider: Literal["simulated", "osrm", "ors"] = "simulated"
    osrm_base_url: str | None = None
    ors_api_key: str | None = None
    road_factor: float = 1.35

    geocoder_provider: Literal["simulated", "nominatim"] = "simulated"
    nominatim_base_url: str | None = None

    notify_provider: Literal["console", "smtp", "twilio"] = "console"
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None

    # ---------- safety ----------
    safety_ruleset_version: str = "demo_v1"

    # ---------- workflow bounds ----------
    reservation_hold_minutes: int = 20
    confirmation_timeout_minutes: int = 15
    max_candidate_attempts: int = 3
    max_node_retries: int = 3
    max_clarification_rounds: int = 2
    max_search_widenings: int = 2

    # ---------- matching ----------
    search_radius_meters_default: int = 8000
    search_radius_widen_factor: float = 1.5

    # ---------- sweeper ----------
    scheduler_enabled: bool = True
    scheduler_poll_seconds: int = 15

    # ---------- CORS ----------
    # Kept as a raw comma-separated string, NOT list[str]. pydantic-settings
    # runs json.loads() on any complex-typed field inside the env source,
    # before field validators execute, so a plain comma-separated value can
    # never reach a validator. Splitting in a property avoids the issue.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        """Parsed origins. An explicit list, never "*", even in dev."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @field_validator("jwt_secret")
    @classmethod
    def _reject_default_secret_in_prod(cls, v: str, info: object) -> str:
        # A soft guard. The prod check happens in main.py where app_env is known.
        if len(v) < 16:
            raise ValueError("JWT_SECRET must be at least 16 characters")
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == "prod"


@lru_cache
def get_settings() -> Settings:
    """Cached accessor. Use this everywhere rather than instantiating Settings()."""
    return Settings()
