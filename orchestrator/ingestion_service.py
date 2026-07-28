"""Ingestion daemon — drains ``feature_intake`` and persists survivors.

Long-running process, **not** an ASGI app.  Start with::

    python -m orchestrator.ingestion_service
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

from orchestrator.screener import ScreeningUnavailable, screen_pitch
from orchestrator import pm_agent
from orchestrator.pm_agent import Classification, FeatureRef, Outcome
from shared.config import settings
from shared.constants import (
    REDIS_FEATURE_INTAKE,
    TABLE_FEATURE_REQUESTS,
    TABLE_FEATURE_VOTES,
    FeatureStatus,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# R2: canonical key set — the contract between API producer and this consumer
# ---------------------------------------------------------------------------

INTAKE_KEYS: Final[frozenset[str]] = frozenset(
    {"feature_id", "author_id", "title", "description", "submitted_at"}
)

# ---------------------------------------------------------------------------
# BRPOP timeout — bounded so the stop-event is noticed promptly (R1)
# ---------------------------------------------------------------------------

_BRPOP_TIMEOUT: Final[int] = 2  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_unique_violation(exc: APIError) -> bool:
    """Return True when a PostgREST error signals a unique-constraint violation."""
    # PostgREST surfaces Postgres error code 23505 in the response body.
    msg = str(exc).lower()
    return "23505" in msg or "duplicate" in msg or "unique" in msg


async def _fetch_refs(
    supabase: AsyncClient,
    status: FeatureStatus,
) -> list[FeatureRef]:
    """Read id/title/description for rows matching *status* (R18)."""
    resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select("id, title, description")
        .eq("status", status.value)
        .execute()
    )
    return [
        FeatureRef(id=row["id"], title=row["title"], description=row["description"])
        for row in (resp.data or [])
    ]


# ---------------------------------------------------------------------------
# process_one
# ---------------------------------------------------------------------------


async def process_one(raw: str, supabase: AsyncClient) -> str:
    """Process a single intake item and return an outcome tag.

    Outcome tags: ``inserted`` | ``rejected`` | ``malformed`` | ``duplicate``
    | ``unavailable`` | ``merged`` | ``already_shipped``
    """

    # --- R3: parse --------------------------------------------------------
    try:
        item = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("malformed intake item (length=%d)", len(raw) if raw else 0)
        return "malformed"

    if not isinstance(item, dict):
        logger.warning("malformed intake item (length=%d)", len(raw))
        return "malformed"

    # --- R2: exact key match ----------------------------------------------
    if set(item.keys()) != INTAKE_KEYS:
        logger.warning("malformed intake item (length=%d)", len(raw))
        return "malformed"

    feature_id: str = item["feature_id"]

    # --- R4 / R16: screen -------------------------------------------------
    try:
        verdict = await screen_pitch(item)
    except ScreeningUnavailable:
        logger.error("screening unavailable for feature_id=%s", feature_id)
        return "unavailable"

    if not verdict.passed:
        # R5: no title/description in logs for rejected pitches
        logger.info("feature_id=%s outcome=rejected", feature_id)
        return "rejected"

    # --- R17–R24: dedup classification ------------------------------------
    backlog = await _fetch_refs(supabase, FeatureStatus.VOTING)
    shipped = await _fetch_refs(supabase, FeatureStatus.COMPILED)

    try:
        classification: Classification = await pm_agent.classify(
            item,
            backlog=backlog,
            shipped=shipped,
        )
    except Exception:  # noqa: BLE001 — R24: never lose a pitch
        classification = Classification(
            feature_id=feature_id,
            outcome=Outcome.new_unique,
            target_id=None,
            target_title=None,
            detail="fallback: classify raised unexpectedly",
        )

    # --- Route on outcome -------------------------------------------------

    if classification.outcome == Outcome.already_shipped:
        # R23: insert nothing
        logger.info("feature_id=%s outcome=already_shipped", feature_id)
        return "already_shipped"

    if classification.outcome == Outcome.duplicate:
        # R20: increment canonical row's upvotes, merge_count, add vote row
        canonical_id: str = classification.target_id  # type: ignore[assignment]
        try:
            await supabase.rpc(
                "increment_upvotes", {"row_id": canonical_id}
            ).execute()
        except APIError as exc:
            if not _is_unique_violation(exc):
                raise

        # Increment merge_count on canonical row
        try:
            # Read current merge_count, then update
            current_resp = (
                await supabase.table(TABLE_FEATURE_REQUESTS)
                .select("merge_count")
                .eq("id", canonical_id)
                .execute()
            )
            current_mc = 0
            if current_resp.data and current_resp.data[0].get("merge_count") is not None:
                current_mc = current_resp.data[0]["merge_count"]
            await (
                supabase.table(TABLE_FEATURE_REQUESTS)
                .update({"merge_count": current_mc + 1})
                .eq("id", canonical_id)
                .execute()
            )
        except APIError:
            # Best-effort; the upvote is the critical part
            logger.warning(
                "feature_id=%s merge_count increment failed for canonical=%s",
                feature_id,
                canonical_id,
            )

        # R21 / R22: insert vote row for the merging author
        try:
            await (
                supabase.table(TABLE_FEATURE_VOTES)
                .insert(
                    {
                        "feature_id": canonical_id,
                        "user_id": item["author_id"],
                    }
                )
                .execute()
            )
        except APIError as exc:
            if _is_unique_violation(exc):
                # R22: already voted — fine
                logger.info(
                    "feature_id=%s merged author already voted on canonical=%s",
                    feature_id,
                    canonical_id,
                )
            else:
                raise

        logger.info("feature_id=%s outcome=merged canonical=%s", feature_id, canonical_id)
        return "merged"

    # --- new_unique or extends_shipped: insert a new row ------------------

    row: dict[str, object] = {
        "id": feature_id,
        "title": item["title"],
        "description": item["description"],
        "author_id": item["author_id"],
        "status": FeatureStatus.VOTING.value,  # R7
        "upvotes": 1,
    }

    # R19: extends_shipped gets extra columns
    if classification.outcome == Outcome.extends_shipped and classification.target_id:
        row["extends_id"] = classification.target_id
        row["extends_title"] = classification.target_title

    # R9: never set merge_count, parent_id, split_depth, unlock_threshold

    try:
        await supabase.table(TABLE_FEATURE_REQUESTS).insert(row).execute()
    except APIError as exc:
        if _is_unique_violation(exc):
            # R8: redelivered id — idempotent
            logger.info("feature_id=%s outcome=duplicate", feature_id)
            return "duplicate"
        raise

    # R10 / R25
    logger.info("feature_id=%s outcome=inserted", feature_id)
    return "inserted"


# ---------------------------------------------------------------------------
# run — the BRPOP loop
# ---------------------------------------------------------------------------


async def run(stop: asyncio.Event | None = None) -> None:
    """Consume ``feature_intake`` until *stop* is set.

    Builds Supabase and Redis clients once (R11, R15) and tears them down
    on exit.
    """
    if stop is None:
        stop = asyncio.Event()

    # --- R12: graceful shutdown on SIGINT / SIGTERM -----------------------
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

    # --- R11 / R15: build clients once ------------------------------------
    supabase: AsyncClient = await create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_KEY.get_secret_value(),
    )
    redis_client: aioredis.Redis = aioredis.from_url(
        settings.REDIS_URL, decode_responses=True
    )

    try:
        logger.info("ingestion_service started — consuming %s", REDIS_FEATURE_INTAKE)

        while not stop.is_set():
            # R1: bounded BRPOP
            try:
                result = await redis_client.brpop(
                    REDIS_FEATURE_INTAKE, timeout=_BRPOP_TIMEOUT
                )
            except Exception:  # noqa: BLE001
                # Redis hiccup — wait briefly and retry
                if not stop.is_set():
                    await asyncio.sleep(1)
                continue

            if result is None:
                # Timeout — loop back to check stop
                continue

            _key, raw = result

            # --- R13: one bad item must not kill the loop -----------------
            try:
                await process_one(raw, supabase)
            except Exception:  # noqa: BLE001
                # Try to extract feature_id for the log line
                fid = "unknown"
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        fid = parsed.get("feature_id", "unknown")
                except Exception:  # noqa: BLE001
                    pass
                logger.exception("unexpected error processing feature_id=%s", fid)

    finally:
        # R11: close both clients
        await redis_client.aclose()
        # supabase-py AsyncClient doesn't expose a close; aclose if available
        if hasattr(supabase, "aclose"):
            await supabase.aclose()  # type: ignore[attr-defined]

    logger.info("ingestion_service stopped")


# ---------------------------------------------------------------------------
# main — sync entrypoint (R14)
# ---------------------------------------------------------------------------


def main() -> None:
    """Console-script entrypoint — wraps ``asyncio.run(run())``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()