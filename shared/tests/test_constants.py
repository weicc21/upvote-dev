"""Contract tests for `shared/constants.py`.

Each test names the rule from `prompts/shared/constants_python.prompt` it enforces.
The rules that matter most here are cross-artifact: R1 and R2 assert the Python
constants still agree with `schema.sql`, which is the drift these constants exist
to prevent. R8/R9 are regression tests for a real defect — the module previously
used a `(str, Enum)` mixin, under which `f"{FeatureStatus.VOTING}"` renders
`"FeatureStatus.VOTING"` instead of `"VOTING"`.
"""

from __future__ import annotations

import ast
import re
import sys
from enum import StrEnum
from pathlib import Path

import pytest

from shared import constants as C

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = PROJECT_ROOT / "schema.sql"
MODULE_SRC = PROJECT_ROOT / "shared" / "constants.py"

ALL_ENUMS = [
    C.FeatureStatus,
    C.BroadcastPhase,
    C.DecisionPhase,
    C.BuildStatus,
    C.DecisionType,
    C.RejectionReason,
]


# --------------------------------------------------------------------------
# schema.sql parsing — the ground truth R1 and R2 are checked against
# --------------------------------------------------------------------------

def _schema_text() -> str:
    return SCHEMA.read_text()


def _schema_enum_labels(type_name: str) -> list[str]:
    """Labels declared for a Postgres enum type, in declaration order."""
    m = re.search(
        rf"create\s+type\s+{type_name}\s+as\s+enum\s*\((.*?)\)\s*;",
        _schema_text(),
        re.S | re.I,
    )
    assert m, f"enum type {type_name!r} not found in schema.sql"
    return re.findall(r"'([^']+)'", m.group(1))


def _schema_tables() -> set[str]:
    return set(
        re.findall(
            r"create\s+table\s+if\s+not\s+exists\s+public\.(\w+)",
            _schema_text(),
            re.I,
        )
    )


# --------------------------------------------------------------------------
# R8 / R9 — StrEnum semantics (regression: the (str, Enum) mixin bug)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("enum_cls", ALL_ENUMS, ids=lambda e: e.__name__)
def test_r8_derives_from_strenum(enum_cls: type) -> None:
    """R8: every string enum is a StrEnum, not a (str, Enum) mixin."""
    assert issubclass(enum_cls, StrEnum), (
        f"{enum_cls.__name__} must derive from enum.StrEnum"
    )


@pytest.mark.parametrize("enum_cls", ALL_ENUMS, ids=lambda e: e.__name__)
def test_r9_str_renders_as_value(enum_cls: type) -> None:
    """R9: str() and f-string give the wire value, never ClassName.MEMBER.

    This is the defect that shipped `FeatureStatus.VOTING` into Redis keys and
    query filters where Postgres expected `VOTING`.
    """
    for member in enum_cls:
        assert str(member) == member.value
        assert f"{member}" == member.value
        assert "{}".format(member) == member.value  # noqa: UP032 - explicit on purpose
        assert f"{member}" != f"{enum_cls.__name__}.{member.name}"


@pytest.mark.parametrize("enum_cls", ALL_ENUMS, ids=lambda e: e.__name__)
def test_r9_member_substitutable_for_wire_string(enum_cls: type) -> None:
    """R9: a member works anywhere its wire string does, with no .value."""
    for member in enum_cls:
        assert member == member.value
        assert member in {member.value}
        assert f"prefix:{member}:suffix" == f"prefix:{member.value}:suffix"


# --------------------------------------------------------------------------
# R1 — enum values match schema.sql byte-for-byte
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "enum_cls,pg_type",
    [
        (C.FeatureStatus, "feature_status"),
        (C.BroadcastPhase, "broadcast_phase"),
        (C.DecisionPhase, "decision_phase"),
        (C.BuildStatus, "build_status"),
    ],
    ids=["feature_status", "broadcast_phase", "decision_phase", "build_status"],
)
def test_r1_enum_matches_schema(enum_cls: type, pg_type: str) -> None:
    """R1: Python enum values reproduce the Postgres labels exactly."""
    assert [m.value for m in enum_cls] == _schema_enum_labels(pg_type)


def test_r1_feature_status_is_uppercase() -> None:
    """R1: FeatureStatus is uppercase; the other Postgres enums are lowercase."""
    assert all(m.value.isupper() for m in C.FeatureStatus)
    for enum_cls in (C.BroadcastPhase, C.DecisionPhase, C.BuildStatus):
        assert all(m.value.islower() for m in enum_cls), enum_cls.__name__


def test_r1_decision_type_is_not_a_postgres_enum() -> None:
    """DecisionType lives inside the decision JSON payload, not in the schema."""
    assert "create type decision_type" not in _schema_text().lower()


def test_r1_rejection_reason_never_reaches_postgres() -> None:
    """RejectionReason travels on Redis only — it is not a Postgres type."""
    assert "create type rejection_reason" not in _schema_text().lower()
    assert "merged" in {m.value for m in C.RejectionReason}


# --------------------------------------------------------------------------
# R2 — table names match schema.sql
# --------------------------------------------------------------------------

def test_r2_table_constants_exist_in_schema() -> None:
    """R2: every TABLE_* constant names a real table."""
    declared = _schema_tables()
    for name in [n for n in dir(C) if n.startswith("TABLE_")]:
        assert getattr(C, name) in declared, f"{name} is not a table in schema.sql"


def test_r2_no_schema_table_is_unreferenced() -> None:
    """Every table in the schema has a constant — catches a forgotten addition."""
    constants = {getattr(C, n) for n in dir(C) if n.startswith("TABLE_")}
    assert _schema_tables() - constants == set()


def test_r2_uses_full_names_not_shorthand() -> None:
    """R2 calls this out explicitly: feature_requests, not features."""
    assert C.TABLE_FEATURE_REQUESTS == "feature_requests"
    assert C.TABLE_FEATURE_VOTES == "feature_votes"


# --------------------------------------------------------------------------
# R3 — tunables are DEFAULT_-prefixed
# --------------------------------------------------------------------------

def test_r3_every_tunable_is_default_prefixed_and_sane() -> None:
    """R3/R10: numeric tunables are positive; the model id is a non-empty string."""
    names = [n for n in dir(C) if n.startswith("DEFAULT_")]
    assert names, "no DEFAULT_* tunables found"
    for n in names:
        v = getattr(C, n)
        assert not isinstance(v, bool), n
        if isinstance(v, str):
            assert v.strip(), f"{n} is an empty string"
        else:
            assert isinstance(v, (int, float)), n
            assert v > 0, n


def test_r10_llm_defaults_exist_and_are_in_range() -> None:
    assert isinstance(C.DEFAULT_LLM_MODEL_SCREENING, str)
    assert 0.0 <= C.DEFAULT_LLM_TEMPERATURE <= 2.0
    assert C.DEFAULT_LLM_TIMEOUT_SECONDS > 0
    assert C.DEFAULT_LLM_MAX_ATTEMPTS >= 1, "a zero attempt cap would skip screening entirely"


def test_r3_ttl_is_fifteen_minutes() -> None:
    """The TTL bounds how long a rejected pitch reads 'screening' (US-06)."""
    assert C.DEFAULT_PENDING_PITCH_TTL_SECONDS == 900


# --------------------------------------------------------------------------
# R7 — Redis keys are templates, not pre-formatted
# --------------------------------------------------------------------------

def test_r7_templates_keep_named_placeholders() -> None:
    assert "{author_id}" in C.REDIS_PENDING_PITCH
    assert "{feature_id}" in C.REDIS_PENDING_PITCH
    assert "{author_id}" in C.REDIS_PITCH_RATE
    assert C.REDIS_PENDING_PITCH.format(author_id="a", feature_id="f") == "pending_pitch:a:f"
    assert C.REDIS_PITCH_RATE.format(author_id="a") == "rate:pitch:a"


def test_r7_plain_channel_names_have_no_placeholders() -> None:
    for name in ("REDIS_FEATURE_INTAKE", "REDIS_AGENT_EVENTS", "REDIS_SCREENING_RESULTS"):
        assert "{" not in getattr(C, name), name


# --------------------------------------------------------------------------
# R4 / R5 / R6 — import hygiene, enforced against the source AST
# --------------------------------------------------------------------------

def _module_ast() -> ast.Module:
    return ast.parse(MODULE_SRC.read_text())


def test_r4_imports_stdlib_only() -> None:
    """R4: nothing outside the standard library."""
    roots: set[str] = set()
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= sys.stdlib_module_names, f"non-stdlib imports: {roots - sys.stdlib_module_names}"


def test_r5_no_io_at_import() -> None:
    """R5: no env reads, file opens, or other I/O at import time."""
    src = MODULE_SRC.read_text()
    for forbidden in ("os.environ", "getenv", "open(", "Path(", "requests.", "socket."):
        assert forbidden not in src, f"possible import-time I/O: {forbidden}"


def test_r6_no_mutable_module_level_state() -> None:
    """R6: no bare list, dict, or set assigned at module level."""
    offenders = []
    for node in _module_ast().body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, (ast.List, ast.Dict, ast.Set)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                offenders += [t.id for t in targets if isinstance(t, ast.Name)]
    assert not offenders, f"mutable module-level state: {offenders}"


# --------------------------------------------------------------------------
# General integrity
# --------------------------------------------------------------------------

@pytest.mark.parametrize("enum_cls", ALL_ENUMS, ids=lambda e: e.__name__)
def test_enum_values_are_unique(enum_cls: type) -> None:
    values = [m.value for m in enum_cls]
    assert len(values) == len(set(values))


def test_lookup_by_value_round_trips() -> None:
    assert C.FeatureStatus("VOTING") is C.FeatureStatus.VOTING
    assert C.BuildStatus("failed") is C.BuildStatus.FAILED
    with pytest.raises(ValueError):
        C.FeatureStatus("voting")  # casing is significant (R1)
