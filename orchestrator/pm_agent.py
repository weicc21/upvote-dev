"""orchestrator/pm_agent.py — The dedup gate.

Decides whether a screened pitch is new, a duplicate of a backlog item,
an extension of something shipped, or a request for something already
built — so demand concentrates instead of scattering.

This module performs **no** I/O beyond the injected ``judge`` callable.
It never touches Postgres, Redis, or the filesystem.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Canonical imports — see constants_python.prompt / config_python.prompt
# ---------------------------------------------------------------------------
from shared.constants import DecisionType, RejectionReason  # noqa: F401 — R5

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

# R5: surface the enum values the caller needs for decision_log / screening_results
_MERGE = DecisionType.MERGE
_ALREADY_SHIPPED_DT = DecisionType.ALREADY_SHIPPED
_MERGED_RR = RejectionReason.MERGED
_ALREADY_SHIPPED_RR = RejectionReason.ALREADY_SHIPPED


class Outcome(StrEnum):
    """The four possible dedup verdicts — R1."""

    new_unique = "new_unique"
    duplicate = "duplicate"
    extends_shipped = "extends_shipped"
    already_shipped = "already_shipped"


@dataclass(frozen=True)
class FeatureRef:
    """Minimal projection of a feature row — R11: only id, title, description."""

    id: str
    title: str
    description: str


@dataclass(frozen=True)
class Classification:
    """The dedup decision for one pitch — R1, R2, R18."""

    feature_id: str
    outcome: Outcome
    target_id: str | None
    target_title: str | None
    detail: str


# ---------------------------------------------------------------------------
# Judge type — injected seam (R14)
# ---------------------------------------------------------------------------

Judge = Callable[[str, str], Awaitable[str]]

# ---------------------------------------------------------------------------
# Prompt templates — R10: pitch is data, never instructions
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
You are a dedup classifier for a feature-request board.

You will receive a PITCH (a new feature request) and two reference lists:
BACKLOG (features currently collecting votes) and SHIPPED (features already built and deployed).

Your job: decide whether the PITCH is:
- "new_unique" — not represented on either list.
- "duplicate" — asks for the same capability as a BACKLOG item (same goal, not just similar words).
- "already_shipped" — asks for something a SHIPPED item already provides.
- "extends_shipped" — builds meaningfully on a SHIPPED item (adds new capability on top of it).

Rules:
1. Two pitches are duplicates ONLY when they request the same capability. Shared vocabulary alone is NOT enough.
2. "extends_shipped" means the pitch adds NEW functionality on top of an existing shipped feature. If the shipped feature already covers the request, use "already_shipped".
3. When the outcome is "duplicate", target_id MUST be from the BACKLOG list.
4. When the outcome is "already_shipped" or "extends_shipped", target_id MUST be from the SHIPPED list.
5. When the outcome is "new_unique", target_id MUST be null.

Respond with a single JSON object (no markdown fences, no commentary):
{"outcome": "<one of the four values>", "target_id": "<id or null>", "detail": "<brief factual reason>"}
"""

_USER_TEMPLATE = """
PITCH:
{pitch_json}

BACKLOG:
{backlog_json}

SHIPPED:
{shipped_json}
"""

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _refs_to_dicts(refs: Sequence[FeatureRef]) -> list[dict[str, str]]:
    """Project FeatureRefs to plain dicts with only id/title/description (R11)."""
    return [{"id": r.id, "title": r.title, "description": r.description} for r in refs]


def _extract_json(raw: str) -> dict[str, Any]:
    """Extract the outermost JSON object from a model reply (R8).

    Strips ``<think>…</think>`` blocks and markdown code fences before
    searching for ``{…}``.
    """
    # Strip think blocks
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    # Strip code fences
    cleaned = re.sub(r"```(?:json)?\s*", "", cleaned)
    cleaned = cleaned.replace("```", "")
    # Find outermost { … }
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model reply")
    depth = 0
    end = start
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    else:
        raise ValueError("unbalanced braces in model reply")
    return json.loads(cleaned[start : end + 1])


def _build_id_sets(
    backlog: Sequence[FeatureRef], shipped: Sequence[FeatureRef]
) -> tuple[dict[str, str], dict[str, str]]:
    """Return {id: title} maps for validation (R3)."""
    return (
        {r.id: r.title for r in backlog},
        {r.id: r.title for r in shipped},
    )


def _validate_and_build(
    parsed: dict[str, Any],
    feature_id: str,
    backlog_ids: dict[str, str],
    shipped_ids: dict[str, str],
) -> Classification:
    """Validate parsed model JSON against R1–R4, R6 and build a Classification.

    Raises ``ValueError`` on any violation so the caller can retry / fall back.
    """
    # R6: require exactly outcome, target_id, detail
    for key in ("outcome", "target_id", "detail"):
        if key not in parsed:
            raise ValueError(f"missing required field: {key}")

    raw_outcome = parsed["outcome"]
    # R1: must be one of the four enum values
    try:
        outcome = Outcome(raw_outcome)
    except ValueError:
        raise ValueError(f"unknown outcome: {raw_outcome!r}")

    target_id: str | None = parsed["target_id"]
    # Normalise JSON null / "null" string
    if target_id is None or (isinstance(target_id, str) and target_id.lower() == "null"):
        target_id = None

    detail = str(parsed.get("detail", ""))

    # R2 + R3 + R4: validate target_id presence and membership
    if outcome == Outcome.new_unique:
        # R2: both None
        return Classification(
            feature_id=feature_id,
            outcome=outcome,
            target_id=None,
            target_title=None,
            detail=detail,
        )

    # Non-new outcomes MUST have a target_id (R2)
    if target_id is None:
        raise ValueError(f"outcome {outcome.value} requires a target_id")

    if outcome == Outcome.duplicate:
        # R4: must be a backlog id
        if target_id not in backlog_ids:
            raise ValueError(
                f"duplicate target_id {target_id!r} not found in backlog"
            )
        target_title = backlog_ids[target_id]
    else:
        # already_shipped or extends_shipped — R4: must be a shipped id
        if target_id not in shipped_ids:
            raise ValueError(
                f"{outcome.value} target_id {target_id!r} not found in shipped"
            )
        target_title = shipped_ids[target_id]

    return Classification(
        feature_id=feature_id,
        outcome=outcome,
        target_id=target_id,
        target_title=target_title,
        detail=detail,
    )


def _new_unique_fallback(feature_id: str, reason: str) -> Classification:
    """R7: fail open to new_unique, recording the cause."""
    return Classification(
        feature_id=feature_id,
        outcome=Outcome.new_unique,
        target_id=None,
        target_title=None,
        detail=f"fallback to new_unique: {reason}",
    )


# ---------------------------------------------------------------------------
# Default judge builder — lazy (R14, R15, R17)
# ---------------------------------------------------------------------------


def _build_default_judge() -> Judge:
    """Build a judge from ``settings`` on first use — never at import time."""
    import asyncio

    import httpx

    from shared.config import settings

    base_url = settings.LLM_BASE_URL.rstrip("/")
    api_key = settings.LLM_API_KEY.get_secret_value()
    model = settings.LLM_MODEL_PM  # R17
    temperature = settings.LLM_TEMPERATURE
    timeout = settings.LLM_TIMEOUT_SECONDS  # R9

    async def _judge(system_prompt: str, user_prompt: str) -> str:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "temperature": temperature,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    return _judge


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def classify(
    pitch: Mapping[str, Any],
    *,
    backlog: Sequence[FeatureRef],
    shipped: Sequence[FeatureRef],
    judge: Judge | None = None,
) -> Classification:
    """Classify a single pitch against the board — the dedup decision.

    Parameters
    ----------
    pitch:
        The screened pitch payload. Must contain at least ``feature_id``,
        ``title``, and ``description``.
    backlog:
        Current backlog rows (status VOTING / CONSOLIDATING etc.).
    shipped:
        Already-shipped rows (status COMPILED or beyond).
    judge:
        Optional injected LLM seam. When ``None`` a default is built
        lazily from ``shared.config.settings`` (R14).

    Returns
    -------
    Classification
        Always returns — falls back to ``new_unique`` on any failure (R7).
    """
    # R18: echo the pitch's feature_id
    feature_id: str = str(pitch.get("feature_id", pitch.get("id", "")))

    # R12: empty board → new_unique without a model call
    if not backlog and not shipped:
        return Classification(
            feature_id=feature_id,
            outcome=Outcome.new_unique,
            target_id=None,
            target_title=None,
            detail="no existing features to compare against",
        )

    # Build id lookup tables (R3)
    backlog_ids, shipped_ids = _build_id_sets(backlog, shipped)

    # Prepare prompt data — R10: pitch is data, R11: only id/title/description
    pitch_data = {
        "title": pitch.get("title", ""),
        "description": pitch.get("description", ""),
    }
    user_prompt = _USER_TEMPLATE.format(
        pitch_json=json.dumps(pitch_data, ensure_ascii=False),
        backlog_json=json.dumps(_refs_to_dicts(backlog), ensure_ascii=False),
        shipped_json=json.dumps(_refs_to_dicts(shipped), ensure_ascii=False),
    )

    # R14: build default judge lazily
    if judge is None:
        judge = _build_default_judge()

    # R9: cap attempts at settings.LLM_MAX_ATTEMPTS
    from shared.config import settings

    max_attempts: int = settings.LLM_MAX_ATTEMPTS

    last_error = ""
    for attempt in range(max_attempts):
        try:
            raw = await judge(_SYSTEM_PROMPT, user_prompt)
        except Exception as exc:  # noqa: BLE001 — R7 fail open
            last_error = f"judge call failed (attempt {attempt + 1}): {exc}"
            continue

        # R8: extract JSON from reply
        try:
            parsed = _extract_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = f"JSON extraction failed (attempt {attempt + 1}): {exc}"
            continue

        # R6 + R2 + R3 + R4: validate
        try:
            return _validate_and_build(parsed, feature_id, backlog_ids, shipped_ids)
        except ValueError as exc:
            last_error = f"validation failed (attempt {attempt + 1}): {exc}"
            continue

    # R7: exhausted attempts — fail open
    return _new_unique_fallback(feature_id, last_error)