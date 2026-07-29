"""Sprint service — the gate that turns votes into work.

This is a scheduled worker, not an ASGI app.  It reads and writes Postgres
through ``supabase-py``, takes a lock in Redis, and calls the architect.
It is invoked by ``main()`` on a cadence, or by ``scripts/simulate_sprint.py``
calling ``run_sprint`` directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

import redis.asyncio as aioredis
from supabase._async.client import AsyncClient, create_client

from orchestrator.architect import (
    BuildabilityUnavailable,
    BuildVerdict,
    Judge,
    assess_buildability,
    load_blueprint,
)
from orchestrator.decisions import record_decision, PROGRAMMATIC
from shared.config import settings
from shared.constants import (
    DecisionPhase,
    REDIS_AGENT_EVENTS,
    TABLE_FEATURE_REQUESTS,
    FeatureStatus,
)

__all__ = [
    "SprintOutcome",
    "SprintInFlight",
    "run_sprint",
    "main",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SPRINT_LOCK_KEY: Final[str] = "sprint:lock"
_SPRINT_LOCK_TTL_SECONDS: Final[int] = 300  # 5-minute backstop
# One winner per sprint (R3a). The build step appends the winning feature to the
# target app's prompt and regenerates the whole app, so selecting several in one
# cycle means several compiles contending for one prompt file and one sandbox.
# Kept as a constant, not a literal, so a test can raise it.
_SPRINT_CAPACITY: Final[int] = 1
_ROLLBACK_WINDOW_SECONDS: Final[int] = 2 * settings.SPRINT_CADENCE_SECONDS
_DECAY_WINDOW_SECONDS: Final[int] = 7 * settings.SPRINT_CADENCE_SECONDS

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SprintOutcome:
    """Immutable record of one sprint's results."""

    selected: tuple[str, ...]
    held: tuple[str, ...]
    deferred: tuple[str, ...]
    rolled_back: tuple[str, ...]
    archived: tuple[str, ...]


class SprintInFlight(Exception):
    """Raised when another sprint already holds the lock."""


# ---------------------------------------------------------------------------
# Redis event publishing (R15)
# ---------------------------------------------------------------------------


async def _publish_event(
    redis: aioredis.Redis,
    phase: str,
    message: str,
    *,
    feature_id: str | None = None,
) -> None:
    """Publish a single agent_events message to Redis.

    Carries phase and micro-copy only — pitch content MUST NOT appear.
    """
    payload: dict[str, Any] = {
        "phase": phase,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if feature_id is not None:
        payload["feature_id"] = feature_id
    try:
        await redis.publish(REDIS_AGENT_EVENTS, json.dumps(payload))
    except Exception:  # noqa: BLE001
        logger.debug("Failed to publish agent event: %s", message)


# ---------------------------------------------------------------------------
# Conditional transition helper (R12)
# ---------------------------------------------------------------------------


async def _conditional_update(
    supabase: AsyncClient,
    feature_id: str,
    expected_status: str,
    updates: dict[str, Any],
) -> bool:
    """Update a feature row only if it still has ``expected_status``.

    Returns True when the update matched and wrote, False otherwise.
    """
    resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .update(updates)
        .eq("id", feature_id)
        .eq("status", expected_status)
        .execute()
    )
    return bool(resp.data)


# ---------------------------------------------------------------------------
# Core sprint logic
# ---------------------------------------------------------------------------


async def run_sprint(
    supabase: AsyncClient,
    redis: Redis,
    *,
    judge: Judge | None = None,
) -> SprintOutcome:
    """Run one sprint: select, gate, transition, then end-of-sprint maintenance.

    R1: Guarded by a Redis lock with NX + EX, released in ``finally``.
    R2: Raises ``SprintInFlight`` immediately when the lock is held.
    R16: Clients are injected, not constructed.
    R17: ``judge`` is passed through to the architect.
    """
    # R1: Acquire lock — SET key value NX EX <ttl>
    lock_value = f"sprint:{time.monotonic_ns()}"
    acquired = await redis.set(
        _SPRINT_LOCK_KEY, lock_value, nx=True, ex=_SPRINT_LOCK_TTL_SECONDS
    )
    if not acquired:
        # R2: Do not wait, queue, or retry
        raise SprintInFlight("Another sprint already holds the lock")

    try:
        return await _run_sprint_inner(supabase, redis, judge=judge)
    finally:
        # R1: Release in finally — check value to avoid releasing someone else's lock
        try:
            current = await redis.get(_SPRINT_LOCK_KEY)
            if current is not None:
                # Compare as bytes or str depending on decode_responses
                current_str = (
                    current.decode() if isinstance(current, bytes) else current
                )
                if current_str == lock_value:
                    await redis.delete(_SPRINT_LOCK_KEY)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to release sprint lock cleanly")


async def _run_sprint_inner(
    supabase: AsyncClient,
    redis: aioredis.Redis,
    *,
    judge: Judge | None = None,
) -> SprintOutcome:
    """The actual sprint body, called while the lock is held."""

    selected: list[str] = []
    held: list[str] = []
    deferred: list[str] = []
    rolled_back: list[str] = []
    archived: list[str] = []

    # R15: Sprint started
    await _publish_event(redis, "sprint", "Sprint started")

    # ------------------------------------------------------------------
    # R6: Load blueprint once per sprint, with refresh=True
    # ------------------------------------------------------------------
    try:
        blueprint = load_blueprint(refresh=True)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Cannot load blueprint, aborting sprint: %s", exc)
        await _publish_event(redis, "sprint", "Sprint aborted — blueprint unavailable")
        # R5: Return an outcome, do not raise
        return SprintOutcome(
            selected=(),
            held=(),
            deferred=(),
            rolled_back=(),
            archived=(),
        )

    # ------------------------------------------------------------------
    # R3: Select eligible features
    # ------------------------------------------------------------------
    resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select("id, title, description, upvotes, status")
        .eq("status", FeatureStatus.VOTING)
        .gte("upvotes", settings.UPVOTE_THRESHOLD)
        .order("upvotes", desc=True)
        .limit(_SPRINT_CAPACITY)
        .execute()
    )
    candidates: list[dict[str, Any]] = resp.data or []

    # R4: Split children are eligible in their own right — they are VOTING rows
    # and the query above already includes them.

    if not candidates:
        # R5: Empty board is a normal outcome
        logger.info("Sprint found no features above threshold (%d)", settings.UPVOTE_THRESHOLD)
        await _publish_event(redis, "sprint", "Sprint finished — no features above threshold")
        # Still run maintenance
        rolled_back, archived = await _end_of_sprint_maintenance(supabase, redis)
        return SprintOutcome(
            selected=(),
            held=(),
            deferred=(),
            rolled_back=tuple(rolled_back),
            archived=tuple(archived),
        )

    # ------------------------------------------------------------------
    # Gate each candidate (R7, R8, R9, R10, R11)
    # ------------------------------------------------------------------
    for candidate in candidates:
        fid: str = candidate["id"]
        title: str = candidate.get("title", "")

        try:
            # R7: Call assess_buildability for every selected feature
            verdict: BuildVerdict = await assess_buildability(
                {
                    "feature_id": fid,
                    "title": title,
                    "description": candidate.get("description", ""),
                },
                blueprint=blueprint,
                judge=judge,
            )

            if verdict.buildable:
                # R8: Move to IN_SPRINT
                wrote = await _conditional_update(
                    supabase,
                    fid,
                    FeatureStatus.VOTING,
                    {"status": FeatureStatus.IN_SPRINT},
                )
                if wrote:
                    selected.append(fid)
                    # R19: Log every transition
                    logger.info(
                        "Feature %s → IN_SPRINT (friction=%s): %s",
                        fid,
                        verdict.friction,
                        verdict.explanation[:120],
                    )
                    await _publish_event(
                        redis,
                        "sprint",
                        f"Feature selected for sprint (friction: {verdict.friction})",
                        feature_id=fid,
                    )
                    # R21: Record the selection decision (R22: ignore result, R23: no title/description)
                    await record_decision(
                        supabase,
                        phase=DecisionPhase.FRICTION,
                        agent="sprint_service",
                        decision={
                            "type": "selected",
                            "friction": verdict.friction.value,
                            "buildable": True,
                            "explanation": verdict.explanation,
                        },
                        model_version=settings.LLM_MODEL_ARCHITECT,
                        feature_id=fid,
                    )
                else:
                    # R12: Row changed between selection and write — skip
                    logger.info(
                        "Feature %s skipped — status changed concurrently",
                        fid,
                    )
            else:
                # R9: Move to POSTPONED_CONFLICT, write explanation, increment postpone_count
                # We need the current postpone_count to increment it
                current_resp = (
                    await supabase.table(TABLE_FEATURE_REQUESTS)
                    .select("postpone_count")
                    .eq("id", fid)
                    .execute()
                )
                current_postpone = 0
                if current_resp.data:
                    current_postpone = current_resp.data[0].get("postpone_count", 0) or 0

                wrote = await _conditional_update(
                    supabase,
                    fid,
                    FeatureStatus.VOTING,
                    {
                        "status": FeatureStatus.POSTPONED_CONFLICT,
                        "ai_explanation": verdict.explanation,
                        "postpone_count": current_postpone + 1,
                    },
                )
                if wrote:
                    held.append(fid)
                    logger.info(
                        "Feature %s → POSTPONED_CONFLICT (friction=%s): %s",
                        fid,
                        verdict.friction,
                        verdict.explanation[:120],
                    )
                    await _publish_event(
                        redis,
                        "sprint",
                        f"Feature held — conflicts with current blueprint (friction: {verdict.friction})",
                        feature_id=fid,
                    )
                    # R21: Record the hold decision (R22: ignore result, R23: no title/description)
                    await record_decision(
                        supabase,
                        phase=DecisionPhase.FRICTION,
                        agent="sprint_service",
                        decision={
                            "type": "postpone",
                            "friction": verdict.friction.value,
                            "buildable": False,
                            # The reason, not just the outcome (R21). Architect
                            # prose, never the author's text (R23).
                            "explanation": verdict.explanation,
                        },
                        model_version=settings.LLM_MODEL_ARCHITECT,
                        feature_id=fid,
                    )
                else:
                    logger.info(
                        "Feature %s skipped — status changed concurrently",
                        fid,
                    )

        except BuildabilityUnavailable:
            # R10: Leave in VOTING, record as deferred — do NOT hold
            deferred.append(fid)
            logger.info(
                "Feature %s deferred — buildability check unavailable",
                fid,
            )
            await _publish_event(
                redis,
                "sprint",
                "Feature deferred — gate unavailable",
                feature_id=fid,
            )

        except Exception:  # noqa: BLE001
            # R11: One bad row must not abandon the rest
            deferred.append(fid)
            logger.exception(
                "Feature %s deferred — unexpected error during gate",
                fid,
            )
            await _publish_event(
                redis,
                "sprint",
                "Feature deferred — unexpected error",
                feature_id=fid,
            )

    # ------------------------------------------------------------------
    # R13: End-of-sprint maintenance
    # ------------------------------------------------------------------
    rolled_back, archived = await _end_of_sprint_maintenance(supabase, redis)

    # R15: Sprint finished
    await _publish_event(
        redis,
        "sprint",
        f"Sprint finished — {len(selected)} selected, {len(held)} held, "
        f"{len(deferred)} deferred, {len(rolled_back)} rolled back, "
        f"{len(archived)} archived",
    )

    return SprintOutcome(
        selected=tuple(selected),
        held=tuple(held),
        deferred=tuple(deferred),
        rolled_back=tuple(rolled_back),
        archived=tuple(archived),
    )


# ---------------------------------------------------------------------------
# End-of-sprint maintenance (R13, R14, R20)
# ---------------------------------------------------------------------------


async def _end_of_sprint_maintenance(
    supabase: AsyncClient,
    redis: aioredis.Redis,
) -> tuple[list[str], list[str]]:
    """Roll back stale IN_SPRINT rows and decay old below-threshold VOTING rows.

    R13: Runs in the same pass as the sprint.
    R14: MUST NOT archive a feature at or above the threshold.
    R20: MUST NOT delete a row — terminal states are statuses.
    """
    rolled_back: list[str] = []
    archived: list[str] = []

    now = datetime.now(timezone.utc)

    # --- Rollback: IN_SPRINT rows older than the rollback window → VOTING ---
    rollback_cutoff = (
        now.timestamp() - _ROLLBACK_WINDOW_SECONDS
    )
    rollback_cutoff_iso = datetime.fromtimestamp(
        rollback_cutoff, tz=timezone.utc
    ).isoformat()

    rollback_resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select("id")
        .eq("status", FeatureStatus.IN_SPRINT)
        .lt("updated_at", rollback_cutoff_iso)
        .execute()
    )
    for row in rollback_resp.data or []:
        rid = row["id"]
        wrote = await _conditional_update(
            supabase,
            rid,
            FeatureStatus.IN_SPRINT,
            {"status": FeatureStatus.VOTING},
        )
        if wrote:
            rolled_back.append(rid)
            logger.info("Feature %s rolled back IN_SPRINT → VOTING (stale)", rid)
            # R21: Record the rollback decision (R22: ignore result, R23: no title/description)
            await record_decision(
                supabase,
                phase=DecisionPhase.LIFECYCLE,
                agent="sprint_service",
                decision={"type": "rollback", "reason": "stale_in_sprint"},
                model_version=PROGRAMMATIC,
                feature_id=rid,
            )

    # --- Decay: old VOTING rows below threshold → ARCHIVED ---
    decay_cutoff = (
        now.timestamp() - _DECAY_WINDOW_SECONDS
    )
    decay_cutoff_iso = datetime.fromtimestamp(
        decay_cutoff, tz=timezone.utc
    ).isoformat()

    # R14: Only rows strictly below the threshold
    decay_resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select("id")
        .eq("status", FeatureStatus.VOTING)
        .lt("upvotes", settings.UPVOTE_THRESHOLD)
        .lt("updated_at", decay_cutoff_iso)
        .execute()
    )
    for row in decay_resp.data or []:
        aid = row["id"]
        wrote = await _conditional_update(
            supabase,
            aid,
            FeatureStatus.VOTING,
            {"status": FeatureStatus.ARCHIVED},
        )
        if wrote:
            archived.append(aid)
            logger.info("Feature %s decayed VOTING → ARCHIVED (below threshold, old)", aid)
            # R21: Record the archival decision (R22: ignore result, R23: no title/description)
            await record_decision(
                supabase,
                phase=DecisionPhase.LIFECYCLE,
                agent="sprint_service",
                decision={"type": "archival", "reason": "below_threshold_decay"},
                model_version=PROGRAMMATIC,
                feature_id=aid,
            )

    return rolled_back, archived


# ---------------------------------------------------------------------------
# Entry point (R18)
# ---------------------------------------------------------------------------


def main() -> None:
    """Run one sprint per ``SPRINT_CADENCE_SECONDS`` until interrupted.

    R18: Handles SIGINT/SIGTERM for clean shutdown, closes Redis with
    ``aclose()``, and MUST NOT call ``supabase.auth.sign_out()``.
    """
    asyncio.run(_main_loop())


async def _main_loop() -> None:
    """Async main loop — creates clients, runs sprints on cadence."""
    # Build real clients (R16: only main() builds them)
    supabase = await create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_KEY.get_secret_value(),
    )
    redis = aioredis.from_url(settings.REDIS_URL)

    shutdown = asyncio.Event()

    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    logger.info(
        "Sprint service started — cadence %ds, threshold %d",
        settings.SPRINT_CADENCE_SECONDS,
        settings.UPVOTE_THRESHOLD,
    )

    try:
        while not shutdown.is_set():
            try:
                outcome = await run_sprint(supabase, redis)
                logger.info(
                    "Sprint complete: selected=%d held=%d deferred=%d "
                    "rolled_back=%d archived=%d",
                    len(outcome.selected),
                    len(outcome.held),
                    len(outcome.deferred),
                    len(outcome.rolled_back),
                    len(outcome.archived),
                )
            except SprintInFlight:
                logger.info("Sprint already in flight — skipping this cycle")
            except Exception:  # noqa: BLE001
                logger.exception("Sprint failed unexpectedly")

            # Wait for the next cadence or shutdown
            try:
                await asyncio.wait_for(
                    shutdown.wait(),
                    timeout=settings.SPRINT_CADENCE_SECONDS,
                )
            except asyncio.TimeoutError:
                pass  # Normal — cadence elapsed, run next sprint
    finally:
        # R18: Close Redis with aclose(), do NOT call supabase.auth.sign_out()
        await redis.aclose()
        logger.info("Sprint service shut down")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main()