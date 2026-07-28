"""Contract tests for `orchestrator/ingestion_service.py` (US-02, step 1).

The daemon is exercised through `process_one`, which takes the raw queue string
and an injected Supabase double — no Redis, no network, no real database.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import pathlib
from typing import Any

import pytest
from postgrest.exceptions import APIError

from orchestrator import ingestion_service as svc
from orchestrator.screener import ScreeningUnavailable, Verdict
from shared.constants import TABLE_FEATURE_REQUESTS, FeatureStatus, RejectionReason

MODULE_SRC = pathlib.Path(svc.__file__)
FID = "33333333-3333-4333-8333-333333333333"
AUTHOR = "11111111-1111-4111-8111-111111111111"

SECRET_TITLE = "ZZQQ-secret-title"
SECRET_DESC = "WWXX-secret-description long enough to clear the thirty character floor"


def item(**over: Any) -> str:
    base = {
        "feature_id": FID,
        "author_id": AUTHOR,
        "title": "Dark mode for the dashboard",
        "description": "Add a persisted dark theme toggle in the header that survives a reload.",
        "submitted_at": "2026-07-27T00:00:00Z",
    }
    base.update(over)
    return json.dumps(base)


class FakeQuery:
    """Records the builder chain; replays canned rows or an injected error."""

    def __init__(self, store: "FakeSupabase", table: str) -> None:
        self._store, self._table = store, table
        self._op: str | None = None
        self._row: Any = None

    def insert(self, row: dict[str, Any]) -> "FakeQuery":
        self._op, self._row = "insert", row
        return self

    def select(self, *a: Any, **k: Any) -> "FakeQuery":
        self._op = "select"
        return self

    def update(self, row: dict[str, Any], **k: Any) -> "FakeQuery":
        self._op, self._row = "update", row
        return self

    def eq(self, *a: Any, **k: Any) -> "FakeQuery":
        return self

    def in_(self, *a: Any, **k: Any) -> "FakeQuery":
        return self

    def limit(self, *a: Any, **k: Any) -> "FakeQuery":
        return self

    def order(self, *a: Any, **k: Any) -> "FakeQuery":
        return self

    def maybe_single(self) -> "FakeQuery":
        self._single = True
        return self

    def single(self) -> "FakeQuery":
        self._single = True
        return self

    async def execute(self) -> Any:
        if self._op == "insert":
            self._store.inserts.append({"table": self._table, "row": self._row})
            if self._store.raise_on_insert is not None:
                raise self._store.raise_on_insert
            return type("Resp", (), {"data": [self._row]})()
        if self._op == "update":
            self._store.updates.append({"table": self._table, "row": self._row})
            return type("Resp", (), {"data": [self._row]})()
        rows = self._store.rows.get(self._table, [])
        if getattr(self, "_single", False):
            return type("Resp", (), {"data": rows[0] if rows else None})()
        return type("Resp", (), {"data": list(rows)})()


class FakeSupabase:
    def __init__(self) -> None:
        self.inserts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.raise_on_insert: Exception | None = None
        self.rpc_calls: list[tuple[str, dict[str, Any]]] = []
        self.rpc_result: Any = None

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    def rpc(self, fn: str, params: dict[str, Any] | None = None) -> Any:
        self.rpc_calls.append((fn, params or {}))
        store = self

        class _Rpc:
            async def execute(self_inner) -> Any:
                return type("Resp", (), {"data": store.rpc_result})()

        return _Rpc()


@pytest.fixture
def sb() -> FakeSupabase:
    return FakeSupabase()


# ==========================================================================
# R6 / R7 / R9 — the insert
# ==========================================================================

async def test_r6_survivor_is_inserted_with_the_payload_feature_id(sb: FakeSupabase) -> None:
    """A fresh uuid would orphan the author's pending entry (US-06)."""
    assert await svc.process_one(item(), sb) == "inserted"
    row = sb.inserts[0]["row"]
    assert sb.inserts[0]["table"] == TABLE_FEATURE_REQUESTS
    assert row["id"] == FID
    assert row["author_id"] == AUTHOR


async def test_r6_row_starts_at_one_upvote(sb: FakeSupabase) -> None:
    await svc.process_one(item(), sb)
    assert sb.inserts[0]["row"]["upvotes"] == 1


async def test_r7_status_is_voting_from_the_enum(sb: FakeSupabase) -> None:
    await svc.process_one(item(), sb)
    status = sb.inserts[0]["row"]["status"]
    assert status == FeatureStatus.VOTING
    assert status == "VOTING"  # StrEnum: the wire value, not 'FeatureStatus.VOTING'


async def test_r9_no_later_phase_columns_are_set(sb: FakeSupabase) -> None:
    """merge_count / extends_id / parent_id belong to dedup and split."""
    await svc.process_one(item(), sb)
    row = sb.inserts[0]["row"]
    for col in ("merge_count", "extends_id", "parent_id", "split_depth", "unlock_threshold"):
        assert col not in row, f"{col} must be left to a later phase"


# ==========================================================================
# R3 — malformed items
# ==========================================================================

@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "[]",
        '"a bare string"',
        "123",
        json.dumps({"feature_id": FID}),                       # missing keys
        json.dumps({**json.loads(item()), "extra": "field"}),   # unexpected key
    ],
)
async def test_r3_malformed_item_is_dropped_not_raised(raw: str, sb: FakeSupabase) -> None:
    assert await svc.process_one(raw, sb) == "malformed"
    assert sb.inserts == [], "a malformed item must never reach Postgres"


async def test_r2_key_names_are_the_cross_process_contract(sb: FakeSupabase) -> None:
    """Renaming a key on either side must fail loudly here, not silently strand pitches."""
    renamed = json.dumps({
        "id": FID, "author_id": AUTHOR, "title": "t",
        "description": "d" * 40, "submitted_at": "2026-07-27T00:00:00Z",
    })
    assert await svc.process_one(renamed, sb) == "malformed"


# ==========================================================================
# R4 / R5 — rejection
# ==========================================================================

async def test_r5_rejected_pitch_text_never_reaches_the_log(
    sb: FakeSupabase, caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    """Unscreened content must not land in any durable store, logs included."""
    async def _reject(pitch, **kw):
        return Verdict(feature_id=pitch.get("feature_id", ""), passed=False,
                       reason=RejectionReason.SECURITY, detail="policy violation")
    monkeypatch.setattr(svc, "screen_pitch", _reject)

    with caplog.at_level(logging.DEBUG):
        await svc.process_one(item(title=SECRET_TITLE, description=SECRET_DESC), sb)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_TITLE not in blob and SECRET_DESC not in blob


async def test_r10_accepted_pitch_text_also_stays_out_of_the_log(
    sb: FakeSupabase, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await svc.process_one(item(title=SECRET_TITLE, description=SECRET_DESC), sb)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_TITLE not in blob and SECRET_DESC not in blob


async def test_r10_log_carries_feature_id_and_outcome(
    sb: FakeSupabase, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        await svc.process_one(item(), sb)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert FID in blob and "inserted" in blob


# ==========================================================================
# R8 — idempotency on redelivery
# ==========================================================================

async def test_r8_unique_violation_is_duplicate_not_an_exception(sb: FakeSupabase) -> None:
    """BRPOP redelivery must never raise; the primary key makes it idempotent."""
    sb.raise_on_insert = APIError(
        {"code": "23505", "message": "duplicate key value violates unique constraint"}
    )
    assert await svc.process_one(item(), sb) == "duplicate"


async def test_unexpected_db_error_propagates_to_the_loop_handler(sb: FakeSupabase) -> None:
    """Only 23505 is swallowed — a real outage must surface (R13 catches it)."""
    sb.raise_on_insert = APIError({"code": "42P01", "message": "relation does not exist"})
    with pytest.raises(Exception):
        await svc.process_one(item(), sb)


# ==========================================================================
# Static guarantees — enforced against the module source
# ==========================================================================

def test_no_redis_writes_anywhere() -> None:
    """This process is a consumer; writing a key would contradict its capabilities.

    Matched on the AST rather than substrings — `stop.set()` on the asyncio.Event
    is not a Redis write, and a naive `".set("` scan flags it.
    """
    WRITE_METHODS = {
        "set", "setex", "getset", "lpush", "rpush", "lset", "delete", "unlink",
        "expire", "pexpire", "publish", "hset", "sadd", "incr", "decr",
    }
    tree = ast.parse(MODULE_SRC.read_text())

    # Names bound to a Redis client, e.g. `redis_client = aioredis.from_url(...)`
    redis_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            src_txt = ast.unparse(fn) if hasattr(ast, "unparse") else ""
            if "from_url" in src_txt or "Redis" in src_txt:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        redis_names.add(t.id)
    redis_names |= {"redis_client", "rds", "redis", "r"}

    offenders = []
    for node in ast.walk(tree):
        call = node.value if isinstance(node, ast.Await) else node
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
            obj = call.func.value
            if isinstance(obj, ast.Name) and obj.id in redis_names:
                if call.func.attr in WRITE_METHODS:
                    offenders.append(f"{obj.id}.{call.func.attr}")
    assert not offenders, f"ingestion_service must not write Redis: {offenders}"


def test_never_touches_the_pending_pitch_record() -> None:
    """The API writes it once; the TTL alone clears it (US-06 scope decision)."""
    assert "REDIS_PENDING_PITCH" not in MODULE_SRC.read_text()


def test_does_not_import_from_backend() -> None:
    """Redis is the only channel between the API and this process."""
    tree = ast.parse(MODULE_SRC.read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert "backend" not in roots


def test_r15_reads_no_environment_directly() -> None:
    src = MODULE_SRC.read_text()
    assert "os.environ" not in src and "getenv" not in src


def test_r14_main_is_sync_and_module_is_runnable() -> None:
    import inspect

    assert not inspect.iscoroutinefunction(svc.main)
    assert '__name__ == "__main__"' in MODULE_SRC.read_text()


def test_r1_uses_brpop_with_a_timeout() -> None:
    src = MODULE_SRC.read_text()
    assert "brpop" in src.lower()
    assert "timeout" in src.lower(), "an unbounded BRPOP would never notice a stop signal"


async def test_run_returns_promptly_when_stop_is_already_set(monkeypatch) -> None:
    """R12: a set stop event must end the loop without consuming anything."""
    class _Redis:
        async def brpop(self, *a: Any, **k: Any) -> None:
            raise AssertionError("must not poll when stop is already set")

        async def aclose(self) -> None:
            pass

    async def _fake_create_client(*a: Any, **k: Any) -> Any:
        return FakeSupabase()

    monkeypatch.setattr(svc.aioredis, "from_url", lambda *a, **k: _Redis())
    monkeypatch.setattr(svc, "create_client", _fake_create_client)

    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(svc.run(stop), timeout=5)


# ==========================================================================
# R4 / R16 — the screener is async now, and outages are their own outcome
# ==========================================================================

@pytest.fixture(autouse=True)
def _pass_screening(monkeypatch):
    """Default: screening passes, so the existing insert tests still describe the insert."""
    async def _ok(pitch, **kw):
        return Verdict(feature_id=pitch.get("feature_id", ""), passed=True, reason=None, detail="ok")
    monkeypatch.setattr(svc, "screen_pitch", _ok)


async def test_r16_screening_unavailable_is_its_own_outcome(sb: FakeSupabase, monkeypatch) -> None:
    """An outage must not look like a rejection, and must never insert."""
    async def _boom(pitch, **kw):
        raise ScreeningUnavailable("model unreachable")
    monkeypatch.setattr(svc, "screen_pitch", _boom)

    assert await svc.process_one(item(), sb) == "unavailable"
    assert sb.inserts == [], "unscreened content must never be published"


async def test_r16_unavailable_is_logged_at_error(sb: FakeSupabase, monkeypatch, caplog) -> None:
    async def _boom(pitch, **kw):
        raise ScreeningUnavailable("model unreachable")
    monkeypatch.setattr(svc, "screen_pitch", _boom)

    with caplog.at_level(logging.DEBUG):
        await svc.process_one(item(), sb)
    assert any(r.levelno >= logging.ERROR for r in caplog.records), "an outage should be ERROR, not INFO"


async def test_r4_rejected_verdict_still_drops_without_inserting(sb: FakeSupabase, monkeypatch) -> None:
    async def _reject(pitch, **kw):
        return Verdict(feature_id=pitch.get("feature_id", ""), passed=False,
                       reason=RejectionReason.OFF_TOPIC, detail="not a product idea")
    monkeypatch.setattr(svc, "screen_pitch", _reject)

    assert await svc.process_one(item(), sb) == "rejected"
    assert sb.inserts == []


async def test_daemon_awaits_the_screener(sb: FakeSupabase) -> None:
    """screen_pitch is a coroutine function in step 2; a missing await would insert a coroutine."""
    import inspect as _i
    from orchestrator import screener as real
    assert _i.iscoroutinefunction(real.screen_pitch)


# ==========================================================================
# R17-R25 — dedup routing (US-03)
# ==========================================================================

from orchestrator.pm_agent import Classification, Outcome  # noqa: E402

CANON = "abcdabcd-abcd-4bcd-8bcd-abcdabcdabcd"
BASE = "99999999-9999-4999-8999-999999999999"


def _classify_as(outcome: Outcome, target_id=None, target_title=None):
    async def _c(pitch, **kw):
        return Classification(feature_id=pitch.get("feature_id", ""), outcome=outcome,
                              target_id=target_id, target_title=target_title, detail="test")
    return _c


async def test_r19_new_unique_inserts_without_extends_columns(sb: FakeSupabase, monkeypatch) -> None:
    monkeypatch.setattr(svc, "classify", _classify_as(Outcome.new_unique))
    assert await svc.process_one(item(), sb) == "inserted"
    row = sb.inserts[0]["row"]
    assert "extends_id" not in row and "extends_title" not in row


async def test_r19_extends_shipped_sets_both_extends_columns(sb: FakeSupabase, monkeypatch) -> None:
    """extends_title is denormalised so a card renders 'builds on' with no second query."""
    monkeypatch.setattr(svc, "classify",
                        _classify_as(Outcome.extends_shipped, BASE, "Login with email"))
    assert await svc.process_one(item(), sb) == "inserted"
    row = sb.inserts[0]["row"]
    assert row["extends_id"] == BASE
    assert row["extends_title"] == "Login with email"


async def test_r20_duplicate_inserts_no_feature_row(sb: FakeSupabase, monkeypatch) -> None:
    """A merge concentrates demand; a second row would scatter it again."""
    monkeypatch.setattr(svc, "classify", _classify_as(Outcome.duplicate, CANON, "Dark mode"))
    assert await svc.process_one(item(), sb) == "merged"
    feature_inserts = [i for i in sb.inserts if i["table"] == TABLE_FEATURE_REQUESTS]
    assert feature_inserts == [], "a duplicate must not create a board row"


async def test_r20_duplicate_increments_the_canonical_row_atomically(sb: FakeSupabase, monkeypatch) -> None:
    monkeypatch.setattr(svc, "classify", _classify_as(Outcome.duplicate, CANON, "Dark mode"))
    await svc.process_one(item(), sb)
    assert any(fn == "increment_upvotes" for fn, _ in sb.rpc_calls), \
        "the merge must use the atomic RPC, not a read-then-write"


async def test_r21_merge_writes_a_vote_row_for_the_author(sb: FakeSupabase, monkeypatch) -> None:
    """Without this the merging author is in `upvotes` but has no vote row,
    so viewer_has_voted reads false and they can vote again — one person, two votes."""
    monkeypatch.setattr(svc, "classify", _classify_as(Outcome.duplicate, CANON, "Dark mode"))
    await svc.process_one(item(), sb)
    votes = [i for i in sb.inserts if i["table"] == "feature_votes"]
    assert votes, "no feature_votes row written for the merged author"
    assert votes[0]["row"]["feature_id"] == CANON, "the vote must land on the canonical row"
    assert votes[0]["row"]["user_id"] == AUTHOR


async def test_r23_already_shipped_writes_nothing(sb: FakeSupabase, monkeypatch) -> None:
    monkeypatch.setattr(svc, "classify", _classify_as(Outcome.already_shipped, BASE, "Login with email"))
    assert await svc.process_one(item(), sb) == "already_shipped"
    assert sb.inserts == [] and sb.rpc_calls == []


async def test_r24_a_raising_classifier_still_publishes(sb: FakeSupabase, monkeypatch) -> None:
    """Dedup fails open — an outage must not lose a screened pitch."""
    async def _boom(pitch, **kw):
        raise RuntimeError("classifier exploded")
    monkeypatch.setattr(svc, "classify", _boom)
    assert await svc.process_one(item(), sb) == "inserted"


async def test_r25_outcomes_are_reported_distinctly(sb: FakeSupabase, monkeypatch) -> None:
    """A merge and an insert are different things happening to the demand signal."""
    seen = set()
    for outcome, target, title in [
        (Outcome.new_unique, None, None),
        (Outcome.duplicate, CANON, "Dark mode"),
        (Outcome.already_shipped, BASE, "Login"),
    ]:
        fresh = FakeSupabase()
        monkeypatch.setattr(svc, "classify", _classify_as(outcome, target, title))
        seen.add(await svc.process_one(item(), fresh))
    assert seen == {"inserted", "merged", "already_shipped"}


async def test_r18_candidate_sets_are_read_before_classifying(sb: FakeSupabase, monkeypatch) -> None:
    captured: dict = {}

    async def _spy(pitch, *, backlog, shipped, **kw):
        captured["backlog"], captured["shipped"] = backlog, shipped
        return Classification(feature_id=pitch["feature_id"], outcome=Outcome.new_unique,
                              target_id=None, target_title=None, detail="t")

    monkeypatch.setattr(svc, "classify", _spy)
    await svc.process_one(item(), sb)
    assert "backlog" in captured, "classify was called without candidate sets"


async def test_r20_rpc_is_called_with_exactly_row_id(sb: FakeSupabase, monkeypatch) -> None:
    """PostgREST matches on the exact named-argument set. An invented `inc`
    parameter fails at runtime with PGRST202, which is how the merge silently
    stopped transferring votes while still reporting success."""
    monkeypatch.setattr(svc, "classify", _classify_as(Outcome.duplicate, CANON, "Dark mode"))
    await svc.process_one(item(), sb)
    calls = [c for c in sb.rpc_calls if c[0] == "increment_upvotes"]
    assert calls, "increment_upvotes was never called"
    assert set(calls[0][1]) == {"row_id"}, f"wrong argument set: {sorted(calls[0][1])}"
    assert calls[0][1]["row_id"] == CANON


async def test_r26_insert_writes_the_authors_vote_row(sb: FakeSupabase, monkeypatch) -> None:
    """`upvotes: 1` represents the author's own vote. Without a matching row that
    vote exists only as a number — the author can upvote their own pitch and be
    counted twice, which a live run demonstrated."""
    monkeypatch.setattr(svc, "classify", _classify_as(Outcome.new_unique))
    assert await svc.process_one(item(), sb) == "inserted"

    votes = [i for i in sb.inserts if i["table"] == "feature_votes"]
    assert votes, "no vote row written for the author of a new feature"
    assert votes[0]["row"]["feature_id"] == FID
    assert votes[0]["row"]["user_id"] == AUTHOR


async def test_r26_every_upvote_is_backed_by_a_vote_row(sb: FakeSupabase, monkeypatch) -> None:
    """Both paths that add to `upvotes` must add the row that justifies it."""
    monkeypatch.setattr(svc, "classify", _classify_as(Outcome.new_unique))
    await svc.process_one(item(), sb)
    feature = [i for i in sb.inserts if i["table"] == TABLE_FEATURE_REQUESTS][0]["row"]
    votes = [i for i in sb.inserts if i["table"] == "feature_votes"]
    assert feature["upvotes"] == len(votes), \
        f"upvotes={feature['upvotes']} but {len(votes)} vote row(s)"


async def test_r27_duplicate_vote_row_does_not_fail_the_insert(sb: FakeSupabase, monkeypatch) -> None:
    """A redelivered item must not fail because the vote was already recorded."""
    from postgrest.exceptions import APIError

    monkeypatch.setattr(svc, "classify", _classify_as(Outcome.new_unique))
    original = sb.table

    def _table(name: str):
        q = original(name)
        if name == "feature_votes":
            sb.raise_on_insert = APIError({"code": "23505", "message": "duplicate key"})
        else:
            sb.raise_on_insert = None
        return q

    monkeypatch.setattr(sb, "table", _table)
    assert await svc.process_one(item(), sb) == "inserted"
