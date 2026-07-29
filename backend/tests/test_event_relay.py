"""Contract tests for the event relay (US-11).

The relay is the only thing standing between "agents publish to Redis" and
"the board can see it", and Supabase Realtime is the only live-update mechanism
in this system — so a row in `broadcast_events` is the whole delivery story.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from backend import event_relay as R
from backend.event_relay import relay_once, translate
from shared.constants import BroadcastPhase

BROADCAST_EVENTS = "broadcast_events"
# The real columns, from schema.sql.
BROADCAST_COLUMNS = {"id", "phase", "agent_name", "message", "created_at"}


class _Query:
    def __init__(self, table: "_Table", payload: Any) -> None:
        self._t, self._payload = table, payload

    async def execute(self) -> Any:
        if self._t.raises is not None:
            raise self._t.raises
        rows = self._payload if isinstance(self._payload, list) else [self._payload]
        for row in rows:
            unknown = set(row) - BROADCAST_COLUMNS
            assert not unknown, f"PGRST204 waiting to happen: no column(s) {sorted(unknown)}"
        self._t.inserted.extend(rows)
        return type("Resp", (), {"data": list(rows)})()


class _Table:
    def __init__(self) -> None:
        self.inserted: list[dict[str, Any]] = []
        self.raises: Exception | None = None

    def insert(self, payload: Any, **_k: Any) -> _Query:
        return _Query(self, payload)


class FakeSupabase:
    def __init__(self) -> None:
        self.t = _Table()

    def table(self, _name: str) -> _Table:
        return self.t


def event(**over: Any) -> str:
    payload = {
        "phase": "compile_started",
        "message": "Ship Agent is compiling the winning feature…",
        "timestamp": "2026-07-29T00:00:00Z",
    }
    payload.update(over)
    return json.dumps(payload)


# ===========================================================================
# R2 / R3 / R4 — the translation that makes any of this work
# ===========================================================================


@pytest.mark.parametrize(
    "internal, expected",
    [
        ("screening", BroadcastPhase.SCREENING),
        ("dedup", BroadcastPhase.SYNTHESIZING),
        ("friction", BroadcastPhase.ARCHITECTING),
        ("sprint", BroadcastPhase.ARCHITECTING),
        ("compile_started", BroadcastPhase.COMPILING),
        ("compile_succeeded", BroadcastPhase.COMPILING),
        ("compile_failed", BroadcastPhase.COMPILING),
        ("deploy", BroadcastPhase.DEPLOYED),
    ],
)
def test_r3_internal_phases_map_onto_the_public_enum(internal, expected) -> None:
    ev = translate({"phase": internal, "message": "x"})
    assert ev is not None, f"{internal} was dropped"
    assert ev.phase == expected


def test_r2_every_phase_the_agents_actually_publish_is_mapped() -> None:
    """A passthrough insert would fail on all of these and the ticker would never move."""
    emitted = ["sprint", "compile_started", "compile_succeeded", "compile_failed"]
    for phase in emitted:
        assert translate({"phase": phase, "message": "x"}) is not None, f"{phase} unmapped"


def test_r2_the_public_phase_is_always_a_real_enum_member() -> None:
    for internal in R._PHASE_MAP:
        ev = translate({"phase": internal, "message": "x"})
        assert ev is not None and ev.phase in set(BroadcastPhase)


def test_r4_every_event_carries_an_agent_name() -> None:
    """`broadcast_events.agent_name` is NOT NULL and the payload has no agent field."""
    for internal in R._PHASE_MAP:
        ev = translate({"phase": internal, "message": "x"})
        assert ev is not None and ev.agent_name.strip()


def test_r4_agent_names_are_the_ones_the_community_already_sees() -> None:
    known = {"Guardagent", "PM Agent", "Architect Agent", "Janitor Agent", "Ship Agent"}
    assert {agent for _phase, agent in R._PHASE_MAP.values()} <= known


def test_r3_the_architect_owns_friction_not_the_pm() -> None:
    """Friction is a buildability verdict, not synthesis."""
    ev = translate({"phase": "friction", "message": "x"})
    assert ev is not None
    assert ev.agent_name == "Architect Agent"


# ===========================================================================
# R5 / R6 — what gets dropped
# ===========================================================================


def test_r5_an_unmapped_phase_is_dropped_rather_than_guessed() -> None:
    """A wrong phase tells the community the wrong agent is working."""
    assert translate({"phase": "quantum_entangling", "message": "x"}) is None


@pytest.mark.parametrize(
    "payload",
    ["not json at all", "[1,2,3]", '{"phase":"sprint"}', '{"message":"no phase"}', '{"phase":"sprint","message":""}'],
)
@pytest.mark.asyncio
async def test_r6_malformed_messages_are_dropped_without_raising(payload) -> None:
    """Anything can be published to a Redis channel."""
    sb = FakeSupabase()
    assert await relay_once(payload, sb) is False
    assert sb.t.inserted == []


@pytest.mark.asyncio
async def test_r6_bytes_are_accepted_like_str() -> None:
    """redis-py hands back bytes unless decode_responses is set."""
    sb = FakeSupabase()
    assert await relay_once(event().encode(), sb) is True


# ===========================================================================
# R7 — the ticker is public and the pitch may be unscreened
# ===========================================================================


@pytest.mark.asyncio
async def test_r7_only_the_message_is_relayed() -> None:
    sb = FakeSupabase()
    await relay_once(
        event(feature_id="3f8c1a22-9b4d-4e7a-8c11-77aa2b3c4d5e", title="Blockchain check-ins"),
        sb,
    )
    row = sb.t.inserted[0]
    assert set(row) <= {"phase", "agent_name", "message"}
    blob = json.dumps(row)
    assert "3f8c1a22" not in blob
    assert "Blockchain" not in blob


@pytest.mark.asyncio
async def test_r7_a_long_message_is_capped() -> None:
    """Agents write micro-copy, but a stack trace must never become the ticker."""
    sb = FakeSupabase()
    await relay_once(event(message="x" * 5000), sb)
    assert len(sb.t.inserted[0]["message"]) <= 500


# ===========================================================================
# R8 — the ticker is decorative; the pipeline is not
# ===========================================================================


@pytest.mark.asyncio
async def test_r8_a_failed_insert_does_not_raise() -> None:
    sb = FakeSupabase()
    sb.t.raises = RuntimeError("postgres unhappy")
    assert await relay_once(event(), sb) is False


@pytest.mark.asyncio
async def test_r8_the_loop_survives_a_failing_insert() -> None:
    """One unhappy insert must not take down the process feeding the board."""
    sb = FakeSupabase()
    sb.t.raises = RuntimeError("boom")
    for _ in range(3):
        assert await relay_once(event(), sb) is False
    sb.t.raises = None
    assert await relay_once(event(), sb) is True


# ===========================================================================
# R12 — the row
# ===========================================================================


@pytest.mark.asyncio
async def test_r12_one_row_per_event_with_only_real_columns() -> None:
    sb = FakeSupabase()
    assert await relay_once(event(), sb) is True
    assert len(sb.t.inserted) == 1
    row = sb.t.inserted[0]
    assert row["phase"] == BroadcastPhase.COMPILING.value or row["phase"] == BroadcastPhase.COMPILING
    assert row["agent_name"] == "Ship Agent"
    assert "compiling" in row["message"].lower()


# ===========================================================================
# R1 / R10 / R11 — the loop
# ===========================================================================


@pytest.mark.asyncio
async def test_r10_the_relay_stops_when_asked() -> None:
    """A stop event must end the loop rather than needing the process killed."""

    class _PubSub:
        async def subscribe(self, *_a: Any) -> None: ...
        async def unsubscribe(self, *_a: Any) -> None: ...
        async def aclose(self) -> None: ...
        async def get_message(self, **_k: Any) -> None:
            await asyncio.sleep(0)
            return None

    class _Redis:
        def pubsub(self) -> _PubSub:
            return _PubSub()

    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(R.run_relay(_Redis(), FakeSupabase(), stop=stop), timeout=2.0)


def test_r1_subscribes_rather_than_draining_a_queue() -> None:
    """Agents `publish`; BRPOP would consume nothing and the ticker would stay silent."""
    src = __import__("pathlib").Path(R.__file__).read_text()
    assert "pubsub" in src
    assert "brpop" not in src.lower()


def test_no_streaming_transport_is_introduced() -> None:
    """Realtime is the only live-update mechanism in this system."""
    src = __import__("pathlib").Path(R.__file__).read_text()
    for forbidden in ("EventSource", "websocket", "WebSocket", "text/event-stream"):
        assert forbidden not in src
