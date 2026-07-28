"""Contract tests for `orchestrator/architect.py` (US-08 intake half).

Every test injects a fake `judge` and a literal blueprint string — nothing here
reads the target repo or reaches a network.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any

import pytest

from orchestrator.architect import (
    ChildSpec,
    Friction,
    Shape,
    decide_shape,
    load_blueprint,
)
from shared.constants import FeatureStatus

MODULE_SRC = pathlib.Path(__file__).resolve().parents[1] / "architect.py"
FID = "33333333-3333-4333-8333-333333333333"

BLUEPRINT = """# Architecture Constraint (binding)
- Client-only monolith: no server, no accounts, no runtime network calls.
## UI Layout
- Minimalist single-list view; competing layout paradigms are structural conflicts.
## Existing Core Features
- One tap marks today's check-in; tapping again undoes it.
"""


def pitch(**over: Any) -> dict[str, Any]:
    base = {
        "feature_id": FID, "author_id": "u",
        "title": "Reorder habits by dragging",
        "description": "Let a user drag habit cards up and down to reorder the list.",
        "submitted_at": "2026-07-28T00:00:00Z",
    }
    base.update(over)
    return base


def judge_returning(payload: Any, *, record: list | None = None):
    async def _judge(system_prompt: str, user_prompt: str) -> str:
        if record is not None:
            record.append((system_prompt, user_prompt))
        return json.dumps(payload) if isinstance(payload, dict) else payload
    return _judge


def judge_raising(exc: Exception):
    async def _judge(s: str, u: str) -> str:
        raise exc
    return _judge


GREEN = {"friction": "green", "axis": "none", "too_large": False, "children": [], "explanation": "fits"}
RED = {"friction": "red", "axis": "architecture_constraint", "too_large": False,
       "children": [], "explanation": "needs a server the blueprint forbids"}
SPLIT = {"friction": "green", "axis": "none", "too_large": True,
         "children": [{"title": "Colour per habit", "description": "Pick a colour for each habit card."},
                      {"title": "Drag to reorder", "description": "Reorder the list by dragging."}],
         "explanation": "two separable wants"}


async def _shape(reply, **kw):
    return await decide_shape(pitch(), blueprint=BLUEPRINT, judge=judge_returning(reply), **kw)


# ==========================================================================
# R2 / R30 — statuses and the echoed id
# ==========================================================================

async def test_r2_green_becomes_voting() -> None:
    s = await _shape(GREEN)
    assert s.friction is Friction.green
    assert s.status is FeatureStatus.VOTING


async def test_r2_red_becomes_postponed_conflict() -> None:
    s = await _shape(RED)
    assert s.status is FeatureStatus.POSTPONED_CONFLICT


async def test_r2_too_large_becomes_split() -> None:
    s = await _shape(SPLIT)
    assert s.status is FeatureStatus.SPLIT


async def test_r30_feature_id_is_read_from_the_envelope_key() -> None:
    """The payload carries `feature_id`; `id` is the board row's column."""
    s = await _shape(GREEN)
    assert s.feature_id == FID


async def test_r30_feature_id_is_required_by_contract() -> None:
    """The daemon validates INTAKE_KEYS before calling, so the key is guaranteed.

    Failing loudly on a broken internal contract is right — shaping a pitch we
    cannot identify would produce a row nobody can trace. The daemon's per-item
    handler catches it either way, so the loop survives.
    """
    with pytest.raises(KeyError):
        await decide_shape({"title": "t", "description": "d"},
                           blueprint=BLUEPRINT, judge=judge_returning(GREEN))


# ==========================================================================
# R4 / R5 / R26 — children
# ==========================================================================

async def test_r4_split_returns_two_or_three_children() -> None:
    s = await _shape(SPLIT)
    assert 2 <= len(s.children) <= 3
    assert all(isinstance(c, ChildSpec) and c.title and c.description for c in s.children)


@pytest.mark.parametrize("reply", [GREEN, RED])
async def test_r5_no_children_on_non_split_outcomes(reply: dict) -> None:
    """A stray child on a green-lit feature becomes a phantom board row."""
    s = await _shape(reply)
    assert s.children == ()


async def test_r5_children_are_dropped_if_sent_without_too_large() -> None:
    rogue = {**GREEN, "children": [{"title": "X", "description": "y"}]}
    s = await _shape(rogue)
    assert s.status is FeatureStatus.VOTING
    assert s.children == ()


# ==========================================================================
# R9 — fails open, like the PM agent
# ==========================================================================

@pytest.mark.parametrize(
    "bad", ["not json", "", "{}", '{"friction": "purple", "axis": "none", "too_large": false, "children": [], "explanation": "e"}'],
)
async def test_r9_malformed_reply_falls_back_to_voting(bad: str) -> None:
    s = await decide_shape(pitch(), blueprint=BLUEPRINT, judge=judge_returning(bad))
    assert s.status is FeatureStatus.VOTING, "an outage must not look like a design objection"


@pytest.mark.parametrize("exc", [ConnectionError("x"), asyncio.TimeoutError(), RuntimeError("x")])
async def test_r9_transport_failure_falls_back_to_voting(exc: Exception) -> None:
    s = await decide_shape(pitch(), blueprint=BLUEPRINT, judge=judge_raising(exc))
    assert s.status is FeatureStatus.VOTING


async def test_r9_never_postpones_on_failure() -> None:
    """Postponing on failure would withhold a legitimate feature silently."""
    for j in (judge_returning("garbage"), judge_raising(RuntimeError())):
        s = await decide_shape(pitch(), blueprint=BLUEPRINT, judge=j)
        assert s.status is not FeatureStatus.POSTPONED_CONFLICT


# ==========================================================================
# R10 — reasoning-model replies
# ==========================================================================

async def test_r10_think_block_and_fences_are_stripped() -> None:
    fence = chr(96) * 3
    wrapped = "<think>\nweighing it up\n</think>\n\n" + fence + "json\n" + json.dumps(RED) + "\n" + fence
    s = await decide_shape(pitch(), blueprint=BLUEPRINT, judge=judge_returning(wrapped))
    assert s.status is FeatureStatus.POSTPONED_CONFLICT, "a correct answer was discarded as malformed"


# ==========================================================================
# R15 / R27 / R28 / R29 — what the model is asked
# ==========================================================================

async def test_r15_blueprint_and_pitch_travel_as_data() -> None:
    calls: list = []
    await decide_shape(pitch(), blueprint=BLUEPRINT, judge=judge_returning(GREEN, record=calls))
    system_prompt, user_prompt = calls[0]
    assert BLUEPRINT.strip()[:40] in user_prompt, "the blueprint belongs in the user turn"
    assert BLUEPRINT.strip()[:40] not in system_prompt, \
        "blueprint prose is imperative — in the system turn a model would follow it"


async def test_r27_two_questions_are_posed_separately() -> None:
    """One verdict plus fields makes the model reason only about buildability."""
    calls: list = []
    await decide_shape(pitch(), blueprint=BLUEPRINT, judge=judge_returning(GREEN, record=calls))
    sys_l = calls[0][0].lower()
    assert "question 1" in sys_l or "two separate questions" in sys_l
    assert "question 2" in sys_l or "one thing" in sys_l


async def test_r28_the_split_question_asks_for_an_enumeration() -> None:
    calls: list = []
    await decide_shape(pitch(), blueprint=BLUEPRINT, judge=judge_returning(GREEN, record=calls))
    sys_l = calls[0][0].lower()
    assert "enumerate" in sys_l or "list the" in sys_l or "count" in sys_l


async def test_r29_green_is_stated_not_to_imply_single() -> None:
    calls: list = []
    await decide_shape(pitch(), blueprint=BLUEPRINT, judge=judge_returning(GREEN, record=calls))
    sys_l = calls[0][0].lower()
    assert "independent of friction" in sys_l or "green" in sys_l.split("question 2")[-1]


# ==========================================================================
# R12 / R13 / R14 / R16 — pins, caching, and failure to load
# ==========================================================================

def test_r12_uses_the_architect_model_pin_only() -> None:
    src = MODULE_SRC.read_text()
    assert "LLM_MODEL_ARCHITECT" in src
    assert "LLM_MODEL_SCREENING" not in src and "LLM_MODEL_PM" not in src


def test_r16_no_vendor_or_absolute_path_is_hard_coded() -> None:
    src = MODULE_SRC.read_text().lower()
    for bad in ("minimax", "openai.com", "gpt-4", "/users/", "/home/"):
        assert bad not in src, f"hard-coded {bad}"


def test_r13_blueprint_is_cached(tmp_path: pathlib.Path) -> None:
    """Read once, not per pitch — the file only changes when a sprint compiles."""
    from orchestrator import architect as A

    f = tmp_path / "bp.prompt"
    f.write_text("BLUEPRINT ONE")
    A._cached_blueprint_text.cache_clear()
    assert load_blueprint(f) == "BLUEPRINT ONE"
    f.write_text("CHANGED ON DISK")
    assert load_blueprint(f) == "BLUEPRINT ONE", "second call re-read the file"
    A._cached_blueprint_text.cache_clear()


def test_r14_missing_blueprint_raises_clearly(tmp_path: pathlib.Path) -> None:
    """Judging against nothing would green-light every feature."""
    from orchestrator import architect as A

    A._cached_blueprint_text.cache_clear()
    with pytest.raises(Exception):
        load_blueprint(tmp_path / "does_not_exist.prompt")
    A._cached_blueprint_text.cache_clear()


def test_r14_empty_blueprint_is_rejected(tmp_path: pathlib.Path) -> None:
    from orchestrator import architect as A

    f = tmp_path / "empty.prompt"
    f.write_text("   \n")
    A._cached_blueprint_text.cache_clear()
    with pytest.raises(Exception):
        load_blueprint(f)
    A._cached_blueprint_text.cache_clear()


def test_module_does_no_db_or_queue_io() -> None:
    import ast

    tree = ast.parse(MODULE_SRC.read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    for banned in ("redis", "supabase", "postgrest"):
        assert banned not in roots


async def test_does_not_mutate_the_pitch() -> None:
    p = pitch()
    before = dict(p)
    await decide_shape(p, blueprint=BLUEPRINT, judge=judge_returning(GREEN))
    assert p == before
