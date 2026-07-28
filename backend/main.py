"""FastAPI application assembly — builds clients, resolves callers, shapes errors.

Every behaviour lives in a route module; this module only wires them together.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import jwt
import redis.asyncio
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from supabase._async.client import AsyncClient as SupabaseAsyncClient
from supabase._async.client import create_client

from backend.deps import ErrorResponse, get_redis, get_supabase
from backend.routes.features import router as features_router
from backend.routes.votes import router as votes_router
from shared.config import settings

logger = logging.getLogger(__name__)

# Fixed UUID used as the fallback anonymous caller in DEV_MODE (R9).
_DEV_ANONYMOUS_UUID = "00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# Lifespan — build clients once, bind into deps, close on shutdown (R1–R4)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Build Supabase + Redis once, bind them, close both on shutdown."""

    # R4 — visible warning when DEV_MODE is on
    if settings.DEV_MODE:
        logger.warning(
            "DEV_MODE is enabled — JWT verification is disabled and "
            "X-Dev-User header is spoofable. Never use in production."
        )

    # R3 — every value comes from the imported `settings` object
    supabase_client: SupabaseAsyncClient = await create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_KEY.get_secret_value(),
    )
    redis_client: redis.asyncio.Redis = redis.asyncio.from_url(  # type: ignore[type-arg]
        settings.REDIS_URL,
    )

    # R1 — bind real clients into the dependency-override map
    async def _supabase_override() -> SupabaseAsyncClient:
        return supabase_client

    async def _redis_override() -> redis.asyncio.Redis:  # type: ignore[type-arg]
        return redis_client

    application.dependency_overrides[get_supabase] = _supabase_override
    application.dependency_overrides[get_redis] = _redis_override

    yield

    # R2 — close both; guard so a failure closing one still closes the other
    try:
        await redis_client.aclose()  # type: ignore[union-attr]
    except Exception:
        logger.exception("Error closing Redis client")
    try:
        # supabase-py AsyncClient exposes aclose on its underlying httpx client
        if hasattr(supabase_client, "aclose"):
            await supabase_client.aclose()  # type: ignore[union-attr]
    except Exception:
        logger.exception("Error closing Supabase client")


# ---------------------------------------------------------------------------
# Application instance (R16 — module-level name `app`)
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------------------
# CORS (R15 — exact origin only, never wildcard)
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FORUM_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Caller-identity middleware (R5–R10)
# ---------------------------------------------------------------------------


@app.middleware("http")
async def resolve_caller(request: Request, call_next: object) -> Response:
    """Stamp ``request.state.user_id`` on every request."""

    user_id: str | None = None

    if settings.DEV_MODE:
        # R9 — spoofable header, fixed fallback UUID
        user_id = request.headers.get("X-Dev-User") or _DEV_ANONYMOUS_UUID
    else:
        # R7 — verify JWT signature + expiry using SUPABASE_JWT_SECRET
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            raw_token = auth_header[7:].strip()
            if raw_token:
                try:
                    secret = settings.SUPABASE_JWT_SECRET
                    assert secret is not None  # guaranteed by config validator in prod
                    payload = jwt.decode(
                        raw_token,
                        secret.get_secret_value(),
                        algorithms=["HS256"],
                        options={
                            "require": ["sub", "exp"],
                            "verify_exp": True,
                            "verify_signature": True,
                        },
                    )
                    # R8 — anonymous sessions carry a valid `sub`; no branch on is_anonymous
                    sub = payload.get("sub")
                    if isinstance(sub, str) and sub:
                        user_id = sub
                except Exception:
                    # Malformed / expired / unverifiable → None (R7)
                    user_id = None

    # R5 — always set, even when None
    request.state.user_id = user_id

    # R6 — never return 401 from middleware; continue unconditionally
    response: Response = await call_next(request)  # type: ignore[call-arg]
    return response


# ---------------------------------------------------------------------------
# Error envelope handlers (R11–R13)
# ---------------------------------------------------------------------------


def _envelope(status: int, code: str, message: str, **extra: object) -> JSONResponse:
    """Build a ``JSONResponse`` matching the frozen error envelope."""
    body = ErrorResponse(
        error={"code": code, "message": message},  # type: ignore[arg-type]
        **({"resets_at": extra["resets_at"]} if "resets_at" in extra else {}),  # type: ignore[arg-type]
    )
    return JSONResponse(status_code=status, content=body.model_dump(exclude_none=True))


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """R12 — map validation failures to 400 / validation_failed (not 422)."""
    return _envelope(400, "validation_failed", "Request body failed validation")


@app.exception_handler(StarletteHTTPException)
async def _http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """R11 — re-serialise HTTPException into the frozen envelope.

    ``raise_error`` from deps.py stores the envelope dict in ``detail``;
    framework-raised HTTPExceptions store a plain string.
    """
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        # Already shaped by raise_error — pass through
        body: dict[str, object] = {"error": detail["error"]}
        if "resets_at" in detail:
            body["resets_at"] = detail["resets_at"]
        return JSONResponse(status_code=exc.status_code, content=body)

    # Framework-generated (e.g. 404 Not Found, 405 Method Not Allowed)
    message = detail if isinstance(detail, str) else "Request error"
    code = "not_found" if exc.status_code == 404 else "request_error"
    return _envelope(exc.status_code, code, message)


@app.exception_handler(Exception)
async def _unhandled_exception(_request: Request, exc: Exception) -> JSONResponse:
    """R13 — 500 with fixed copy; log detail server-side, never expose it."""
    logger.exception("Unhandled exception")
    return _envelope(500, "internal_error", "An unexpected error occurred. Please try again later.")


# ---------------------------------------------------------------------------
# Router registration (R14 — features + votes only)
# ---------------------------------------------------------------------------

app.include_router(features_router)
app.include_router(votes_router)