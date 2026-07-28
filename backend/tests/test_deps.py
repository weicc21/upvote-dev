"""Contract tests for `backend/deps.py` (the injection seam + error envelope)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend import deps

MODULE_SRC = Path(__file__).resolve().parents[2] / "backend" / "deps.py"


class _Req:
    """Minimal stand-in for starlette Request — deps only reads request.state."""

    class _State:
        pass

    def __init__(self, user_id: object = ...) -> None:
        self.state = self._State()
        if user_id is not ...:
            self.state.user_id = user_id


# --------------------------------------------------------------------------
# R1 / R8 — unbound providers raise RuntimeError, never a 401
# --------------------------------------------------------------------------

async def test_r1_get_supabase_unbound_raises_runtimeerror() -> None:
    with pytest.raises(RuntimeError) as exc:
        await deps.get_supabase()
    assert "get_supabase" in str(exc.value)


async def test_r1_get_redis_unbound_raises_runtimeerror() -> None:
    with pytest.raises(RuntimeError) as exc:
        await deps.get_redis()
    assert "get_redis" in str(exc.value)


async def test_r8_unbound_client_is_not_an_http_401() -> None:
    """A programmer error must not masquerade as a client error."""
    with pytest.raises(RuntimeError):
        await deps.get_supabase()
    with pytest.raises(RuntimeError):
        await deps.get_redis()


# --------------------------------------------------------------------------
# R2 / R3 — no module-level client globals, no import-time I/O
# --------------------------------------------------------------------------

def test_r2_no_module_level_client_global() -> None:
    tree = ast.parse(MODULE_SRC.read_text())
    banned = {"_supabase", "_redis", "supabase_client", "redis_client", "_client"}
    assigned = {
        t.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    assert not (assigned & banned), f"module-level client global: {assigned & banned}"


def test_r3_no_io_at_import() -> None:
    src = MODULE_SRC.read_text()
    for forbidden in ("create_client(", "redis.from_url(", "open(", "requests."):
        assert forbidden not in src, f"import-time I/O: {forbidden}"


# --------------------------------------------------------------------------
# R4 / R6 / R7 — caller identity
# --------------------------------------------------------------------------

def test_r4_current_user_returns_stamped_id() -> None:
    assert deps.get_current_user_id(_Req("user-123")) == "user-123"  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [None, "", ...])
def test_r4_current_user_401_when_absent_or_falsy(value: object) -> None:
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user_id(_Req(value))  # type: ignore[arg-type]
    assert exc.value.status_code == 401


def test_r6_optional_user_returns_none_and_never_raises() -> None:
    assert deps.get_optional_user_id(_Req(None)) is None  # type: ignore[arg-type]
    assert deps.get_optional_user_id(_Req("")) is None  # type: ignore[arg-type]
    assert deps.get_optional_user_id(_Req()) is None  # type: ignore[arg-type]
    assert deps.get_optional_user_id(_Req("u1")) == "u1"  # type: ignore[arg-type]


def test_r7_401_body_carries_no_caller_id() -> None:
    """A caller must not be able to probe which ids exist."""
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user_id(_Req(None))  # type: ignore[arg-type]
    assert "secret-user-id" not in str(exc.value.detail)


def test_r5_does_not_decode_tokens() -> None:
    src = MODULE_SRC.read_text()
    for forbidden in ("jwt.decode", "Authorization", "X-Dev-User", "headers"):
        assert forbidden not in src, f"deps must not do auth work: {forbidden}"


# --------------------------------------------------------------------------
# R9 / R10 — the frozen error envelope
# --------------------------------------------------------------------------

def test_r9_raise_error_uses_frozen_envelope() -> None:
    with pytest.raises(HTTPException) as exc:
        deps.raise_error(400, "validation_failed", "Title is too long.")
    detail = exc.value.detail
    assert exc.value.status_code == 400
    assert "error" in detail
    assert detail["error"]["code"] == "validation_failed"
    assert detail["error"]["message"] == "Title is too long."
    assert "detail" not in detail


def test_r10_429_carries_resets_at_as_sibling_of_error() -> None:
    """resets_at sits beside `error`, not inside it (openapi.yaml)."""
    with pytest.raises(HTTPException) as exc:
        deps.raise_error(429, "out_of_coins", "Back tomorrow.", resets_at="2026-07-28T00:00:00Z")
    detail = exc.value.detail
    assert detail["resets_at"] == "2026-07-28T00:00:00Z"
    assert "resets_at" not in detail["error"]


def test_get_settings_returns_the_shared_instance() -> None:
    from shared.config import settings

    assert deps.get_settings() is settings
