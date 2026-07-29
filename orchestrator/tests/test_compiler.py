"""Contract tests for the compiler (US-09).

The whole path runs against an injected runner and a temporary copy of the
target prompt, so no test spends a token, spawns a subprocess, or writes to the
real repository.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator import compiler as C
from orchestrator.compiler import build_feature_block, compile_feature, compile_next
from shared.constants import FeatureStatus

MODULE_SRC = pathlib.Path(C.__file__)

ANCHOR = "# [COMMUNITY FEATURES INSERTION ZONE]"

BLUEPRINT = f"""# Role and Purpose
Generate a habit tracker.

# Architecture Constraint (binding)
- **Client-only monolith**: no server components, no accounts.

# Baseline Application Blueprint
## Existing Core Features
- One tap marks today's check-in; tapping again undoes it.

# EXTENSION ANCHOR POINT [DO NOT REMOVE]
{ANCHOR}
"""


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class _Query:
    def __init__(self, table: "_Table", op: str, payload: Any = None) -> None:
        self._t, self._op, self._payload = table, op, payload
        self._filters: dict[str, Any] = {}
        self._limit: int | None = None

    def select(self, *_a: Any, **_k: Any) -> "_Query":
        self._op = "select"
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self._filters[col] = val
        return self

    def order(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def limit(self, n: int) -> "_Query":
        self._limit = n
        return self

    async def execute(self) -> _Resp:
        if self._op == "insert":
            rows = self._payload if isinstance(self._payload, list) else [self._payload]
            self._t.inserted.extend(rows)
            return _Resp(list(rows))
        if self._op == "update":
            matched = [r for r in self._t.rows if all(r.get(k) == v for k, v in self._filters.items())]
            for r in matched:
                r.update(self._payload)
            return _Resp([dict(r) for r in matched])
        rows = [r for r in self._t.rows if all(r.get(k) == v for k, v in self._filters.items())]
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Resp([dict(r) for r in rows])


# Postgres rejects an unknown column with PGRST204; a fake that accepts any
# dict turns that into a green test and a hard failure the first time it runs
# for real. These mirror schema.sql.
KNOWN_COLUMNS: dict[str, set[str]] = {
    "build_logs": {"id", "version_hash", "synthesis_summary", "status", "completed_at"},
    "decision_log": {
        "id", "feature_id", "batch_id", "phase", "agent", "decision",
        "model_version", "created_at",
    },
    "feature_requests": {
        "id", "title", "description", "upvotes", "status", "parent_id", "split_depth",
        "unlock_threshold", "postpone_count", "ai_explanation", "merge_count",
        "extends_id", "extends_title", "author_id", "author_handle",
        "created_at", "updated_at",
    },
}


class _Table:
    def __init__(self, rows: list[dict[str, Any]], name: str = "") -> None:
        self.rows = rows
        self.name = name
        self.inserted: list[dict[str, Any]] = []

    def _check_columns(self, payload: Any) -> None:
        known = KNOWN_COLUMNS.get(self.name)
        if known is None:
            return
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            unknown = set(row) - known
            assert not unknown, (
                f"PGRST204 waiting to happen: {self.name} has no column(s) {sorted(unknown)}"
            )

    def select(self, *_a: Any, **_k: Any) -> _Query:
        return _Query(self, "select")

    def insert(self, payload: Any, **_k: Any) -> _Query:
        self._check_columns(payload)
        return _Query(self, "insert", payload)

    def update(self, payload: Any, **_k: Any) -> _Query:
        self._check_columns(payload)
        return _Query(self, "update", payload)


class FakeSupabase:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.tables: dict[str, _Table] = {}
        self._seed = rows or []

    def table(self, name: str) -> _Table:
        if name not in self.tables:
            self.tables[name] = _Table(self._seed if "feature_requests" in name else [], name)
        return self.tables[name]

    def inserted(self, name: str) -> list[dict[str, Any]]:
        return self.tables.get(name, _Table([], name)).inserted


class FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1

    async def aclose(self) -> None:  # pragma: no cover
        return None


def feature(**over: Any) -> dict[str, Any]:
    row = {
        "id": "f-1",
        "title": "Streak counter on every habit",
        "description": "Show a flame badge on each habit card counting consecutive checked days.",
        "status": FeatureStatus.IN_SPRINT,
        "upvotes": 12,
    }
    row.update(over)
    return row


def runner_returning(code: int, out: str = "", err: str = ""):
    calls: list[dict[str, Any]] = []

    async def _run(command: str, cwd: Any, env: Any) -> tuple[int, str, str]:
        calls.append({"command": command, "cwd": cwd, "env": dict(env)})
        return code, out, err

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


@pytest.fixture
def target(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A throwaway target app: the blueprint plus a generated source file."""
    (tmp_path / C._PROMPT_FILENAME).write_text(BLUEPRINT)
    (tmp_path / C._GENERATED_SOURCE_FILENAME).write_text("export const App = () => null;\n")
    # Settings is a frozen pydantic model, so swap the object the module reads
    # rather than trying to mutate a field on it.
    monkeypatch.setattr(
        C,
        "settings",
        SimpleNamespace(
            TARGET_PROMPT_DIR=tmp_path,
            COMPILE_COMMAND="pdd --local --force generate x --output y",
            TOKENROUTER_API_KEY=SimpleNamespace(get_secret_value=lambda: "tr-test-key"),
        ),
    )
    return tmp_path


def prompt_text(target: pathlib.Path) -> str:
    return (target / C._PROMPT_FILENAME).read_text()


# ===========================================================================
# R1 / R2 / R19 — where the block goes
# ===========================================================================


@pytest.mark.asyncio
async def test_r1_block_lands_below_the_anchor(target: pathlib.Path) -> None:
    await compile_feature(feature(), FakeSupabase([feature()]), FakeRedis(), runner=runner_returning(0))
    text = prompt_text(target)
    assert text.index(ANCHOR) < text.index("Streak counter on every habit")


@pytest.mark.asyncio
async def test_r1_everything_above_the_anchor_is_preserved_byte_for_byte(
    target: pathlib.Path,
) -> None:
    """The blueprint above the anchor is what every later judgement is made against."""
    head_before = BLUEPRINT[: BLUEPRINT.index(ANCHOR)]
    await compile_feature(feature(), FakeSupabase([feature()]), FakeRedis(), runner=runner_returning(0))
    text = prompt_text(target)
    assert text[: text.index(ANCHOR)] == head_before


@pytest.mark.asyncio
async def test_r2_a_missing_anchor_fails_loudly_and_writes_nothing(
    target: pathlib.Path,
) -> None:
    """Appending to the end of an unrecognised file corrupts it."""
    (target / C._PROMPT_FILENAME).write_text("# some other file entirely\n")
    before = prompt_text(target)
    outcome = await compile_feature(
        feature(), FakeSupabase([feature()]), FakeRedis(), runner=runner_returning(0)
    )
    assert outcome.ok is False
    assert prompt_text(target) == before


def test_r19_the_block_states_behaviour_and_carries_the_title() -> None:
    block = build_feature_block(feature())
    assert "Streak counter on every habit" in block
    assert "flame badge" in block


def test_r4_the_block_carries_the_feature_id_as_a_comment() -> None:
    block = build_feature_block(feature(id="abc-123"))
    assert "abc-123" in block


# ===========================================================================
# R3 / R4 / R5 — the prompt file is handled carefully
# ===========================================================================


@pytest.mark.asyncio
async def test_r4_a_second_compile_does_not_append_the_block_twice(
    target: pathlib.Path,
) -> None:
    sb = FakeSupabase([feature()])
    await compile_feature(feature(), sb, FakeRedis(), runner=runner_returning(0))
    first = prompt_text(target)
    await compile_feature(feature(), sb, FakeRedis(), runner=runner_returning(0))
    assert prompt_text(target).count("Streak counter on every habit") == first.count(
        "Streak counter on every habit"
    )


@pytest.mark.asyncio
async def test_r5_a_failed_compile_restores_the_prompt_exactly(
    target: pathlib.Path,
) -> None:
    """A failed feature's block left behind poisons every later compile."""
    before = prompt_text(target)
    outcome = await compile_feature(
        feature(), FakeSupabase([feature()]), FakeRedis(),
        runner=runner_returning(1, err="TypeError: cannot read property"),
    )
    assert outcome.ok is False
    assert prompt_text(target) == before
    assert "Streak counter" not in prompt_text(target)


@pytest.mark.asyncio
async def test_r3_the_prompt_is_written_atomically(target: pathlib.Path) -> None:
    """A crash mid-write must not truncate the blueprint."""
    src = MODULE_SRC.read_text()
    assert "os.replace" in src or "Path.replace" in src or ".replace(" in src
    # and no temp file is left lying around after a successful run
    await compile_feature(feature(), FakeSupabase([feature()]), FakeRedis(), runner=runner_returning(0))
    leftovers = [p.name for p in target.iterdir() if p.suffix in {".tmp", ".swp"}]
    assert leftovers == []


# ===========================================================================
# R6 / R7 / R8 — running the command
# ===========================================================================


@pytest.mark.asyncio
async def test_r6_the_command_runs_in_the_target_dir_with_the_token_ceiling(
    target: pathlib.Path,
) -> None:
    """Unset, pdd truncates generated source mid-file and it reads as a broken compile."""
    run = runner_returning(0)
    await compile_feature(feature(), FakeSupabase([feature()]), FakeRedis(), runner=run)
    call = run.calls[0]  # type: ignore[attr-defined]
    assert str(call["cwd"]) == str(target)
    assert call["env"].get("PDD_COMMAND_MAX_OUTPUT_TOKENS") == "32000"
    assert call["command"] == C.settings.COMPILE_COMMAND


@pytest.mark.asyncio
async def test_r7_no_pitch_content_reaches_the_command(target: pathlib.Path) -> None:
    """A shell is only safe while the string is operator configuration."""
    run = runner_returning(0)
    nasty = "Streaks; rm -rf / #"
    await compile_feature(
        feature(title=nasty, description="d" * 40), FakeSupabase([feature()]), FakeRedis(), runner=run
    )
    assert nasty not in run.calls[0]["command"]  # type: ignore[attr-defined]
    assert run.calls[0]["command"] == C.settings.COMPILE_COMMAND  # type: ignore[attr-defined]


def test_r7_the_command_is_never_interpolated() -> None:
    """Statically: COMPILE_COMMAND must not be an f-string or a concatenation."""
    tree = ast.parse(MODULE_SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            rendered = ast.unparse(node)
            assert "COMPILE_COMMAND" not in rendered, f"command interpolated: {rendered}"


@pytest.mark.asyncio
async def test_r8_a_timeout_is_a_failure_not_a_hang(target: pathlib.Path) -> None:
    async def _timeout(_c: str, _cwd: Any, _env: Any) -> tuple[int, str, str]:
        raise TimeoutError("compile exceeded the limit")

    outcome = await compile_feature(
        feature(), FakeSupabase([feature()]), FakeRedis(), runner=_timeout
    )
    assert outcome.ok is False
    assert prompt_text(target) == BLUEPRINT


@pytest.mark.asyncio
async def test_r9_only_a_bounded_tail_of_the_log_is_kept(target: pathlib.Path) -> None:
    huge = "\n".join(f"line {i}" for i in range(5000))
    outcome = await compile_feature(
        feature(), FakeSupabase([feature()]), FakeRedis(), runner=runner_returning(1, err=huge)
    )
    assert len(outcome.log_tail.splitlines()) <= 200
    assert "line 4999" in outcome.log_tail, "the tail is where the error is"


# ===========================================================================
# R10 / R11 / R12 — the feature always ends somewhere honest
# ===========================================================================


@pytest.mark.asyncio
async def test_r10_success_moves_the_feature_to_compiled(target: pathlib.Path) -> None:
    rows = [feature()]
    sb = FakeSupabase(rows)
    outcome = await compile_feature(feature(), sb, FakeRedis(), runner=runner_returning(0))
    assert outcome.ok is True
    assert rows[0]["status"] == FeatureStatus.COMPILED


@pytest.mark.asyncio
async def test_r10_failure_returns_the_feature_to_voting(target: pathlib.Path) -> None:
    """It kept its votes and was not rejected — a later sprint can try again."""
    rows = [feature()]
    sb = FakeSupabase(rows)
    outcome = await compile_feature(feature(), sb, FakeRedis(), runner=runner_returning(1, err="boom"))
    assert outcome.ok is False
    assert rows[0]["status"] == FeatureStatus.VOTING


@pytest.mark.asyncio
async def test_r10_a_failed_compile_is_not_a_holding_verdict(target: pathlib.Path) -> None:
    """Holding means the architect judged the idea; a build error judged nothing."""
    rows = [feature()]
    await compile_feature(feature(), FakeSupabase(rows), FakeRedis(), runner=runner_returning(1))
    assert rows[0]["status"] != FeatureStatus.POSTPONED_CONFLICT
    assert rows[0].get("ai_explanation") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, 1])
async def test_r11_the_feature_never_stays_in_sprint(target: pathlib.Path, code: int) -> None:
    rows = [feature()]
    await compile_feature(feature(), FakeSupabase(rows), FakeRedis(), runner=runner_returning(code))
    assert rows[0]["status"] != FeatureStatus.IN_SPRINT


@pytest.mark.asyncio
async def test_r11_even_an_exploding_runner_resolves_the_feature(target: pathlib.Path) -> None:
    async def _boom(_c: str, _cwd: Any, _env: Any) -> tuple[int, str, str]:
        raise RuntimeError("the runner itself broke")

    rows = [feature()]
    outcome = await compile_feature(feature(), FakeSupabase(rows), FakeRedis(), runner=_boom)
    assert outcome.ok is False
    assert rows[0]["status"] == FeatureStatus.VOTING


# ===========================================================================
# R13 / R14 / R15 — the record
# ===========================================================================


@pytest.mark.asyncio
async def test_r13_every_attempt_writes_a_build_log(target: pathlib.Path) -> None:
    sb = FakeSupabase([feature()])
    await compile_feature(feature(), sb, FakeRedis(), runner=runner_returning(0))
    logs = sb.inserted("build_logs")
    assert len(logs) == 1
    assert logs[0]["status"] == "success"

    sb2 = FakeSupabase([feature()])
    await compile_feature(feature(id="f-2"), sb2, FakeRedis(), runner=runner_returning(1))
    assert sb2.inserted("build_logs")[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_r14_the_hash_is_of_the_generated_source(target: pathlib.Path) -> None:
    """Two compiles emitting identical code must produce identical hashes."""
    first = await compile_feature(feature(), FakeSupabase([feature()]), FakeRedis(), runner=runner_returning(0))
    second = await compile_feature(feature(id="f-2"), FakeSupabase([feature()]), FakeRedis(), runner=runner_returning(0))
    assert first.version_hash == second.version_hash

    (target / C._GENERATED_SOURCE_FILENAME).write_text("export const App = () => <div/>;\n")
    third = await compile_feature(feature(id="f-3"), FakeSupabase([feature()]), FakeRedis(), runner=runner_returning(0))
    assert third.version_hash != first.version_hash


def test_r14_the_hash_never_walks_the_target_directory() -> None:
    """TARGET_PROMPT_DIR holds node_modules/, dist/ and .git/.

    AST, not substring: the first version of this test matched the word "rglob"
    inside the comment explaining why rglob is forbidden, which is the same
    false positive this project has hit three times before.
    """
    tree = ast.parse(MODULE_SRC.read_text())
    walkers = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    forbidden = walkers & {"rglob", "glob", "iterdir", "walk"}
    assert not forbidden, f"a recursive hash would cover node_modules and .git: {forbidden}"


@pytest.mark.asyncio
async def test_r15_the_decision_log_does_not_claim_a_model(target: pathlib.Path) -> None:
    sb = FakeSupabase([feature()])
    await compile_feature(feature(), sb, FakeRedis(), runner=runner_returning(0))
    entries = sb.inserted("decision_log")
    assert entries, "the compile step left no governance record"
    assert entries[0]["phase"] == "compile"
    assert entries[0]["model_version"] == "programmatic"


# ===========================================================================
# R16 — the public ticker
# ===========================================================================


@pytest.mark.asyncio
async def test_r16_publishes_progress_without_leaking_content(target: pathlib.Path) -> None:
    secret_desc = "Anchor a hash of every check-in on a public chain."
    redis = FakeRedis()
    await compile_feature(
        feature(description=secret_desc), FakeSupabase([feature()]), redis,
        runner=runner_returning(1, err="stack trace with " + secret_desc),
    )
    assert redis.published, "a compile nobody can see is not a broadcast"
    blob = " ".join(p for _c, p in redis.published)
    assert secret_desc not in blob
    assert "stack trace" not in blob


# ===========================================================================
# R18 / R20 — the queue, and staying in the compiler's lane
# ===========================================================================


@pytest.mark.asyncio
async def test_r18_an_empty_queue_returns_none(target: pathlib.Path) -> None:
    assert await compile_next(FakeSupabase([]), FakeRedis(), runner=runner_returning(0)) is None


@pytest.mark.asyncio
async def test_r18_compile_next_picks_up_an_in_sprint_feature(target: pathlib.Path) -> None:
    rows = [feature(id="waiting")]
    outcome = await compile_next(FakeSupabase(rows), FakeRedis(), runner=runner_returning(0))
    assert outcome is not None and outcome.feature_id == "waiting"


def test_r20_the_generated_source_is_never_edited() -> None:
    """Prompts are source; a hand-edited artifact vanishes at the next compile."""
    src = MODULE_SRC.read_text()
    assert "_GENERATED_SOURCE_FILENAME" in src
    # it may be read (for the hash) but never written
    for bad in ("write_text(", "write_bytes("):
        for line in src.splitlines():
            if bad in line and "_GENERATED_SOURCE_FILENAME" in line:
                raise AssertionError(f"compiler writes generated source: {line.strip()}")


def test_module_never_calls_an_llm_or_git() -> None:
    src = MODULE_SRC.read_text()
    for forbidden in ("openai", "AsyncOpenAI", "LLM_MODEL", "chat.completions", '"git"', "'git'"):
        assert forbidden not in src, f"compiler reached for {forbidden}"


@pytest.mark.asyncio
async def test_r6a_the_pdd_credential_is_passed_to_the_child(target: pathlib.Path) -> None:
    """pdd reads .env from its own cwd — the target repo, which has none.

    Inheriting the key from the launching shell works on a developer's machine
    and fails in every daemon, reporting only "All candidate models failed".
    """
    run = runner_returning(0)
    await compile_feature(feature(), FakeSupabase([feature()]), FakeRedis(), runner=run)
    env = run.calls[0]["env"]  # type: ignore[attr-defined]
    assert env.get("TOKENROUTER_API_KEY") == "tr-test-key"
