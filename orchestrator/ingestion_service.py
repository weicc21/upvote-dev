"""Long-running intake daemon — drains ``feature_intake`` and persists survivors.

This module is a standalone process (``python -m orchestrator.ingestion_service``),
**not** an ASGI app.  It bridges the Redis queue written by ``POST /api/features``
and the ``feature_requests`` table that the board reads.

Every verdict is delegated to :func:`orchestrator.screener.screen_pitch`; this
module owns only the queue loop, the Postgres insert, and the lifecycle of its
own clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from typing import Final

import redis.asyncio as aioredis
from postgrest.exceptions import APIError
from supabase._async.client import AsyncClient, create_client

from orchestrator.screener import screen_pitch
from shared.config import settings
from shared.constants import (
    REDIS_FEATURE_INTAKE,
    TABLE_FEATURE_REQUESTS,
    FeatureStatus,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_BRPOP_TIMEOUT_SECONDS: Final[int] = 2
"""Bounded timeout so the stop-event is checked at least every N seconds (R1)."""

_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {"feature_id", "author_id", "title", "description", "submitted_at"}
)
"""Exact keys the API writes — the cross-process contract (R2)."""

# Outcome tags returned by :func:`process_one`.
_INSERTED: Final[str] = "inserted"
_REJECTED: Final[str] = "rejected"
_MALFORMED: Final[str] = "malformed"
_DUPLICATE: Final[str] = "duplicate"


# ---------------------------------------------------------------------------
# process_one
# ---------------------------------------------------------------------------


async def process_one(raw: str, supabase: AsyncClient) -> str:
    """Screen a single intake item and persist it if it passes.

    Returns one of ``'inserted'``, ``'rejected'``, ``'malformed'``, or
    ``'duplicate'``.
    """

    # -- R3: parse --------------------------------------------------------
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "malformed intake item (length=%d); dropping",
            len(raw) if isinstance(raw, str) else 0,
        )
        return _MALFORMED

    if not isinstance(payload, dict):
        logger.warning(
            "malformed intake item (length=%d); dropping",
            len(raw),
        )
        return _MALFORMED

    # -- R2: exact key check ----------------------------------------------
    if set(payload.keys()) != _REQUIRED_KEYS:
        logger.warning(
            "malformed intake item (length=%d); dropping",
            len(raw),
        )
        return _MALFORMED

    feature_id: str = payload.get("feature_id", "")

    # -- R4: delegate verdict to screener ---------------------------------
    verdict = screen_pitch(payload)

    if not verdict.passed:
        # R5: never log title or description of a rejected pitch.
        logger.info(
            "feature_id=%s outcome=%s",
            feature_id,
            _REJECTED,
        )
        return _REJECTED

    # -- R6, R7: insert survivor ------------------------------------------
    try:
        await (
            supabase.table(TABLE_FEATURE_REQUESTS)
            .insert(
                {
                    "id": payload["feature_id"],
                    "author_id": payload["author_id"],
                    "title": payload["title"],
                    "description": payload["description"],
                    "status": FeatureStatus.VOTING,  # R7
                    "upvotes": 1,
                },
            )
            .execute()
        )
    except APIError as exc:
        # R8: unique-violation → duplicate, not a crash.
        # Postgres error code 23505 is unique_violation.
        msg = str(exc)
        if "23505" in msg or "duplicate" in msg.lower():
            logger.info(
                "feature_id=%s outcome=%s",
                feature_id,
                _DUPLICATE,
            )
            return _DUPLICATE
        raise  # unexpected DB error — let the outer handler deal with it

    # R10: log outcome without pitch text.
    logger.info(
        "feature_id=%s outcome=%s",
        feature_id,
        _INSERTED,
    )
    return _INSERTED


# ---------------------------------------------------------------------------
# run — the BRPOP loop
# ---------------------------------------------------------------------------


async def run(stop: asyncio.Event | None = None) -> None:
    """Consume ``feature_intake`` until *stop* is set.

    Builds its own Supabase and Redis clients (R11, R15) and tears them
    down on exit.
    """

    if stop is None:
        stop = asyncio.Event()

    # -- R12: wire SIGINT / SIGTERM to the stop event ---------------------
    loop = asyncio.get_running_loop()

    def _request_stop(sig: signal.Signals) -> None:  # noqa: ARG001
        logger.info("shutdown signal received — finishing in-flight item")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop, sig)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler; fall back to
            # signal.signal which is less clean but still functional.
            signal.signal(sig, lambda _s, _f: _request_stop(_s))

    # -- R11, R15: build clients once -------------------------------------
    redis: aioredis.Redis = aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
    )
    supabase: AsyncClient = await create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_KEY.get_secret_value(),
    )

    logger.info("ingestion_service started — consuming %s", REDIS_FEATURE_INTAKE)

    try:
        while not stop.is_set():
            # -- R1: bounded BRPOP ----------------------------------------
            try:
                result = await redis.brpop(
                    REDIS_FEATURE_INTAKE,
                    timeout=_BRPOP_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("redis BRPOP error — retrying after timeout")
                await asyncio.sleep(_BRPOP_TIMEOUT_SECONDS)
                continue

            if result is None:
                # Timeout — no item available; loop to re-check stop.
                continue

            # result is (key, value)
            _key, raw = result

            # -- R13: isolate per-item failures ---------------------------
            try:
                await process_one(raw, supabase)
            except asyncio.CancelledError:
                break
            except Exception:
                # Extract feature_id best-effort for the log line.
                try:
                    fid = json.loads(raw).get("feature_id", "<unknown>")
                except Exception:
                    fid = "<unknown>"
                logger.exception(
                    "feature_id=%s unexpected error — dropping item",
                    fid,
                )
    finally:
        # -- R11: close clients -------------------------------------------
        logger.info("ingestion_service shutting down")
        await redis.aclose()
        # supabase-py AsyncClient doesn't expose a close(); the httpx
        # transport is cleaned up by GC.  If a future version adds one,
        # call it here.


# ---------------------------------------------------------------------------
# main — console-script entrypoint (R14)
# ---------------------------------------------------------------------------


def main() -> None:
    """Synchronous wrapper suitable for ``console_scripts`` or direct invocation."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()