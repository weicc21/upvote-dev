"""Deterministic pitch screener — step 1 of US-02.

Pure verdict function: same input → same output, no I/O of any kind.
The daemon that calls :func:`screen_pitch` owns the queue, Redis keys,
and every side-effect; this module owns only the judgement.

Step 2 will swap the body of :func:`screen_pitch` for an LLM call
behind the identical signature.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from shared.constants import RejectionReason

# ---------------------------------------------------------------------------
# Frozen length bounds — lifted from openapi.yaml, not configurable (R7).
# ---------------------------------------------------------------------------

_TITLE_MIN: int = 1
_TITLE_MAX: int = 60
_DESCRIPTION_MIN: int = 30
_DESCRIPTION_MAX: int = 300


# ---------------------------------------------------------------------------
# Verdict data-class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """The complete output of :func:`screen_pitch`.

    Attributes
    ----------
    feature_id:
        Echo of the input pitch's ``feature_id`` so the daemon can
        correlate the decision with the queue item (R5).
    passed:
        ``True`` → the pitch may become a ``VOTING`` row.
        ``False`` → the pitch is dropped and never persisted.
    reason:
        ``None`` when *passed* is ``True``; a :class:`RejectionReason`
        member otherwise (R2).
    detail:
        Short, factual operator log line.  Free of the pitch's own text
        so unscreened content never travels into logs (R6).
    """

    feature_id: str
    passed: bool
    reason: RejectionReason | None
    detail: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def screen_pitch(pitch: Mapping[str, Any]) -> Verdict:
    """Decide whether *pitch* may become a public feature request.

    This is a **synchronous, pure function** (R8, R7).  It never raises
    for content reasons (R1) and never returns ``already_shipped`` or
    ``merged`` (R3).

    In step 1 the only grounds for rejection are structural: missing or
    non-string fields and text outside the frozen ``openapi.yaml``
    length bounds (R4).
    """

    # -- R5: extract feature_id early; fall back to empty string --------
    raw_fid = pitch.get("feature_id") if isinstance(pitch, Mapping) else None
    feature_id: str = raw_fid if isinstance(raw_fid, str) else ""

    # -- R1: presence & type checks -------------------------------------
    raw_title = pitch.get("title") if isinstance(pitch, Mapping) else None
    raw_desc = pitch.get("description") if isinstance(pitch, Mapping) else None

    if not isinstance(raw_title, str) or not isinstance(raw_desc, str):
        return Verdict(
            feature_id=feature_id,
            passed=False,
            reason=RejectionReason.UNCLEAR,
            detail="missing or non-string title/description",
        )

    # -- R4: length bounds from openapi.yaml ----------------------------
    title_len = len(raw_title)
    if title_len < _TITLE_MIN or title_len > _TITLE_MAX:
        return Verdict(
            feature_id=feature_id,
            passed=False,
            reason=RejectionReason.UNCLEAR,
            detail=f"title length {title_len} outside [{_TITLE_MIN}, {_TITLE_MAX}]",
        )

    desc_len = len(raw_desc)
    if desc_len < _DESCRIPTION_MIN or desc_len > _DESCRIPTION_MAX:
        return Verdict(
            feature_id=feature_id,
            passed=False,
            reason=RejectionReason.UNCLEAR,
            detail=f"description length {desc_len} outside [{_DESCRIPTION_MIN}, {_DESCRIPTION_MAX}]",
        )

    # -- R4: structurally valid → pass ----------------------------------
    return Verdict(
        feature_id=feature_id,
        passed=True,
        reason=None,
        detail="structurally valid pitch accepted",
    )