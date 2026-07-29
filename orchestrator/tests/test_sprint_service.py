"""Contract tests for the sprint service (US-07).

Nothing here touches Postgres, Redis, or an LLM: `run_sprint` takes its clients
as arguments (R16) and its judge as a keyword (R17), which is the whole reason
a sprint is testable at all.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

from orchestrator import sprint_service as S
from orchestrator.architect import BuildabilityUnavailable, BuildVerdict, Friction
from orchestrator.sprint_service import SprintInFlight, run_sprint
from shared.constants import FeatureStatus

MODULE_SRC = pathlib.Path(S.__file__)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class _Query:
    """Records the filters applied, returns whatever the table was seeded with."""

    def __init__(self, table: "_Table", op: str, payload: dict[str, Any] | None = None) -> None:
        self._t = table
        self._op = op
        self._payload = payload or {}
        self._filters: dict[str, Any] = {}
        self._limit: int | None = None
        self._order: tuple[str, bool] | None = None

    def select(self, *_a: Any, **_k: Any) -> "_Query":
        self._op = "select"
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self._filters[f"eq:{col}"] = val
        return self

    def gte(self, col: str, val: Any) -> "_Query":
        self._filters[f"gte:{col}"] = val
        return self

    def lt(self, col: str, val: Any) -> "_Query":
        self._filters[f"lt:{col}"] = val
        return self

    def order(self, col: str, desc: bool = False) -> "_Query":
        self._order = (col, desc)
        return self

    def limit(self, n: int) -> "_Query":
        self._limit = n
        return self

    async def execute(self) -> _Resp:
        self._t.calls.append((self._op, dict(self._filters), self._payload, self._limit))

        if self._op == "update":
            matched = [
                r
                for r in self._t.rows
                if all(r.get(k.split(":", 1)[1]) == v for k, v in self._filters.items() if k.startswith("eq:"))
            ]
            for r in matched:
                r.update(self._payload)
            return _Resp([dict(r) for r in matched])

        rows = list(self._t.rows)
        for key, val in self._filters.items():
            kind, col = key.split(":", 1)
            if kind == "eq":
                rows = [r for r in rows if r.get(col) == val]
            elif kind == "gte":
                rows = [r for r in rows if (r.get(col) or 0) >= val]
            elif kind == "lt":
                rows = [r for r in rows if str(r.get(col) or "") < str(val)]
        if self._order:
            col, desc = self._order
            rows.sort(key=lambda r: r.get(col) or 0, reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Resp([dict(r) for r in rows])


class _Table:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any], int | None]] = []

    def select(self, *_a: Any, **_k: Any) -> _Query:
        return _Query(self, "select")

    def update(self, payload: dict[str, Any]) -> _Query:
        return _Query(self, "update", payload)


class FakeSupabase:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._table = _Table(rows)

    def table(self, _name: str) -> _Table:
        return self._table

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self._table.rows


class FakeRedis:
    """Enough of redis.asyncio for the lock and the event feed."""

    def __init__(self, *, lock_held: bool = False) -> None:
        self.store: dict[str, str] = {"sprint:lock": "someone-else"} if lock_held else {}
        self.published: list[tuple[str, str]] = []

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


def feature(**over: Any) -> dict[str, Any]:
    row = {
        "id": "f-1",
        "title": "Streak counter on every habit",
        "description": "Show a flame badge counting consecutive checked days.",
        "status": FeatureStatus.VOTING,
        "upvotes": 9,
        "postpone_count": 0,
        "updated_at": "2999-01-01T00:00:00+00:00",  # far future = never stale
    }
    row.update(over)
    return row


def judge_returning(payload: str):
    async def _judge(_system: str, _user: str) -> str:
        return payload

    return _judge


@pytest.fixture(autouse=True)
def _blueprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sprint reads the blueprint once; give it one without touching disk."""
    monkeypatch.setattr(S, "load_blueprint", lambda **_k: "## Existing Core Features\n- one tap")


def stub_verdict(monkeypatch: pytest.MonkeyPatch, **by_id: BuildVerdict | Exception) -> list[str]:
    """Replace the architect gate; record which features it was asked about."""
    asked: list[str] = []

    async def _fake(feature_map: Any, *, blueprint: str, judge: Any = None) -> BuildVerdict:
        fid = feature_map["feature_id"]
        asked.append(fid)
        result = by_id.get(fid, BuildVerdict(fid, True, Friction.green, "fits cleanly"))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(S, "assess_buildability", _fake)
    return asked


# ===========================================================================
# R1 / R2 — one sprint at a time
# ===========================================================================


@pytest.mark.asyncio
async def test_r2_refuses_when_a_sprint_is_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two sprints can select the same feature and compile it twice."""
    stub_verdict(monkeypatch)
    with pytest.raises(SprintInFlight):
        await run_sprint(FakeSupabase([feature()]), FakeRedis(lock_held=True))


@pytest.mark.asyncio
async def test_r1_releases_the_lock_so_the_next_sprint_can_run(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_verdict(monkeypatch)
    redis = FakeRedis()
    await run_sprint(FakeSupabase([feature()]), redis)
    assert "sprint:lock" not in redis.store


@pytest.mark.asyncio
async def test_r1_releases_the_lock_even_when_the_sprint_explodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash mid-sprint must not wedge the system until the TTL expires."""

    async def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("database on fire")

    monkeypatch.setattr(S, "_run_sprint_inner", _boom)
    redis = FakeRedis()
    with pytest.raises(RuntimeError):
        await run_sprint(FakeSupabase([]), redis)
    assert "sprint:lock" not in redis.store


@pytest.mark.asyncio
async def test_r1_lock_is_taken_with_nx_and_a_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_verdict(monkeypatch)
    seen: dict[str, Any] = {}
    redis = FakeRedis()
    original = redis.set

    async def _spy(key: str, value: str, nx: bool = False, ex: int | None = None):
        seen.update({"nx": nx, "ex": ex})
        return await original(key, value, nx=nx, ex=ex)

    redis.set = _spy  # type: ignore[method-assign]
    await run_sprint(FakeSupabase([feature()]), redis)
    assert seen["nx"] is True
    assert isinstance(seen["ex"], int) and seen["ex"] > 0


# ===========================================================================
# R3 / R4 / R5 — selection
# ===========================================================================


@pytest.mark.asyncio
async def test_r3_selects_only_at_or_above_the_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A strict > would move the bar by one vote and make the threshold a lie."""
    from shared.config import settings

    t = settings.UPVOTE_THRESHOLD
    rows = [
        feature(id="exactly", upvotes=t),
        feature(id="above", upvotes=t + 3),
        feature(id="below", upvotes=t - 1),
    ]
    monkeypatch.setattr(S, "_SPRINT_CAPACITY", 5)  # exercise ranking, not the cap
    asked = stub_verdict(monkeypatch)
    await run_sprint(FakeSupabase(rows), FakeRedis())
    assert "exactly" in asked, "a feature exactly on the threshold is eligible"
    assert "above" in asked
    assert "below" not in asked


@pytest.mark.asyncio
async def test_r3_only_voting_rows_are_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        feature(id="voting", upvotes=99, status=FeatureStatus.VOTING),
        feature(id="already-building", upvotes=99, status=FeatureStatus.IN_SPRINT),
        feature(id="held", upvotes=99, status=FeatureStatus.POSTPONED_CONFLICT),
    ]
    asked = stub_verdict(monkeypatch)
    await run_sprint(FakeSupabase(rows), FakeRedis())
    assert asked == ["voting"]


@pytest.mark.asyncio
async def test_r3_takes_the_most_wanted_first(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [feature(id="mid", upvotes=20), feature(id="top", upvotes=99), feature(id="low", upvotes=9)]
    asked = stub_verdict(monkeypatch)
    await run_sprint(FakeSupabase(rows), FakeRedis())
    assert asked[0] == "top"


@pytest.mark.asyncio
async def test_r4_a_split_child_is_eligible_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """Splitting exists so the community can fund the parts separately."""
    child = feature(id="c-1", upvotes=99, parent_id="p-1", split_depth=1)
    asked = stub_verdict(monkeypatch)
    outcome = await run_sprint(FakeSupabase([child]), FakeRedis())
    assert asked == ["c-1"]
    assert outcome.selected == ("c-1",)


@pytest.mark.asyncio
async def test_r5_an_empty_board_is_a_no_op_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_verdict(monkeypatch)
    outcome = await run_sprint(FakeSupabase([feature(upvotes=0)]), FakeRedis())
    assert outcome.selected == ()
    assert outcome.held == ()


# ===========================================================================
# R6 / R7 / R17 — the gate
# ===========================================================================


@pytest.mark.asyncio
async def test_r6_blueprint_is_read_once_per_sprint(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[dict[str, Any]] = []
    monkeypatch.setattr(S, "load_blueprint", lambda **k: (reads.append(k), "BLUEPRINT")[1])
    stub_verdict(monkeypatch)
    await run_sprint(FakeSupabase([feature(id="a"), feature(id="b")]), FakeRedis())
    assert len(reads) == 1, "re-read the blueprint per feature"


@pytest.mark.asyncio
async def test_r6_blueprint_is_read_fresh_not_from_the_intake_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Judging against a stale blueprint answers a question about an older app."""
    reads: list[dict[str, Any]] = []
    monkeypatch.setattr(S, "load_blueprint", lambda **k: (reads.append(k), "BLUEPRINT")[1])
    stub_verdict(monkeypatch)
    await run_sprint(FakeSupabase([feature()]), FakeRedis())
    assert reads[0].get("refresh") is True


@pytest.mark.asyncio
async def test_r17_the_injected_judge_reaches_the_architect(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _fake(feature_map: Any, *, blueprint: str, judge: Any = None) -> BuildVerdict:
        seen["judge"] = judge
        return BuildVerdict(feature_map["feature_id"], True, Friction.green, "ok")

    monkeypatch.setattr(S, "assess_buildability", _fake)
    my_judge = judge_returning("{}")
    await run_sprint(FakeSupabase([feature()]), FakeRedis(), judge=my_judge)
    assert seen["judge"] is my_judge


# ===========================================================================
# R8 / R9 / R10 — outcomes
# ===========================================================================


@pytest.mark.asyncio
async def test_r8_a_buildable_feature_enters_the_sprint(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_verdict(monkeypatch, **{"f-1": BuildVerdict("f-1", True, Friction.green, "fits")})
    db = FakeSupabase([feature()])
    outcome = await run_sprint(db, FakeRedis())
    assert outcome.selected == ("f-1",)
    assert db.rows[0]["status"] == FeatureStatus.IN_SPRINT


@pytest.mark.asyncio
async def test_r8_yellow_friction_still_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Awkward is not impossible — holding awkward work stalls the board."""
    stub_verdict(monkeypatch, **{"f-1": BuildVerdict("f-1", True, Friction.yellow, "awkward but fine")})
    db = FakeSupabase([feature()])
    outcome = await run_sprint(db, FakeRedis())
    assert outcome.selected == ("f-1",)


@pytest.mark.asyncio
async def test_r9_an_unbuildable_feature_is_held_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = "This needs a server, and the app is a client-only build."
    stub_verdict(monkeypatch, **{"f-1": BuildVerdict("f-1", False, Friction.red, reason)})
    db = FakeSupabase([feature(postpone_count=1)])
    outcome = await run_sprint(db, FakeRedis())

    assert outcome.held == ("f-1",)
    row = db.rows[0]
    assert row["status"] == FeatureStatus.POSTPONED_CONFLICT
    assert row["ai_explanation"] == reason, "a hold with no reason reads as the system losing it"
    assert row["postpone_count"] == 2


@pytest.mark.asyncio
async def test_r10_an_outage_defers_rather_than_blaming_the_community(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSTPONED_CONFLICT says 'the architecture said no'. An outage did not."""
    stub_verdict(monkeypatch, **{"f-1": BuildabilityUnavailable("model down")})
    db = FakeSupabase([feature()])
    outcome = await run_sprint(db, FakeRedis())

    assert outcome.deferred == ("f-1",)
    assert outcome.held == ()
    assert db.rows[0]["status"] == FeatureStatus.VOTING, "left where it was, to retry next sprint"
    assert db.rows[0].get("ai_explanation") is None


# ===========================================================================
# R11 / R12 — resilience
# ===========================================================================


@pytest.mark.asyncio
async def test_r11_one_bad_feature_does_not_abandon_the_sprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_verdict(
        monkeypatch,
        **{
            "boom": RuntimeError("gate exploded"),
            "fine": BuildVerdict("fine", True, Friction.green, "ok"),
        },
    )
    monkeypatch.setattr(S, "_SPRINT_CAPACITY", 2)  # the rule only bites above one
    db = FakeSupabase([feature(id="boom", upvotes=99), feature(id="fine", upvotes=50)])
    outcome = await run_sprint(db, FakeRedis())
    assert "fine" in outcome.selected, "the community voted for this one too"


@pytest.mark.asyncio
async def test_r12_a_concurrently_changed_row_is_skipped_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selection and write are not atomic; the daemon edits the same table."""
    stub_verdict(monkeypatch)
    db = FakeSupabase([feature()])

    # Simulate the daemon moving the row between selection and write.
    original_update = _Table.update

    def _steal(self: _Table, payload: dict[str, Any]) -> _Query:
        for r in self.rows:
            if r["status"] == FeatureStatus.VOTING:
                r["status"] = FeatureStatus.ARCHIVED
        _Table.update = original_update  # only steal once
        return original_update(self, payload)

    _Table.update = _steal  # type: ignore[method-assign]
    try:
        outcome = await run_sprint(db, FakeRedis())
    finally:
        _Table.update = original_update  # type: ignore[method-assign]

    assert outcome.selected == ()
    assert db.rows[0]["status"] == FeatureStatus.ARCHIVED, "the sprint overwrote a concurrent change"


# ===========================================================================
# R13 / R14 / R20 — end-of-sprint maintenance
# ===========================================================================


@pytest.mark.asyncio
async def test_r13_stale_in_sprint_rows_roll_back_to_voting(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_verdict(monkeypatch)
    stale = feature(id="stuck", status=FeatureStatus.IN_SPRINT, updated_at="2000-01-01T00:00:00+00:00")
    db = FakeSupabase([stale])
    outcome = await run_sprint(db, FakeRedis())
    assert "stuck" in outcome.rolled_back
    assert db.rows[0]["status"] == FeatureStatus.VOTING


@pytest.mark.asyncio
async def test_r14_a_popular_feature_is_never_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is waiting for capacity, not for interest."""
    from shared.config import settings

    stub_verdict(monkeypatch)
    old_and_wanted = feature(
        id="wanted", upvotes=settings.UPVOTE_THRESHOLD + 50, updated_at="2000-01-01T00:00:00+00:00"
    )
    db = FakeSupabase([old_and_wanted])
    outcome = await run_sprint(db, FakeRedis())
    assert "wanted" not in outcome.archived
    assert db.rows[0]["status"] != FeatureStatus.ARCHIVED


@pytest.mark.asyncio
async def test_r20_nothing_is_ever_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every terminal state is a status, so the Vault can hold it and US-16 revive it."""
    stub_verdict(monkeypatch)
    rows = [feature(id="a"), feature(id="b", upvotes=0, updated_at="2000-01-01T00:00:00+00:00")]
    db = FakeSupabase(rows)
    await run_sprint(db, FakeRedis())
    assert len(db.rows) == 2


def test_r20_no_row_deletion_reaches_the_table_builder() -> None:
    """`redis.delete` (releasing the lock) is fine; `supabase.table(...).delete()` is not.

    Walk the AST rather than grepping for "delete", so the lock release does not
    mask a row deletion — the two read identically as text.
    """
    tree = ast.parse(MODULE_SRC.read_text())
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "delete":
            continue
        # Walk back down the chain: anything rooted at .table(...) is a row delete.
        chain = node.func.value
        while isinstance(chain, (ast.Call, ast.Attribute)):
            if isinstance(chain, ast.Call) and isinstance(chain.func, ast.Attribute):
                if chain.func.attr == "table":
                    offenders.append(ast.unparse(node))
                    break
                chain = chain.func.value
            elif isinstance(chain, ast.Attribute):
                chain = chain.value
            else:
                break
    assert not offenders, f"row deletion would defeat the Vault: {offenders}"


# ===========================================================================
# R15 — the ticker feed
# ===========================================================================


@pytest.mark.asyncio
async def test_r15_publishes_phase_events(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_verdict(monkeypatch)
    redis = FakeRedis()
    await run_sprint(FakeSupabase([feature()]), redis)
    assert redis.published, "a sprint nobody can see happening is not a broadcast"


@pytest.mark.asyncio
async def test_r15_never_leaks_pitch_content_to_the_public_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ticker carries phase and micro-copy only."""
    secret_title = "Blockchain-verified check-ins"
    secret_desc = "Anchor a hash of every check-in on a public chain."
    stub_verdict(monkeypatch)
    redis = FakeRedis()
    await run_sprint(
        FakeSupabase([feature(title=secret_title, description=secret_desc)]), redis
    )
    blob = " ".join(payload for _chan, payload in redis.published)
    assert secret_title not in blob
    assert secret_desc not in blob


# ===========================================================================
# R16 / non-responsibilities — the module stays in its lane
# ===========================================================================


def test_module_never_calls_an_llm_directly() -> None:
    """Every judgement goes through the architect, which owns the model pin.

    Checks imports and calls rather than substrings: the sprint legitimately
    *names* `settings.LLM_MODEL_ARCHITECT` when recording which model decided
    (R21), and a substring ban made that a false positive — the same
    substring-vs-AST trap this project has hit repeatedly.
    """
    tree = ast.parse(MODULE_SRC.read_text())

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"openai", "anthropic", "litellm", "httpx", "requests"}), (
        f"sprint_service imported an LLM/HTTP client: {imported}"
    )

    # No completion-style call anywhere.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"create", "acreate", "completion", "chat"}, (
                f"sprint_service called {ast.unparse(node.func)}"
            )


def test_the_model_id_is_only_ever_recorded_never_called() -> None:
    """Naming the model in a governance record is not the same as using it."""
    tree = ast.parse(MODULE_SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            rendered = ast.unparse(node.func)
            assert "LLM_MODEL" not in rendered, f"the model id was called: {rendered}"


def test_module_does_not_compile_or_write_the_target_prompt() -> None:
    """Selection is this module's job; US-09 builds."""
    src = MODULE_SRC.read_text()
    for forbidden in ("TARGET_PROMPT_DIR", "COMPILE_COMMAND", "subprocess"):
        assert forbidden not in src, f"sprint_service reached for {forbidden}"


def test_r16_clients_are_injected_not_constructed_at_module_scope() -> None:
    """Only main() builds real clients, so a test can drive the whole sprint."""
    tree = ast.parse(MODULE_SRC.read_text())
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            src = ast.unparse(node)
            assert "create_client" not in src and "from_url" not in src


@pytest.mark.asyncio
async def test_r10_a_deferred_feature_is_picked_up_again_next_sprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferral is not a state — it is the absence of a decision.

    The row is never written, so it stays VOTING and above threshold, and the
    next sprint's selection query finds it again with no retry bookkeeping.
    """
    db = FakeSupabase([feature(id="f-1")])

    # Sprint 1: the gate is unreachable.
    stub_verdict(monkeypatch, **{"f-1": BuildabilityUnavailable("model down")})
    first = await run_sprint(db, FakeRedis())
    assert first.deferred == ("f-1",)
    assert db.rows[0]["status"] == FeatureStatus.VOTING
    # Nothing about the deferral was recorded on the row.
    assert set(db.rows[0]) == set(feature(id="f-1")), "deferral wrote to the row"

    # Sprint 2: the gate is back. No special handling, no queue, no flag.
    stub_verdict(monkeypatch, **{"f-1": BuildVerdict("f-1", True, Friction.green, "fits")})
    second = await run_sprint(db, FakeRedis())
    assert second.selected == ("f-1",)
    assert db.rows[0]["status"] == FeatureStatus.IN_SPRINT


def test_deferred_is_not_a_persisted_status() -> None:
    """It lives on SprintOutcome, never in FeatureStatus or the database."""
    assert not hasattr(FeatureStatus, "DEFERRED")
    assert "DEFERRED" not in {s.value for s in FeatureStatus}
    src = MODULE_SRC.read_text()
    # The deferred branch must not write a status, an explanation, or a marker.
    deferred_block = src[src.index("except BuildabilityUnavailable") :][:600]
    assert "_conditional_update" not in deferred_block, "deferral wrote to Postgres"


@pytest.mark.asyncio
async def test_r3a_exactly_one_winner_per_sprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """One winner, one build, one deploy.

    The build step regenerates the whole target app from one prompt file, so a
    sprint that selected several features would put several compiles in
    contention for that file and for the single sandbox everyone is watching.
    """
    assert S._SPRINT_CAPACITY == 1, "the default is one winner per sprint"

    rows = [feature(id="top", upvotes=99), feature(id="second", upvotes=50), feature(id="third", upvotes=20)]
    asked = stub_verdict(monkeypatch)
    outcome = await run_sprint(FakeSupabase(rows), FakeRedis())

    assert asked == ["top"], "more than one feature was put through the gate"
    assert outcome.selected == ("top",)


@pytest.mark.asyncio
async def test_r3b_a_deferred_feature_rises_as_the_pool_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No 'owed a turn' bookkeeping is needed: the winner leaves VOTING.

    A newcomer with more votes delays a deferred feature by one cycle. It cannot
    displace it permanently, because being selected removes it from the pool.
    """
    db = FakeSupabase([feature(id="deferred-one", upvotes=10)])

    # Sprint 1: the gate is down — the only candidate defers, untouched.
    stub_verdict(monkeypatch, **{"deferred-one": BuildabilityUnavailable("model down")})
    assert (await run_sprint(db, FakeRedis())).deferred == ("deferred-one",)

    # A newcomer overtakes it while it waited.
    db.rows.append(feature(id="newcomer", upvotes=99))

    # Sprint 2: the newcomer wins this cycle and leaves VOTING.
    stub_verdict(monkeypatch)
    assert (await run_sprint(db, FakeRedis())).selected == ("newcomer",)

    # Sprint 3: with the newcomer gone from the pool, the deferred feature is top.
    assert (await run_sprint(db, FakeRedis())).selected == ("deferred-one",)


def test_r3b_no_deferral_memory_is_written_anywhere() -> None:
    """Remembering deferrals in Redis would depend on the infra whose failure caused them."""
    src = MODULE_SRC.read_text()
    for marker in ("deferred_set", "sprint:deferred", "owed", "sadd", "smembers"):
        assert marker not in src, f"sprint_service kept deferral state via {marker}"


# ===========================================================================
# R21 / R22 / R23 — the sprint's decisions are on the record (US-12)
# ===========================================================================


def _decisions(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what the sprint files, without a database."""
    filed: list[dict[str, Any]] = []

    async def _fake(_sb: Any, **kw: Any) -> bool:
        filed.append(kw)
        return True

    monkeypatch.setattr(S, "record_decision", _fake)
    return filed


@pytest.mark.asyncio
async def test_r21_a_selection_is_recorded_with_the_deciding_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filed = _decisions(monkeypatch)
    stub_verdict(monkeypatch, **{"f-1": BuildVerdict("f-1", True, Friction.green, "fits cleanly")})
    await run_sprint(FakeSupabase([feature()]), FakeRedis())

    friction = [d for d in filed if str(d.get("phase")) .endswith("friction")]
    assert friction, "selecting a feature left no record"
    assert friction[0]["feature_id"] == "f-1"
    assert friction[0]["model_version"] != "programmatic", "a model made this call"


@pytest.mark.asyncio
async def test_r21_a_hold_is_recorded_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holding a feature the community won is the most consequential call here."""
    filed = _decisions(monkeypatch)
    reason = "The blueprint forbids a server and this needs accounts."
    stub_verdict(monkeypatch, **{"f-1": BuildVerdict("f-1", False, Friction.red, reason)})
    await run_sprint(FakeSupabase([feature()]), FakeRedis())

    held = [d for d in filed if d.get("feature_id") == "f-1"]
    assert held, "a hold left no record"
    import json as _json

    assert reason in _json.dumps(held[0]["decision"], default=str)


@pytest.mark.asyncio
async def test_r22_a_failed_decision_write_does_not_change_the_sprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Governance is a record, not a control path."""

    async def _fails(_sb: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(S, "record_decision", _fails)
    stub_verdict(monkeypatch)
    db = FakeSupabase([feature()])
    outcome = await run_sprint(db, FakeRedis())
    assert outcome.selected == ("f-1",)
    assert db.rows[0]["status"] == FeatureStatus.IN_SPRINT


@pytest.mark.asyncio
async def test_r23_no_pitch_text_reaches_the_permanent_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decision_log is never pruned; the board already carries public text."""
    import json as _json

    filed = _decisions(monkeypatch)
    title = "Blockchain-verified check-ins"
    desc = "Anchor a hash of every check-in on a public chain."
    stub_verdict(monkeypatch)
    await run_sprint(FakeSupabase([feature(title=title, description=desc)]), FakeRedis())

    blob = _json.dumps([d["decision"] for d in filed], default=str)
    assert title not in blob
    assert desc not in blob
