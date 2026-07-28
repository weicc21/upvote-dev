"""orchestrator/ingestion_service.py — Long-running queue consumer.

Drains ``feature_intake``, screens each pitch, dedup-classifies survivors,
and persists the result as public ``feature_requests`` rows.

This module is a daemon process, **not** an ASGI app.  It is started via
``python -m orchestrator.ingestion_service`` and communicates with the API
exclusively through Redis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from typing import Any, Final

import redis.asyncio as aioredis
from postgrest.exceptions import APIError
from supabase._async.client import AsyncClient, create_client

from orchestrator.pm_agent import Classification, FeatureRef, Outcome, classify
from orchestrator.screener import ScreeningUnavailable, screen_pitch
from shared.config import settings
from shared.constants import (
    REDIS_FEATURE_INTAKE,
    TABLE_FEATURE_REQUESTS,
    TABLE_FEATURE_VOTES,
    FeatureStatus,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

logger: Final[logging.Logger] = logging.getLogger(__name__)

#: R2 — exact key set expected in every intake item.  Exposed so contract
#: tests can import it and assert the producer writes the same set.
INTAKE_KEYS: Final[frozenset[str]] = frozenset(
    {"feature_id", "author_id", "title", "description", "submitted_at"}
)

#: BRPOP timeout in seconds — bounded so the stop event is noticed promptly.
_BRPOP_TIMEOUT: Final[int] = 2

# ---------------------------------------------------------------------------
# Postgres unique-violation detection
# ---------------------------------------------------------------------------

_PG_UNIQUE_VIOLATION: Final[str] = "23505"


def _is_unique_violation(exc: APIError) -> bool:
    """Return ``True`` when *exc* represents a Postgres unique-violation."""
    code = getattr(exc, "code", None) or ""
    message = getattr(exc, "message", "") or str(exc)
    return str(code) == _PG_UNIQUE_VIOLATION or "duplicate key" in message.lower()


# ---------------------------------------------------------------------------
# Backlog / shipped readers  (R18)
# ---------------------------------------------------------------------------


async def _read_backlog(supabase: AsyncClient) -> list[FeatureRef]:
    """Return VOTING rows projected to id/title/description (R18)."""
    resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select("id, title, description")
        .eq("status", FeatureStatus.VOTING.value)
        .execute()
    )
    return [
        FeatureRef(id=r["id"], title=r["title"], description=r["description"])
        for r in (resp.data or [])
    ]


async def _read_shipped(supabase: AsyncClient) -> list[FeatureRef]:
    """Return COMPILED rows projected to id/title/description (R18)."""
    resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select("id, title, description")
        .eq("status", FeatureStatus.COMPILED.value)
        .execute()
    )
    return [
        FeatureRef(id=r["id"], title=r["title"], description=r["description"])
        for r in (resp.data or [])
    ]


# ---------------------------------------------------------------------------
# Vote-row helper (R26, R27, R21, R22)
# ---------------------------------------------------------------------------


async def _ensure_vote_row(
    supabase: AsyncClient,
    feature_id: str,
    user_id: str,
    *,
    context: str,
) -> None:
    """Insert a ``feature_votes`` row; swallow unique-violations (R27/R22)."""
    try:
        await (
            supabase.table(TABLE_FEATURE_VOTES)
            .insert({"feature_id": feature_id, "user_id": user_id})
            .execute()
        )
    except APIError as exc:
        if _is_unique_violation(exc):
            logger.info(
                "vote row already exists (%s): feature_id=%s user_id=%s",
                context,
                feature_id,
                user_id,
            )
        else:
            raise


# ---------------------------------------------------------------------------
# Outcome handlers
# ---------------------------------------------------------------------------


async def _handle_new_unique(
    supabase: AsyncClient,
    item: dict[str, Any],
    *,
    extends_id: str | None = None,
    extends_title: str | None = None,
) -> str:
    """Insert a new board row and the author's vote row.  Returns outcome tag."""
    row: dict[str, Any] = {
        "id": item["feature_id"],
        "author_id": item["author_id"],
        "title": item["title"],
        "description": item["description"],
        "status": FeatureStatus.VOTING.value,  # R7
        "upvotes": 1,  # R6
    }
    if extends_id is not None:
        row["extends_id"] = extends_id  # R19
    if extends_title is not None:
        row["extends_title"] = extends_title  # R19

    # R9: do NOT set merge_count, parent_id, split_depth, unlock_threshold

    try:
        await supabase.table(TABLE_FEATURE_REQUESTS).insert(row).execute()
    except APIError as exc:
        if _is_unique_violation(exc):
            logger.info("duplicate insert (idempotent): feature_id=%s", item["feature_id"])
            return "duplicate"  # R8
        raise

    # R26: author's own vote row
    await _ensure_vote_row(
        supabase,
        item["feature_id"],
        item["author_id"],
        context="insert",
    )

    return "inserted"


async def _handle_merge(
    supabase: AsyncClient,
    item: dict[str, Any],
    classification: Classification,
) -> str:
    """Merge into the canonical row: increment upvotes, bump merge_count, add vote."""
    canonical_id: str = classification.target_id  # type: ignore[assignment]

    # R20: increment upvotes via RPC (exactly one argument)
    await supabase.rpc("increment_upvotes", {"row_id": canonical_id}).execute()

    # R20: increment merge_count
    # Read current value, then update
    resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select("merge_count")
        .eq("id", canonical_id)
        .execute()
    )
    current = (resp.data or [{}])[0].get("merge_count") or 0
    await (
        supabase.table(TABLE_FEATURE_REQUESTS)
        .update({"merge_count": current + 1})
        .eq("id", canonical_id)
        .execute()
    )

    # R21: vote row for the merging author against the canonical feature
    await _ensure_vote_row(
        supabase,
        canonical_id,
        item["author_id"],
        context="merge",
    )

    return "merged"


# ---------------------------------------------------------------------------
# Core per-item processor
# ---------------------------------------------------------------------------


async def process_one(raw: str, supabase: AsyncClient) -> str:
    """Process a single intake item.  Returns an outcome tag.

    Outcome tags: ``inserted``, ``rejected``, ``malformed``, ``duplicate``,
    ``unavailable``, ``merged``, ``already_shipped``.
    """
    # -- R3: parse JSON -----------------------------------------------------
    try:
        item = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("malformed intake item (length=%d)", len(raw) if raw else 0)
        return "malformed"

    if not isinstance(item, dict):
        logger.warning("malformed intake item (length=%d)", len(raw))
        return "malformed"

    # -- R2: exact key-set check --------------------------------------------
    if set(item.keys()) != INTAKE_KEYS:
        logger.warning("malformed intake item (length=%d)", len(raw))
        return "malformed"

    feature_id: str = item["feature_id"]

    # -- R4: screen the pitch -----------------------------------------------
    try:
        verdict = await screen_pitch(item)
    except ScreeningUnavailable:
        # R16: infrastructure failure — distinct from rejection
        logger.error("screening unavailable: feature_id=%s", feature_id)
        return "unavailable"

    if not verdict.passed:
        # R5: do NOT log title or description of a rejected pitch
        logger.info("feature_id=%s outcome=rejected", feature_id)
        return "rejected"

    # -- R17: dedup classification ------------------------------------------
    backlog = await _read_backlog(supabase)
    shipped = await _read_shipped(supabase)

    try:
        classification = await classify(
            item,
            backlog=backlog,
            shipped=shipped,
        )
    except Exception:  # noqa: BLE001 — R24: never lose a pitch
        logger.exception("classify raised unexpectedly: feature_id=%s — treating as new_unique", feature_id)
        classification = Classification(
            feature_id=feature_id,
            outcome=Outcome.new_unique,
            target_id=None,
            target_title=None,
            detail="fallback: classify raised",
        )

    # -- Route on outcome (R19, R20, R23, R25) ------------------------------
    outcome_tag: str

    if classification.outcome == Outcome.new_unique:
        outcome_tag = await _handle_new_unique(supabase, item)

    elif classification.outcome == Outcome.extends_shipped:
        # R19: insert with extends_id and extends_title
        outcome_tag = await _handle_new_unique(
            supabase,
            item,
            extends_id=classification.target_id,
            extends_title=classification.target_title,
        )

    elif classification.outcome == Outcome.duplicate:
        # R20: merge into canonical
        outcome_tag = await _handle_merge(supabase, item, classification)

    elif classification.outcome == Outcome.already_shipped:
        # R23: insert nothing
        outcome_tag = "already_shipped"

    else:
        # Defensive — treat unknown outcome as new_unique (R24 spirit)
        outcome_tag = await _handle_new_unique(supabase, item)

    # -- R10: one INFO line per item, no pitch text -------------------------
    logger.info("feature_id=%s outcome=%s", feature_id, outcome_tag)
    return outcome_tag


# ---------------------------------------------------------------------------
# BRPOP loop
# ---------------------------------------------------------------------------


async def run(stop: asyncio.Event | None = None) -> None:
    """Consume ``feature_intake`` until *stop* is set.

    Builds Supabase and Redis clients once (R11, R15), installs signal
    handlers (R12), and loops with bounded BRPOP (R1).
    """
    if stop is None:
        stop = asyncio.Event()

    # -- R15: credentials from settings only --------------------------------
    supabase: AsyncClient = await create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_KEY.get_secret_value(),
    )
    redis_client: aioredis.Redis = aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
    )

    # -- R12: graceful shutdown on SIGINT / SIGTERM -------------------------
    loop = asyncio.get_running_loop()

    def _request_stop(sig: signal.Signals) -> None:  # noqa: N803
        logger.info("received %s — shutting down after current item", sig.name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop, sig)
        except NotImplementedError:
            # Windows does not support add_signal_handler
            pass

    logger.info("ingestion_service started — consuming %s", REDIS_FEATURE_INTAKE)

    try:
        while not stop.is_set():
            # -- R1: bounded BRPOP ------------------------------------------
            try:
                result = await redis_client.brpop(
                    REDIS_FEATURE_INTAKE,
                    timeout=_BRPOP_TIMEOUT,
                )
            except Exception:  # noqa: BLE001
                logger.exception("BRPOP error — retrying after timeout")
                await asyncio.sleep(_BRPOP_TIMEOUT)
                continue

            if result is None:
                # Timeout — loop back and check stop
                continue

            _key, raw = result

            # -- R13: one bad item must not kill the loop -------------------
            try:
                await process_one(raw, supabase)
            except Exception:  # noqa: BLE001
                # Try to extract feature_id for the log line
                fid = "<unknown>"
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        fid = parsed.get("feature_id", fid)
                except Exception:  # noqa: BLE001
                    pass
                logger.exception("unexpected error processing item: feature_id=%s", fid)
    finally:
        # -- R11: clean shutdown — close Redis, leave Supabase alone --------
        await redis_client.aclose()
        logger.info("ingestion_service stopped")


# ---------------------------------------------------------------------------
# Console-script entrypoint (R14)
# ---------------------------------------------------------------------------


def main() -> None:
    """Sync wrapper — ``python -m orchestrator.ingestion_service``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()