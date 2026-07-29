"""Contract tests for `shared/config.py` (settings, validation, secrets)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest
from pydantic import SecretStr, ValidationError

from shared import config as config_module
from shared.config import Settings
from shared.constants import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_PENDING_PITCH_TTL_SECONDS,
    DEFAULT_PITCH_COIN_LIMIT,
    DEFAULT_SPRINT_CADENCE_SECONDS,
    DEFAULT_UPVOTE_THRESHOLD,
)

REQUIRED = {
    "SUPABASE_URL": "https://x.supabase.co",
    "SUPABASE_SERVICE_KEY": "k",
    "REDIS_URL": "redis://localhost:6379/0",
    "LLM_BASE_URL": "https://llm",
    "LLM_API_KEY": "k",
    # Required since US-09: the compiler passes it to the compile subprocess,
    # which runs in the target repo where no .env exists.
    "TOKENROUTER_API_KEY": "tr",
    "TARGET_PROMPT_DIR": "/tmp/t",
    "COMPILE_COMMAND": "echo",
    "RENDER_WEBHOOK_SECRET": "s",
    "SANDBOX_ALLOWED_HOSTS": "*.onrender.com",
    "FORUM_ORIGIN": "http://localhost:5173",
    "SUPABASE_JWT_SECRET": "j",
}


def _build(**overrides: str | None) -> Settings:
    """Construct Settings from an explicit env mapping only.

    `_env_file=None` disables the .env file but NOT `os.environ`, which the root
    conftest populates — so the process environment has to be replaced wholesale
    for a "missing key" test to mean anything.
    """
    env = {**REQUIRED, **overrides}
    env = {k: v for k, v in env.items() if v is not None}
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings(_env_file=None)  # type: ignore[call-arg]


def _build_missing(*drop: str) -> Settings:
    """Build with specific required keys absent from the environment."""
    env = {k: v for k, v in REQUIRED.items() if k not in drop}
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings(_env_file=None)  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# R1 — required keys
# --------------------------------------------------------------------------

def test_r1_missing_required_key_fails() -> None:
    with pytest.raises(ValidationError):
        _build_missing("SUPABASE_URL")


def test_r1_error_names_every_missing_key_not_just_the_first() -> None:
    """R1 is explicit: one error naming every missing key."""
    with pytest.raises(ValidationError) as exc:
        _build_missing("SUPABASE_URL", "LLM_API_KEY", "COMPILE_COMMAND")
    reported = {e["loc"][0] for e in exc.value.errors()}
    assert {"SUPABASE_URL", "LLM_API_KEY", "COMPILE_COMMAND"} <= reported


# --------------------------------------------------------------------------
# R2 — fallbacks come from the DEFAULT_* constants
# --------------------------------------------------------------------------

def test_r2_tunables_fall_back_to_shared_constants() -> None:
    s = _build()
    assert s.PITCH_COIN_LIMIT == DEFAULT_PITCH_COIN_LIMIT
    assert s.UPVOTE_THRESHOLD == DEFAULT_UPVOTE_THRESHOLD
    assert s.SPRINT_CADENCE_SECONDS == DEFAULT_SPRINT_CADENCE_SECONDS
    assert s.MAX_RETRIES == DEFAULT_MAX_RETRIES
    assert s.PENDING_PITCH_TTL_SECONDS == DEFAULT_PENDING_PITCH_TTL_SECONDS


def test_r2_tunables_are_overridable() -> None:
    assert _build(PITCH_COIN_LIMIT="9").PITCH_COIN_LIMIT == 9


# --------------------------------------------------------------------------
# R3 — secrets never leak through repr/str
# --------------------------------------------------------------------------

def test_r3_secrets_are_masked() -> None:
    s = _build(SUPABASE_SERVICE_KEY="super-secret-value")
    assert isinstance(s.SUPABASE_SERVICE_KEY, SecretStr)
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" not in str(s)
    assert "super-secret-value" not in str(s.SUPABASE_SERVICE_KEY)
    assert s.SUPABASE_SERVICE_KEY.get_secret_value() == "super-secret-value"


# --------------------------------------------------------------------------
# R4 — frozen
# --------------------------------------------------------------------------

def test_r4_settings_is_frozen() -> None:
    s = _build()
    with pytest.raises(Exception):
        s.DEV_MODE = True  # type: ignore[misc]


# --------------------------------------------------------------------------
# R5 — SANDBOX_ALLOWED_HOSTS parsing
# --------------------------------------------------------------------------

def test_r5_allowed_hosts_parses_csv_to_tuple() -> None:
    s = _build(SANDBOX_ALLOWED_HOSTS="*.onrender.com, example.com")
    assert isinstance(s.SANDBOX_ALLOWED_HOSTS, tuple)
    assert "*.onrender.com" in s.SANDBOX_ALLOWED_HOSTS
    assert "example.com" in s.SANDBOX_ALLOWED_HOSTS


def test_r5_empty_allow_list_is_rejected() -> None:
    """An empty allow-list would silently permit nothing (US-10)."""
    with pytest.raises(ValidationError):
        _build(SANDBOX_ALLOWED_HOSTS="")


# --------------------------------------------------------------------------
# R6 / R8 — DEV_MODE and the JWT secret it gates
# --------------------------------------------------------------------------

def test_r6_dev_mode_defaults_false() -> None:
    """An unset environment must enforce JWT verification."""
    assert _build().DEV_MODE is False


def test_r8_jwt_secret_required_when_dev_mode_off() -> None:
    """Fail at startup, not on the first authenticated request."""
    with pytest.raises(ValidationError):
        _build(DEV_MODE="false", SUPABASE_JWT_SECRET=None)


def test_r8_jwt_secret_optional_when_dev_mode_on() -> None:
    s = _build(DEV_MODE="true", SUPABASE_JWT_SECRET=None)
    assert s.DEV_MODE is True
    assert s.SUPABASE_JWT_SECRET is None


# --------------------------------------------------------------------------
# R7 — non-positive tunables rejected at import, not first use
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "key",
    ["PITCH_COIN_LIMIT", "UPVOTE_THRESHOLD", "SPRINT_CADENCE_SECONDS",
     "MAX_RETRIES", "PENDING_PITCH_TTL_SECONDS"],
)
@pytest.mark.parametrize("bad", ["0", "-1"])
def test_r7_non_positive_tunable_rejected(key: str, bad: str) -> None:
    with pytest.raises(ValidationError):
        _build(**{key: bad})


# --------------------------------------------------------------------------
# R9 — wildcard origin rejected
# --------------------------------------------------------------------------

def test_r9_wildcard_origin_rejected() -> None:
    """Writes carry a bearer token, so `*` is never a valid origin."""
    with pytest.raises(ValidationError):
        _build(FORUM_ORIGIN="*")


def test_r9_normal_origin_accepted() -> None:
    assert _build(FORUM_ORIGIN="https://forum.example.com").FORUM_ORIGIN == "https://forum.example.com"


# --------------------------------------------------------------------------
# module-level instance
# --------------------------------------------------------------------------

def test_module_exposes_single_settings_instance() -> None:
    assert isinstance(config_module.settings, Settings)


def test_target_prompt_dir_is_a_path() -> None:
    assert isinstance(_build().TARGET_PROMPT_DIR, Path)


# --------------------------------------------------------------------------
# R13 — one model setting per agent role
# --------------------------------------------------------------------------

def test_r13_each_role_has_its_own_model_setting() -> None:
    from shared.constants import (
        DEFAULT_LLM_MODEL_ARCHITECT,
        DEFAULT_LLM_MODEL_PM,
        DEFAULT_LLM_MODEL_SCREENING,
    )

    s = _build()
    assert s.LLM_MODEL_SCREENING == DEFAULT_LLM_MODEL_SCREENING
    assert s.LLM_MODEL_PM == DEFAULT_LLM_MODEL_PM
    assert s.LLM_MODEL_ARCHITECT == DEFAULT_LLM_MODEL_ARCHITECT


def test_r13_one_role_can_move_without_touching_the_others() -> None:
    """The whole point of separate pins."""
    s = _build(LLM_MODEL_PM="some-other-model")
    assert s.LLM_MODEL_PM == "some-other-model"
    assert s.LLM_MODEL_SCREENING != "some-other-model"
    assert s.LLM_MODEL_ARCHITECT != "some-other-model"


def test_r13_env_pins_are_honoured() -> None:
    s = _build(LLM_MODEL_SCREENING="m-screen", LLM_MODEL_PM="m-pm",
               LLM_MODEL_ARCHITECT="m-arch", LLM_TEMPERATURE="0.7")
    assert (s.LLM_MODEL_SCREENING, s.LLM_MODEL_PM, s.LLM_MODEL_ARCHITECT) == ("m-screen", "m-pm", "m-arch")
    assert s.LLM_TEMPERATURE == 0.7
