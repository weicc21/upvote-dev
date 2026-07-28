"""Dependency-injection seam for every route and every test override.

Providers for Supabase, Redis, settings, and caller identity live here.
Routes import only these functions; tests override only these functions.
No client is constructed, no socket is opened, no environment is read.
"""

from __future__ import annotations

from typing import Any, NoReturn

import redis.asyncio
from fastapi import HTTPException, Request
from pydantic import BaseModel
from supabase._async.client import AsyncClient

from shared.config import Settings, settings as _settings


# ---------------------------------------------------------------------------
# Error envelope — frozen by openapi.yaml
# ---------------------------------------------------------------------------


class _ErrorBody(BaseModel):
    """Inner ``error`` object: machine code + friendly message."""

    code: str
    message: str

    model_config = {"frozen": True}


class ErrorResponse(BaseModel):
    """Top-level error envelope: ``{"error": {"code", "message"}}``.

    The ``429`` variant adds ``resets_at`` as a **sibling** of ``error``
    (R10), not a field inside it.
    """

    error: _ErrorBody
    resets_at: str | None = None

    model_config = {"frozen": True}


def raise_error(
    status: int,
    code: str,
    message: str,
    **extra: Any,  # noqa: ANN401
) -> NoReturn:
    """Raise an ``HTTPException`` whose body matches the frozen envelope.

    Parameters
    ----------
    status:
        HTTP status code (e.g. ``400``, ``401``, ``404``, ``429``).
    code:
        Machine-readable error code (``already_voted``, ``out_of_coins``, …).
    message:
        Friendly copy the frontend may display verbatim.
    **extra:
        Sibling keys placed next to ``error`` in the body.  The only
        recognised key today is ``resets_at`` (for 429).
    """
    body: dict[str, Any] = {
        "error": {"code": code, "message": message},
    }
    if "resets_at" in extra:
        body["resets_at"] = extra["resets_at"]
    raise HTTPException(status_code=status, detail=body)


# ---------------------------------------------------------------------------
# Settings provider
# ---------------------------------------------------------------------------


def get_settings() -> Settings:
    """Return the shared, frozen ``Settings`` instance.

    Never re-reads the environment or builds a second object.
    """
    return _settings


# ---------------------------------------------------------------------------
# Client providers — unbound until main.py's lifespan installs overrides
# ---------------------------------------------------------------------------


async def get_supabase() -> AsyncClient:
    """Yield the Supabase async client.

    Raises ``RuntimeError`` while unbound — this is a programmer error
    (startup binding was missed), not a client error.
    """
    raise RuntimeError(
        "get_supabase provider is unbound: "
        "main.py must bind a Supabase AsyncClient via "
        "app.dependency_overrides during startup"
    )


async def get_redis() -> redis.asyncio.Redis:  # type: ignore[type-arg]
    """Yield the async Redis client.

    Raises ``RuntimeError`` while unbound — this is a programmer error
    (startup binding was missed), not a client error.
    """
    raise RuntimeError(
        "get_redis provider is unbound: "
        "main.py must bind a redis.asyncio.Redis client via "
        "app.dependency_overrides during startup"
    )


# ---------------------------------------------------------------------------
# Caller-identity providers
# ---------------------------------------------------------------------------


def get_current_user_id(request: Request) -> str:
    """Return the authenticated caller's id from ``request.state``.

    Raises ``HTTPException(401)`` when the id is absent or falsy.
    The 401 body carries a fixed message — no caller id is leaked (R7).
    """
    user_id: str | None = getattr(request.state, "user_id", None)
    if not user_id:
        raise_error(401, "not_authenticated", "Authentication required")
    return user_id  # type: ignore[return-value]  # raise_error is NoReturn


def get_optional_user_id(request: Request) -> str | None:
    """Return the caller's id, or ``None`` for anonymous access.

    Never raises — anonymous reads are the norm on the public board (US-05).
    """
    user_id: str | None = getattr(request.state, "user_id", None)
    return user_id if user_id else None