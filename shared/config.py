"""Frozen, validated runtime settings — the only module that reads the environment.

Every setting is read exactly once at import from ``.env`` and ``os.environ``.
Secrets are typed ``SecretStr`` so their values never leak through ``repr``,
``str``, logging, or serialisation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode


from shared.constants import (
    DEFAULT_LLM_MAX_ATTEMPTS,
    DEFAULT_LLM_MODEL_ARCHITECT,
    DEFAULT_LLM_MODEL_PM,
    DEFAULT_LLM_MODEL_SCREENING,
    DEFAULT_LLM_TEMPERATURE,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_PENDING_PITCH_TTL_SECONDS,
    DEFAULT_PITCH_COIN_LIMIT,
    DEFAULT_SPRINT_CADENCE_SECONDS,
    DEFAULT_UPVOTE_THRESHOLD,
)


class Settings(BaseSettings):
    """Immutable, validated application settings.

    Required keys have no default — pydantic-settings raises
    ``ValidationError`` at construction when they are absent.
    """

    model_config = {  # type: ignore[assignment]
        "frozen": True,
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        # Extra env vars are silently ignored, not errors.
        "extra": "ignore",
    }

    # ------------------------------------------------------------------
    # Required — no defaults
    # ------------------------------------------------------------------
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: SecretStr
    REDIS_URL: str
    LLM_BASE_URL: str
    LLM_API_KEY: SecretStr
    TARGET_PROMPT_DIR: Path
    COMPILE_COMMAND: str
    RENDER_WEBHOOK_SECRET: SecretStr
    SANDBOX_ALLOWED_HOSTS: Annotated[tuple[str, ...], NoDecode]
    FORUM_ORIGIN: str

    # ------------------------------------------------------------------
    # Optional
    # ------------------------------------------------------------------
    SUPABASE_JWT_SECRET: SecretStr | None = None
    RENDER_API_KEY: SecretStr | None = None
    RENDER_SERVICE_ID: str | None = None
    DEV_MODE: bool = False

    # Tunables — fallbacks imported from shared/constants.py (R2)
    PITCH_COIN_LIMIT: int = DEFAULT_PITCH_COIN_LIMIT
    UPVOTE_THRESHOLD: int = DEFAULT_UPVOTE_THRESHOLD
    SPRINT_CADENCE_SECONDS: int = DEFAULT_SPRINT_CADENCE_SECONDS
    MAX_RETRIES: int = DEFAULT_MAX_RETRIES
    PENDING_PITCH_TTL_SECONDS: int = DEFAULT_PENDING_PITCH_TTL_SECONDS

    # LLM tunables — fallbacks from shared/constants.py (R2, R11, R13)
    LLM_MODEL_SCREENING: str = DEFAULT_LLM_MODEL_SCREENING
    LLM_MODEL_PM: str = DEFAULT_LLM_MODEL_PM
    LLM_MODEL_ARCHITECT: str = DEFAULT_LLM_MODEL_ARCHITECT
    LLM_TEMPERATURE: float = DEFAULT_LLM_TEMPERATURE
    LLM_TIMEOUT_SECONDS: int = DEFAULT_LLM_TIMEOUT_SECONDS
    LLM_MAX_ATTEMPTS: int = DEFAULT_LLM_MAX_ATTEMPTS

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("SANDBOX_ALLOWED_HOSTS", mode="before")
    @classmethod
    def _parse_allowed_hosts(cls, v: object) -> tuple[str, ...]:
        """Parse a comma-separated string into a tuple (R5, R10)."""
        if isinstance(v, str):
            hosts = tuple(h.strip() for h in v.split(",") if h.strip())
        elif isinstance(v, (list, tuple)):
            hosts = tuple(str(h).strip() for h in v if str(h).strip())
        else:
            raise ValueError(
                "SANDBOX_ALLOWED_HOSTS must be a comma-separated string"
            )
        if not hosts:
            raise ValueError(
                "SANDBOX_ALLOWED_HOSTS must contain at least one host"
            )
        return hosts

    @field_validator("FORUM_ORIGIN")
    @classmethod
    def _reject_wildcard_origin(cls, v: str) -> str:
        """Reject ``*`` — writes carry a bearer token (R9)."""
        if v.strip() == "*":
            raise ValueError(
                "FORUM_ORIGIN must be an exact origin, not '*'; "
                "wildcard CORS is unsafe when requests carry bearer tokens"
            )
        return v

    @field_validator(
        "PITCH_COIN_LIMIT",
        "UPVOTE_THRESHOLD",
        "SPRINT_CADENCE_SECONDS",
        "MAX_RETRIES",
        "PENDING_PITCH_TTL_SECONDS",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_ATTEMPTS",
    )
    @classmethod
    def _must_be_positive(cls, v: int, info: object) -> int:  # noqa: ANN401
        """Reject non-positive tunables at import (R7, R12)."""
        if v <= 0:
            # info is a FieldValidationInfo but we only need the field name
            field_name = getattr(info, "field_name", "tunable")
            raise ValueError(f"{field_name} must be positive, got {v}")
        return v

    @field_validator("LLM_TEMPERATURE")
    @classmethod
    def _temperature_in_range(cls, v: float) -> float:
        """Reject LLM_TEMPERATURE outside 0.0–2.0 at import (R12)."""
        if v < 0.0 or v > 2.0:
            raise ValueError(
                f"LLM_TEMPERATURE must be between 0.0 and 2.0 inclusive, got {v}"
            )
        return v

    @model_validator(mode="after")
    def _require_jwt_secret_in_prod(self) -> Settings:
        """When ``DEV_MODE`` is false, ``SUPABASE_JWT_SECRET`` is required (R8)."""
        if not self.DEV_MODE and self.SUPABASE_JWT_SECRET is None:
            raise ValueError(
                "SUPABASE_JWT_SECRET is required when DEV_MODE is false; "
                "set DEV_MODE=true only for local development without JWT verification"
            )
        return self


# ------------------------------------------------------------------
# Module-level singleton (R4) — import triggers validation (R1)
# ------------------------------------------------------------------

settings: Final[Settings] = Settings()  # type: ignore[call-arg]
