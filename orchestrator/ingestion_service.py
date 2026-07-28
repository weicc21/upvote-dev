"""orchestrator/ingestion_service.py — Long-running daemon that drains
``feature_intake`` and turns surviving pitches into public board rows.

This module is a queue consumer, not an ASGI app.  It reads from Redis,
calls the screening / dedup / architect agents, and writes to Postgres.
It never serves HTTP and never imports from ``backend/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import uuid
from typing import Any, Final

import redis.asyncio as aioredis
from postgrest.exceptions import APIError
from supabase._async.client import AsyncClient, create_client

from orchestrator.architect import Shape, decide_shape, load_blueprint
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
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Contract: the exact key set every intake item must carry (R2)
# ---------------------------------------------------------------------------

INTAKE_KEYS: Final[frozenset[str]] = frozenset(
    {"feature_id", "author_id", "title", "description", "submitted_at"}
)

# ---------------------------------------------------------------------------
# BRPOP timeout — bounded so the stop event is noticed promptly (R1)
# ---------------------------------------------------------------------------

_BRPOP_TIMEOUT: Final[int] = 2  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_unique_violation(exc: APIError) -> bool:
    """Return True when a PostgREST error is a Postgres unique-violation."""
    # PostgREST surfaces the PG error code in the JSON body under "code"
    # or in the message.  The Postgres unique_violation code is 23505.
    msg = str(getattr(exc, "message", "")) + str(getattr(exc, "details", ""))
    code = str(getattr(exc, "code", ""))
    return "23505" in code or "23505" in msg or "duplicate key" in msg.lower()


async def _fetch_refs(
    supabase: AsyncClient,
    status: FeatureStatus,
) -> list[FeatureRef]:
    """Read minimal feature rows for dedup comparison (R18)."""
    resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select("id, title, description")
        .eq("status", status.value)
        .execute()
    )
    return [
        FeatureRef(id=r["id"], title=r["title"], description=r["description"])
        for r in (resp.data or [])
    ]


async def _insert_vote_row(
    supabase: AsyncClient,
    feature_id: str,
    user_id: str,
) -> None:
    """Insert a feature_votes row; swallow unique-violations (R26/R27, R21/R22)."""
    try:
        await (
            supabase.table(TABLE_FEATURE_VOTES)
            .insert({"feature_id": feature_id, "user_id": user_id})
            .execute()
        )
    except APIError as exc:
        if _is_unique_violation(exc):
            logger.info(
                "vote row already exists feature_id=%s user_id=%s",
                feature_id,
                user_id,
            )
        else:
            raise


async def _insert_board_row(
    supabase: AsyncClient,
    item: dict[str, Any],
    *,
    status: FeatureStatus,
    extends_id: str | None = None,
    extends_title: str | None = None,
    ai_explanation: str | None = None,
) -> None:
    """Insert a single feature_requests row (R6, R9, R30, R31)."""
    row: dict[str, Any] = {
        "id": item["feature_id"],
        "author_id": item["author_id"],
        "title": item["title"],
        "description": item["description"],
        "status": status.value,
        "upvotes": 1,
    }
    if extends_id is not None:
        row["extends_id"] = extends_id
    if extends_title is not None:
        row["extends_title"] = extends_title
    if ai_explanation is not None:
        row["ai_explanation"] = ai_explanation
    await supabase.table(TABLE_FEATURE_REQUESTS).insert(row).execute()


# ---------------------------------------------------------------------------
# Core per-item processing
# ---------------------------------------------------------------------------


async def process_one(raw: str, supabase: AsyncClient, *, blueprint: str) -> str:
    """Process a single intake item and return an outcome tag.

    Returns one of:
    ``'inserted'`` | ``'rejected'`` | ``'malformed'`` | ``'duplicate'`` |
    ``'unavailable'`` | ``'merged'`` | ``'already_shipped'`` |
    ``'postponed'`` | ``'split'``
    """
    # -- Parse JSON (R3) ----------------------------------------------------
    try:
        item = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("malformed intake item length=%d", len(raw))
        return "malformed"

    if not isinstance(item, dict):
        logger.warning("malformed intake item length=%d", len(raw))
        return "malformed"

    # -- Exact key check (R2) -----------------------------------------------
    if set(item.keys()) != INTAKE_KEYS:
        logger.warning("malformed intake item length=%d", len(raw))
        return "malformed"

    feature_id: str = str(item["feature_id"])

    # -- Screen (R4) --------------------------------------------------------
    try:
        verdict = await screen_pitch(item)
    except ScreeningUnavailable:
        # R16: infrastructure failure — own outcome, not a rejection
        logger.error("screening unavailable feature_id=%s", feature_id)
        return "unavailable"

    if not verdict.passed:
        # R5: never persist rejected title/description anywhere
        logger.info("item feature_id=%s outcome=rejected", feature_id)
        return "rejected"

    # -- Dedup / classify (R17, R24) ----------------------------------------
    try:
        backlog = await _fetch_refs(supabase, FeatureStatus.VOTING)
        shipped = await _fetch_refs(supabase, FeatureStatus.COMPILED)
        classification: Classification = await classify(
            item,
            backlog=backlog,
            shipped=shipped,
        )
    except Exception:  # noqa: BLE001 — R24: never lose a screened pitch
        logger.exception(
            "classify raised unexpectedly feature_id=%s — treating as new_unique",
            feature_id,
        )
        classification = Classification(
            feature_id=feature_id,
            outcome=Outcome.new_unique,
            target_id=None,
            target_title=None,
            detail="fallback: classify raised",
        )

    outcome = classification.outcome

    # -- R23: already_shipped — insert nothing ------------------------------
    if outcome == Outcome.already_shipped:
        logger.info("item feature_id=%s outcome=already_shipped", feature_id)
        return "already_shipped"

    # -- R20/R21/R22: duplicate — merge into canonical ----------------------
    if outcome == Outcome.duplicate:
        canonical_id = classification.target_id
        assert canonical_id is not None  # guaranteed by pm_agent contract
        try:
            await supabase.rpc(
                "increment_upvotes", {"row_id": canonical_id}
            ).execute()
            # Increment merge_count
            existing = (
                await supabase.table(TABLE_FEATURE_REQUESTS)
                .select("merge_count")
                .eq("id", canonical_id)
                .execute()
            )
            current_merge = (existing.data[0].get("merge_count") or 0) if existing.data else 0
            await (
                supabase.table(TABLE_FEATURE_REQUESTS)
                .update({"merge_count": current_merge + 1})
                .eq("id", canonical_id)
                .execute()
            )
            # R21: vote row for the merging author
            await _insert_vote_row(supabase, canonical_id, item["author_id"])
        except APIError as exc:
            if _is_unique_violation(exc):
                logger.info(
                    "merge vote already counted feature_id=%s canonical=%s",
                    feature_id,
                    canonical_id,
                )
            else:
                raise
        logger.info("item feature_id=%s outcome=merged", feature_id)
        return "merged"

    # -- R28: decide_shape only for rows that will be created ---------------
    try:
        shape: Shape = await decide_shape(
            item,
            blueprint=blueprint,
        )
    except Exception:  # noqa: BLE001 — R34
        logger.exception(
            "decide_shape raised feature_id=%s — defaulting to VOTING",
            feature_id,
        )
        shape = Shape(
            feature_id=feature_id,
            friction=__import__("orchestrator.architect", fromlist=["Friction"]).Friction.green,
            status=FeatureStatus.VOTING,
            children=(),
            explanation="",
        )

    # R30: use the status the shape returns
    status = shape.status
    ai_explanation: str | None = None
    if status != FeatureStatus.VOTING:
        # R31: write explanation when not VOTING
        ai_explanation = shape.explanation or None

    # -- Determine extends fields (R19) -------------------------------------
    extends_id: str | None = None
    extends_title: str | None = None
    if outcome == Outcome.extends_shipped:
        extends_id = classification.target_id
        extends_title = classification.target_title

    # -- Insert the board row (R6, R8) --------------------------------------
    try:
        if status == FeatureStatus.SPLIT:
            # R32: parent row with SPLIT status, then children
            await _insert_board_row(
                supabase,
                item,
                status=FeatureStatus.SPLIT,
                extends_id=extends_id,
                extends_title=extends_title,
                ai_explanation=ai_explanation,
            )
            # R26: author vote on parent
            await _insert_vote_row(supabase, feature_id, item["author_id"])

            # R32: child rows
            for child_spec in shape.children:
                child_id = str(uuid.uuid4())
                child_row: dict[str, Any] = {
                    "id": child_id,
                    "author_id": item["author_id"],
                    "title": child_spec.title[:60],
                    "description": child_spec.description[:300],
                    "status": FeatureStatus.VOTING.value,
                    "upvotes": 0,  # R33
                    "parent_id": feature_id,
                }
                await (
                    supabase.table(TABLE_FEATURE_REQUESTS)
                    .insert(child_row)
                    .execute()
                )
            logger.info("item feature_id=%s outcome=split", feature_id)
            return "split"
        else:
            await _insert_board_row(
                supabase,
                item,
                status=status,
                extends_id=extends_id,
                extends_title=extends_title,
                ai_explanation=ai_explanation,
            )
            # R26: author vote row
            await _insert_vote_row(supabase, feature_id, item["author_id"])
    except APIError as exc:
        if _is_unique_violation(exc):
            # R8: redelivered id — idempotent
            logger.info("item feature_id=%s outcome=duplicate", feature_id)
            return "duplicate"
        raise

    # R35: distinct tags for postponed vs inserted
    if status == FeatureStatus.POSTPONED_CONFLICT:
        logger.info("item feature_id=%s outcome=postponed", feature_id)
        return "postponed"

    logger.info("item feature_id=%s outcome=inserted", feature_id)
    return "inserted"


# ---------------------------------------------------------------------------
# BRPOP loop (R1, R11, R12, R13)
# ---------------------------------------------------------------------------


async def run(stop: asyncio.Event | None = None) -> None:
    """Consume ``feature_intake`` until *stop* is set.

    Builds Supabase and Redis clients once (R11, R15), installs signal
    handlers (R12), and loops with bounded BRPOP (R1).
    """
    if stop is None:
        stop = asyncio.Event()

    # -- Signal handling (R12) ----------------------------------------------
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("shutdown signal received — finishing in-flight item")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # -- Build clients once (R11, R15) --------------------------------------
    redis_client = aioredis.from_url(settings.REDIS_URL)
    supabase: AsyncClient = await create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_KEY.get_secret_value(),
    )

    # -- Load blueprint once (R29) ------------------------------------------
    try:
        blueprint = load_blueprint()
    except (FileNotFoundError, ValueError):
        logger.warning(
            "blueprint not found — architect will fall back to VOTING for every pitch"
        )
        blueprint = ""

    logger.info("ingestion_service started — consuming %s", REDIS_FEATURE_INTAKE)

    try:
        while not stop.is_set():
            # R1: bounded BRPOP
            result = await redis_client.brpop(
                REDIS_FEATURE_INTAKE, timeout=_BRPOP_TIMEOUT
            )
            if result is None:
                # Timeout — check stop and loop
                continue

            _key, raw_bytes = result
            raw: str = (
                raw_bytes.decode("utf-8")
                if isinstance(raw_bytes, (bytes, bytearray))
                else str(raw_bytes)
            )

            # R13: one bad item must not kill the loop
            try:
                tag = await process_one(raw, supabase, blueprint=blueprint)
            except Exception:  # noqa: BLE001
                # Extract feature_id if possible for the log line
                fid = "<unknown>"
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        fid = str(parsed.get("feature_id", "<unknown>"))
                except Exception:  # noqa: BLE001
                    pass
                logger.exception(
                    "unexpected error processing item feature_id=%s — dropped",
                    fid,
                )
    finally:
        # R11: close Redis; Supabase has no public close
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