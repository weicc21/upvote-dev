"""Dedup gate — classifies a screened pitch against the existing board.

This module decides whether a pitch is new, a duplicate of a backlog item,
an extension of something shipped, or a request for something already built.
It performs no I/O beyond the injected ``judge`` callable.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Public vocabulary
# ---------------------------------------------------------------------------


class Outcome(StrEnum):
    """The four possible dedup outcomes — nothing else is valid."""

    new_unique = "new_unique"
    duplicate = "duplicate"
    extends_shipped = "extends_shipped"
    already_shipped = "already_shipped"


@dataclass(frozen=True)
class FeatureRef:
    """Minimal projection of a feature row — only what the model needs (R11)."""

    id: str
    title: str
    description: str


@dataclass(frozen=True)
class Classification:
    """The dedup decision for one pitch."""

    feature_id: str
    outcome: Outcome
    target_id: str | None
    target_title: str | None
    detail: str


# ---------------------------------------------------------------------------
# Judge type
# ---------------------------------------------------------------------------

Judge = Callable[[str, str], Awaitable[str]]

# ---------------------------------------------------------------------------
# System prompt — data-only framing (R10, R16)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
You are a dedup classifier for a feature-request board.

You will receive a PITCH (a new feature request) and two reference lists:
BACKLOG (features currently collecting votes) and SHIPPED (features already built and deployed).

Your job: decide whether the PITCH is:
- "new_unique" — not represented on the board at all.
- "duplicate" — asks for the same capability as a BACKLOG item (same goal, not just similar words).
- "already_shipped" — asks for a capability that a SHIPPED item already provides.
- "extends_shipped" — builds on or extends a SHIPPED item with additional capability.

IMPORTANT:
- Two pitches are duplicates ONLY when they request the same capability, not merely when they share vocabulary.
- "extends_shipped" means the pitch adds NEW functionality on top of an existing shipped feature.
- "already_shipped" means the pitch is fully covered by an existing shipped feature.

Respond with a single JSON object (no markdown fences, no commentary):
{"outcome": "<one of the four values>", "target_id": "<id of the matching row or null>", "detail": "<one short sentence explaining your reasoning>"}

When outcome is "new_unique", target_id MUST be null.
When outcome is "duplicate", target_id MUST be the id of the matching BACKLOG item.
When outcome is "already_shipped" or "extends_shipped", target_id MUST be the id of the matching SHIPPED item.
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_user_prompt(
    pitch: Mapping[str, Any],
    backlog: Sequence[FeatureRef],
    shipped: Sequence[FeatureRef],
) -> str:
    """Compose the user message with pitch and candidate data (R10, R11)."""

    def _refs_block(refs: Sequence[FeatureRef]) -> str:
        if not refs:
            return "  (none)"
        lines: list[str] = []
        for r in refs:
            lines.append(
                json.dumps(
                    {"id": r.id, "title": r.title, "description": r.description},
                    ensure_ascii=False,
                )
            )
        return "\n".join(f"  {l}" for l in lines)

    pitch_data = json.dumps(
        {
            "title": pitch.get("title", ""),
            "description": pitch.get("description", ""),
        },
        ensure_ascii=False,
    )

    return (
        f"PITCH:\n  {pitch_data}\n\n"
        f"BACKLOG:\n{_refs_block(backlog)}\n\n"
        f"SHIPPED:\n{_refs_block(shipped)}"
    )


# Regex helpers for R8 — strip think blocks and code fences, then grab outermost JSON object.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _extract_json(raw: str) -> dict[str, Any]:
    """Extract the outermost JSON object from a model reply (R8).

    Strips ``<think>`` blocks and code fences first, then finds the first
    ``{…}`` pair.  Raises ``ValueError`` on failure.
    """
    # Strip think blocks
    text = _THINK_RE.sub("", raw)

    # Try inside code fences first
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1)

    # Find outermost { … }
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in reply")

    depth = 0
    end = start
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    else:
        raise ValueError("unbalanced braces in reply")

    return json.loads(text[start : end + 1])  # type: ignore[no-any-return]


def _validate_parsed(
    parsed: dict[str, Any],
    backlog_ids: set[str],
    shipped_ids: set[str],
    backlog_title_map: dict[str, str],
    shipped_title_map: dict[str, str],
) -> Classification | None:
    """Validate a parsed JSON dict against R1–R4, R6.  Returns None on failure."""

    # R6: require exactly outcome, target_id, detail
    if "outcome" not in parsed or "detail" not in parsed:
        return None

    raw_outcome = parsed["outcome"]
    if raw_outcome not in {o.value for o in Outcome}:
        return None

    outcome = Outcome(raw_outcome)
    target_id: str | None = parsed.get("target_id")
    detail: str = str(parsed.get("detail", ""))

    # Normalise null-ish target_id
    if target_id is None or (isinstance(target_id, str) and target_id.lower() in ("null", "none", "")):
        target_id = None

    # R2 + R4: validate target presence and pool membership
    if outcome == Outcome.new_unique:
        # Must have no target
        if target_id is not None:
            return None
        return Classification(
            feature_id="",  # caller fills this
            outcome=outcome,
            target_id=None,
            target_title=None,
            detail=detail[:200],
        )

    # Non-new outcomes require a target (R2)
    if target_id is None:
        return None

    if outcome == Outcome.duplicate:
        # R4: must target a backlog row
        if target_id not in backlog_ids:  # R3
            return None
        return Classification(
            feature_id="",
            outcome=outcome,
            target_id=target_id,
            target_title=backlog_title_map.get(target_id),
            detail=detail[:200],
        )

    # already_shipped or extends_shipped — must target a shipped row (R4)
    if target_id not in shipped_ids:  # R3
        return None

    return Classification(
        feature_id="",
        outcome=outcome,
        target_id=target_id,
        target_title=shipped_title_map.get(target_id),
        detail=detail[:200],
    )


# ---------------------------------------------------------------------------
# Default judge builder (R14, R15)
# ---------------------------------------------------------------------------


def _build_default_judge() -> Judge:
    """Lazily construct a judge from ``settings`` (R14, R15)."""
    from shared.config import settings

    import httpx

    base_url = settings.LLM_BASE_URL.rstrip("/")
    api_key = settings.LLM_API_KEY.get_secret_value()
    model = settings.LLM_MODEL_SCREENING
    temperature = settings.LLM_TEMPERATURE
    timeout = settings.LLM_TIMEOUT_SECONDS

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
            return data["choices"][0]["message"]["content"]  # type: ignore[no-any-return]

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
    """Classify a single screened pitch against the board.

    Returns a :class:`Classification` for every call — falls back to
    ``new_unique`` when no decision can be obtained (R7).
    """
    # Derive feature_id from pitch — caller may have set it
    feature_id: str = str(pitch.get("id", pitch.get("feature_id", "")))

    def _new_unique(detail: str) -> Classification:
        return Classification(
            feature_id=feature_id,
            outcome=Outcome.new_unique,
            target_id=None,
            target_title=None,
            detail=detail[:200],
        )

    # R12: empty board → new_unique without a model call
    if not backlog and not shipped:
        return _new_unique("no existing features to compare against")

    # Build lookup structures (R3, R4)
    backlog_ids: set[str] = {r.id for r in backlog}
    shipped_ids: set[str] = {r.id for r in shipped}
    backlog_title_map: dict[str, str] = {r.id: r.title for r in backlog}
    shipped_title_map: dict[str, str] = {r.id: r.title for r in shipped}

    # Resolve judge (R14)
    if judge is None:
        judge = _build_default_judge()

    # Build prompts (R10, R11)
    user_prompt = _build_user_prompt(pitch, backlog, shipped)

    # Read attempt/timeout bounds from settings (R9)
    from shared.config import settings

    max_attempts: int = settings.LLM_MAX_ATTEMPTS
    timeout_seconds: int = settings.LLM_TIMEOUT_SECONDS

    last_error: str = "unknown"

    for attempt in range(1, max_attempts + 1):
        try:
            raw = await asyncio.wait_for(
                judge(_SYSTEM_PROMPT, user_prompt),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            last_error = f"attempt {attempt}: timeout after {timeout_seconds}s"
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = f"attempt {attempt}: {type(exc).__name__}: {exc}"
            continue

        # R8: extract JSON
        try:
            parsed = _extract_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = f"attempt {attempt}: JSON extraction failed: {exc}"
            continue

        # R6: validate structure and values
        result = _validate_parsed(
            parsed,
            backlog_ids,
            shipped_ids,
            backlog_title_map,
            shipped_title_map,
        )
        if result is None:
            last_error = f"attempt {attempt}: validation failed for parsed response"
            continue

        # Success — fill in feature_id and return
        return Classification(
            feature_id=feature_id,
            outcome=result.outcome,
            target_id=result.target_id,
            target_title=result.target_title,
            detail=result.detail,
        )

    # R7: all attempts exhausted — fail open
    return _new_unique(f"fallback to new_unique: {last_error}")