"""Contract tests for decision logging (US-12).

The one property worth more than the rest: this module must never be the reason
the pipeline stops. Every caller sits on the critical path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from orchestrator.decisions import PROGRAMMATIC, record_decision
from shared.constants import DecisionPhase, FeatureStatus

# decision_log's real columns, from schema.sql.
DECISION_LOG_COLUMNS = {
    "id", "feature_id", "batch_id", "phase", "agent", "decision",
    "model_version", "created_at",
}


class _Query:
    def __init__(self, table: "_Table", payload: Any) -> None:
        self._t, self._payload = table, payload

    async def execute(self) -> Any:
        if self._t.raises is not None:
            raise self._t.raises
        rows = self._payload if isinstance(self._payload, list) else [self._payload]
        for row in rows:
            unknown = set(row) - DECISION_LOG_COLUMNS
            assert not unknown, f"PGRST204 waiting to happen: no column(s) {sorted(unknown)}"
        self._t.inserted.extend(rows)
        return type("R", (), {"data": list(rows)})()


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


# ===========================================================================
# R1 / R7 — the row
# ===========================================================================


@pytest.mark.asyncio
async def test_r1_writes_one_row_with_only_real_columns() -> None:
    sb = FakeSupabase()
    ok = await record_decision(
        sb,
        phase=DecisionPhase.SCREENING,
        agent="screener",
        decision={"verdict": "off_topic", "reason": "not about this app"},
        model_version="MiniMax-M2.7",
        feature_id="f-1",
    )
    assert ok is True
    assert len(sb.t.inserted) == 1
    row = sb.t.inserted[0]
    assert row["agent"] == "screener"
    assert row["feature_id"] == "f-1"


@pytest.mark.asyncio
async def test_r7_a_batch_level_decision_needs_no_feature() -> None:
    """A sprint-level judgement is about no single feature; the column is nullable."""
    sb = FakeSupabase()
    ok = await record_decision(
        sb,
        phase=DecisionPhase.LIFECYCLE,
        agent="sprint_service",
        decision={"selected": 1, "held": 0, "archived": 3},
        model_version=PROGRAMMATIC,
    )
    assert ok is True
    assert sb.t.inserted[0].get("feature_id") is None


# ===========================================================================
# R2 — fail-soft, the property that matters most
# ===========================================================================


@pytest.mark.asyncio
async def test_r2_a_failed_insert_returns_false_and_never_raises() -> None:
    """A verdict that cannot be filed must not stop the pitch being screened."""
    sb = FakeSupabase()
    sb.t.raises = RuntimeError("postgres unreachable")
    ok = await record_decision(
        sb,
        phase=DecisionPhase.SCREENING,
        agent="screener",
        decision={"verdict": "pass"},
        model_version="MiniMax-M2.7",
        feature_id="f-1",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_r2_survives_an_unserialisable_payload() -> None:
    """Logging must not be the most fragile dependency in the pipeline."""
    sb = FakeSupabase()

    class Exploding:
        def __repr__(self) -> str:
            raise ValueError("even repr fails")

    ok = await record_decision(
        sb,
        phase=DecisionPhase.DEDUP,
        agent="pm_agent",
        decision={"weird": Exploding()},
        model_version=PROGRAMMATIC,
    )
    assert ok in (True, False)  # either is fine; not raising is the contract


# ===========================================================================
# R3 / R4 — the labels that make the dataset queryable
# ===========================================================================


@pytest.mark.asyncio
async def test_r3_an_invalid_phase_is_refused_before_the_database() -> None:
    """A typo must fail in the caller's test, not as a Postgres enum error."""
    sb = FakeSupabase()
    ok = await record_decision(
        sb,
        phase="screeening",  # type: ignore[arg-type]
        agent="screener",
        decision={"verdict": "pass"},
        model_version=PROGRAMMATIC,
    )
    assert ok is False
    assert sb.t.inserted == [], "an unknown phase reached the database"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", list(DecisionPhase))
async def test_r3_every_declared_phase_is_accepted(phase: DecisionPhase) -> None:
    sb = FakeSupabase()
    assert await record_decision(
        sb, phase=phase, agent="a", decision={"x": 1}, model_version=PROGRAMMATIC
    )


def test_r4_programmatic_matches_the_schema_and_the_compiler() -> None:
    """Two spellings would split the dataset this table exists to build."""
    assert PROGRAMMATIC == "programmatic"


@pytest.mark.asyncio
async def test_r4_the_model_id_is_recorded_when_a_model_decided() -> None:
    sb = FakeSupabase()
    await record_decision(
        sb, phase=DecisionPhase.FRICTION, agent="architect",
        decision={"friction": "red"}, model_version="MiniMax-M2.7", feature_id="f-1",
    )
    assert sb.t.inserted[0]["model_version"] == "MiniMax-M2.7"


# ===========================================================================
# R8 — the payload survives the driver
# ===========================================================================


@pytest.mark.asyncio
async def test_r8_enums_and_dataclasses_are_serialised() -> None:
    @dataclass(frozen=True)
    class Verdict:
        outcome: str
        score: float

    sb = FakeSupabase()
    ok = await record_decision(
        sb,
        phase=DecisionPhase.FRICTION,
        agent="architect",
        decision={"status": FeatureStatus.SPLIT, "verdict": Verdict("split", 0.91)},
        model_version="MiniMax-M2.7",
        feature_id="f-1",
    )
    assert ok is True
    payload = sb.t.inserted[0]["decision"]
    # Whatever shape it took, the driver must be able to send it as JSON.
    json.dumps(payload) if not isinstance(payload, str) else json.loads(payload)
    assert "SPLIT" in json.dumps(payload)


@pytest.mark.asyncio
async def test_r6_the_reason_travels_with_the_outcome() -> None:
    """'Rejected' alone is not accountability."""
    sb = FakeSupabase()
    await record_decision(
        sb, phase=DecisionPhase.SCREENING, agent="screener",
        decision={"verdict": "unclear", "detail": "title and description describe different things"},
        model_version="MiniMax-M2.7", feature_id="f-1",
    )
    blob = json.dumps(sb.t.inserted[0]["decision"])
    assert "different things" in blob


# ===========================================================================
# R5 — the permanence constraint
# ===========================================================================


def test_r5_the_contract_forbids_storing_unscreened_content() -> None:
    """decision_log is permanent and never pruned.

    Unscreened and rejected pitch text lives in Redis under a TTL; a rejected
    injection attempt filed here would outlive every other trace of it. This
    module cannot enforce that on the caller's payload, so the rule is pinned
    where a caller will read it.
    """
    import pathlib

    from orchestrator import decisions as D

    prompt = pathlib.Path("prompts/orchestration/decisions_python.prompt").read_text()
    assert "R5 (MUST NOT): Write pitch content" in prompt
    assert "never reaches Postgres" in prompt
    # and the module documents it for anyone reading the code alone
    assert "screen" in pathlib.Path(D.__file__).read_text().lower()
