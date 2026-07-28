"""Contract tests for `orchestrator/screener.py` (US-02, step 1)."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from orchestrator.screener import Verdict, screen_pitch
from shared.constants import RejectionReason

FID = "33333333-3333-4333-8333-333333333333"


def pitch(**over: Any) -> dict[str, Any]:
    base = {
        "feature_id": FID,
        "author_id": "11111111-1111-4111-8111-111111111111",
        "title": "Dark mode for the dashboard",
        "description": "Add a persisted dark theme toggle in the header that survives a reload.",
        "submitted_at": "2026-07-27T00:00:00Z",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# R4 — step 1 passes anything structurally valid
# --------------------------------------------------------------------------

def test_r4_valid_pitch_passes() -> None:
    v = screen_pitch(pitch())
    assert v.passed is True
    assert v.reason is None


def test_r4_boundary_lengths_pass() -> None:
    """Exactly at the openapi bounds is valid, not rejected."""
    assert screen_pitch(pitch(title="x", description="y" * 30)).passed
    assert screen_pitch(pitch(title="x" * 60, description="y" * 300)).passed


@pytest.mark.parametrize(
    "over",
    [
        {"title": ""},
        {"title": "x" * 61},
        {"description": "y" * 29},
        {"description": "y" * 301},
    ],
)
def test_r4_out_of_bounds_is_rejected_as_unclear(over: dict) -> None:
    v = screen_pitch(pitch(**over))
    assert v.passed is False
    assert v.reason is RejectionReason.UNCLEAR


# --------------------------------------------------------------------------
# R1 — never raises, even on garbage
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"feature_id": FID},
        {"feature_id": FID, "title": None, "description": "y" * 40},
        {"feature_id": FID, "title": "ok", "description": 12345},
        {"feature_id": FID, "title": ["not", "a", "string"], "description": "y" * 40},
    ],
)
def test_r1_malformed_input_returns_a_verdict_not_an_exception(bad: dict) -> None:
    v = screen_pitch(bad)
    assert isinstance(v, Verdict)
    assert v.passed is False
    assert v.reason is RejectionReason.UNCLEAR


def test_r1_missing_feature_id_still_returns_a_verdict() -> None:
    """The daemon must always get something back to log."""
    v = screen_pitch({"title": "ok title", "description": "y" * 40})
    assert isinstance(v, Verdict)


# --------------------------------------------------------------------------
# R2 / R3 — reason vocabulary
# --------------------------------------------------------------------------

def test_r2_reason_is_the_enum_never_a_bare_string() -> None:
    v = screen_pitch(pitch(title=""))
    assert isinstance(v.reason, RejectionReason)
    assert v.reason not in {"unsafe", "incoherent"}  # invented names must not appear


def test_r3_never_returns_a_dedup_outcome() -> None:
    """already_shipped and merged belong to the PM agent, not screening."""
    forbidden = {RejectionReason.ALREADY_SHIPPED, RejectionReason.MERGED}
    for over in [{}, {"title": ""}, {"description": "short"}, {"title": None}]:
        assert screen_pitch(pitch(**over)).reason not in forbidden


# --------------------------------------------------------------------------
# R5 — feature_id echoed back
# --------------------------------------------------------------------------

def test_r5_feature_id_is_echoed_on_pass_and_fail() -> None:
    assert screen_pitch(pitch()).feature_id == FID
    assert screen_pitch(pitch(title="")).feature_id == FID


# --------------------------------------------------------------------------
# R6 — detail carries no pitch content
# --------------------------------------------------------------------------

def test_r6_detail_never_contains_the_pitch_text() -> None:
    """Unscreened content must not travel into operator logs."""
    secret_title = "ZZQQ-secret-title-marker"
    secret_desc = "WWXX-secret-description-marker that is long enough to pass bounds"
    for p in (pitch(title=secret_title, description=secret_desc),
              pitch(title="", description=secret_desc),
              pitch(title=secret_title, description="short")):
        v = screen_pitch(p)
        assert secret_title not in v.detail
        assert secret_desc not in v.detail


def test_r6_detail_is_short_and_non_empty() -> None:
    v = screen_pitch(pitch(title=""))
    assert v.detail and len(v.detail) < 200


# --------------------------------------------------------------------------
# R7 / R8 — purity and sync-ness
# --------------------------------------------------------------------------

def test_r8_screen_pitch_is_synchronous() -> None:
    """No I/O means no coroutine; an async def would be a false promise."""
    assert not inspect.iscoroutinefunction(screen_pitch)


def test_r7_is_deterministic() -> None:
    p = pitch()
    first = screen_pitch(p)
    for _ in range(5):
        assert screen_pitch(p) == first


def test_r7_does_not_mutate_its_input() -> None:
    p = pitch()
    before = dict(p)
    screen_pitch(p)
    assert p == before


def test_r7_module_reads_no_config_or_environment() -> None:
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "screener.py").read_text()
    for forbidden in ("os.environ", "getenv", "shared.config", "settings", "open("):
        assert forbidden not in src, f"screener must be pure: found {forbidden}"


def test_verdict_is_frozen() -> None:
    v = screen_pitch(pitch())
    with pytest.raises(Exception):
        v.passed = False  # type: ignore[misc]
