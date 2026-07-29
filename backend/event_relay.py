"""Event relay: Redis pub/sub → ``broadcast_events`` rows for Supabase Realtime.

Subscribes to the Redis ``agent_events`` channel and writes one
``broadcast_events`` row per translatable message.  Supabase Realtime fans
the row out to every open board — this module never touches a browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from dataclasses import dataclass
from typing import Any, Mapping

import redis.asyncio as aioredis
from supabase._async.client import AsyncClient, create_client

from shared.config import settings
from shared.constants import (
    BroadcastPhase,
    REDIS_AGENT_EVENTS,
    TABLE_BROADCAST_EVENTS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase translation table  (R2, R3, R4)
# ---------------------------------------------------------------------------
# Keys: internal phase strings agents publish.
# Values: (BroadcastPhase, agent_name) — the public enum value and the
# human-facing name shown on the ticker.

_PHASE_MAP: dict[str, tuple[BroadcastPhase, str]] = {
    # Screening-related
    "screening": (BroadcastPhase.SCREENING, "Guardagent"),
    "screen": (BroadcastPhase.SCREENING, "Guardagent"),
    "screen_started": (BroadcastPhase.SCREENING, "Guardagent"),
    "screen_completed": (BroadcastPhase.SCREENING, "Guardagent"),
    "screening_started": (BroadcastPhase.SCREENING, "Guardagent"),
    "screening_completed": (BroadcastPhase.SCREENING, "Guardagent"),
    "intake": (BroadcastPhase.SCREENING, "Guardagent"),
    # Dedup / PM work → synthesizing
    "dedup": (BroadcastPhase.SYNTHESIZING, "PM Agent"),
    "dedup_started": (BroadcastPhase.SYNTHESIZING, "PM Agent"),
    "dedup_completed": (BroadcastPhase.SYNTHESIZING, "PM Agent"),
    "synthesize": (BroadcastPhase.SYNTHESIZING, "PM Agent"),
    "synthesizing": (BroadcastPhase.SYNTHESIZING, "PM Agent"),
    "pm": (BroadcastPhase.SYNTHESIZING, "PM Agent"),
    # (friction lives with the architect below — it is a buildability verdict,
    #  not synthesis)
    # Sprint / architect → architecting
    "sprint": (BroadcastPhase.ARCHITECTING, "Architect Agent"),
    "sprint_started": (BroadcastPhase.ARCHITECTING, "Architect Agent"),
    "sprint_selected": (BroadcastPhase.ARCHITECTING, "Architect Agent"),
    "sprint_completed": (BroadcastPhase.ARCHITECTING, "Architect Agent"),
    "hold": (BroadcastPhase.ARCHITECTING, "Architect Agent"),
    "friction": (BroadcastPhase.ARCHITECTING, "Architect Agent"),
    "friction_started": (BroadcastPhase.ARCHITECTING, "Architect Agent"),
    "friction_completed": (BroadcastPhase.ARCHITECTING, "Architect Agent"),
    "architect": (BroadcastPhase.ARCHITECTING, "Architect Agent"),
    "architect_started": (BroadcastPhase.ARCHITECTING, "Architect Agent"),
    "architect_completed": (BroadcastPhase.ARCHITECTING, "Architect Agent"),
    "architecting": (BroadcastPhase.ARCHITECTING, "Architect Agent"),
    # Compile phases → compiling
    "compile": (BroadcastPhase.COMPILING, "Ship Agent"),
    "compile_started": (BroadcastPhase.COMPILING, "Ship Agent"),
    "compile_succeeded": (BroadcastPhase.COMPILING, "Ship Agent"),
    "compile_failed": (BroadcastPhase.COMPILING, "Ship Agent"),
    "compile_retry": (BroadcastPhase.COMPILING, "Ship Agent"),
    "compiling": (BroadcastPhase.COMPILING, "Ship Agent"),
    # Lifecycle sweeps → the Janitor's actual job (rollbacks, decay, the Vault)
    "lifecycle": (BroadcastPhase.ARCHITECTING, "Janitor Agent"),
    "archived": (BroadcastPhase.ARCHITECTING, "Janitor Agent"),
    "rolled_back": (BroadcastPhase.ARCHITECTING, "Janitor Agent"),
    # Deploy → deployed
    "deploy": (BroadcastPhase.DEPLOYED, "Ship Agent"),
    "deploy_started": (BroadcastPhase.DEPLOYED, "Ship Agent"),
    "deploy_completed": (BroadcastPhase.DEPLOYED, "Ship Agent"),
    "deployed": (BroadcastPhase.DEPLOYED, "Ship Agent"),
    "ship": (BroadcastPhase.DEPLOYED, "Ship Agent"),
}

# Maximum length for the micro-copy message (R7).
_MAX_MESSAGE_LENGTH: int = 280


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentEvent:
    """A single broadcast row, ready for insertion."""

    phase: BroadcastPhase
    agent_name: str
    message: str


# ---------------------------------------------------------------------------
# Pure translation (R2–R7, R11)
# ---------------------------------------------------------------------------


def translate(payload: Mapping[str, Any]) -> AgentEvent | None:
    """Translate an internal agent payload into a broadcast row.

    Returns ``None`` when the event should not be relayed (unknown phase,
    missing message, etc.).  Never raises.
    """
    raw_phase = payload.get("phase")
    if not isinstance(raw_phase, str):
        logger.debug("Dropping event: missing or non-string 'phase' field")
        return None

    mapping = _PHASE_MAP.get(raw_phase)
    if mapping is None:
        logger.debug("Dropping event with unmapped phase: %s", raw_phase)
        return None

    broadcast_phase, agent_name = mapping

    raw_message = payload.get("message")
    if not isinstance(raw_message, str) or not raw_message.strip():
        logger.debug("Dropping event: missing or empty 'message' field")
        return None

    # Cap length; never include any other payload field (R7).
    message = raw_message.strip()[:_MAX_MESSAGE_LENGTH]

    return AgentEvent(phase=broadcast_phase, agent_name=agent_name, message=message)


# ---------------------------------------------------------------------------
# Single-message relay (R6, R7, R8, R11, R12)
# ---------------------------------------------------------------------------


async def relay_once(raw: str | bytes, supabase: AsyncClient) -> bool:
    """Parse *raw*, translate, and insert one ``broadcast_events`` row.

    Returns ``True`` when a row was written, ``False`` when the message was
    dropped (malformed JSON, unmapped phase, missing fields, or insert
    failure).  Never raises.
    """
    # --- Parse JSON (R6) ---
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("Dropping non-JSON message")
        return False

    if not isinstance(payload, dict):
        logger.debug("Dropping non-object JSON message")
        return False

    # --- Translate (R2–R5) ---
    event = translate(payload)
    if event is None:
        return False

    # --- Insert (R8, R12) ---
    try:
        await (
            supabase.table(TABLE_BROADCAST_EVENTS)
            .insert(
                {
                    "phase": event.phase.value,
                    "agent_name": event.agent_name,
                    "message": event.message,
                }
            )
            .execute()
        )
    except Exception:  # noqa: BLE001 — R8: never let one insert kill the loop
        logger.exception("Failed to insert broadcast_events row; continuing")
        return False

    return True


# ---------------------------------------------------------------------------
# Relay loop (R1, R8, R9, R10)
# ---------------------------------------------------------------------------


async def run_relay(
    redis: aioredis.Redis,
    supabase: AsyncClient,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Subscribe to ``agent_events`` and relay until *stop* is set.

    Reconnects automatically when Redis drops the connection (R9).
    Never raises out of the loop (R8).
    """
    if stop is None:
        stop = asyncio.Event()

    while not stop.is_set():
        pubsub: aioredis.client.PubSub | None = None
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(REDIS_AGENT_EVENTS)
            logger.info("Subscribed to Redis channel '%s'", REDIS_AGENT_EVENTS)

            while not stop.is_set():
                try:
                    msg = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    continue

                if msg is None:
                    continue

                if msg.get("type") != "message":
                    continue

                data = msg.get("data")
                if data is not None:
                    await relay_once(data, supabase)

        except asyncio.CancelledError:
            logger.info("Relay cancelled; shutting down")
            break
        except Exception:  # noqa: BLE001 — R9: reconnect on any Redis failure
            logger.exception(
                "Redis connection lost; reconnecting in 2 seconds"
            )
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(REDIS_AGENT_EVENTS)
                    await pubsub.aclose()  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    pass

    logger.info("Relay stopped")


# ---------------------------------------------------------------------------
# Entry point (R10)
# ---------------------------------------------------------------------------


def main() -> None:
    """Build clients, run the relay until interrupted."""

    async def _run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        redis_client = aioredis.from_url(settings.REDIS_URL)
        supabase_client = await create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_KEY.get_secret_value(),
        )

        def _signal_handler() -> None:
            logger.info("Signal received; stopping relay")
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        try:
            await run_relay(redis_client, supabase_client, stop=stop)
        finally:
            await redis_client.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    main()