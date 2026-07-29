"""orchestrator/decisions.py

Write one durable, queryable record of every automated decision, without
ever becoming a reason the pipeline stops.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Final, Mapping

from supabase._async.client import AsyncClient

from shared.constants import DecisionPhase, TABLE_DECISION_LOG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# R4: model_version for deterministic (non-model) decisions
# ---------------------------------------------------------------------------

# Lowercase, matching schema.sql's "deterministic steps record 'programmatic'"
# and the row compiler.py already writes. Two spellings would split the very
# dataset R4 exists to keep coherent.
PROGRAMMATIC: Final[str] = "programmatic"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _serialise_value(value: Any) -> Any:
    """Recursively convert non-JSON-primitive types so the Supabase driver
    never chokes on an enum member, a dataclass, or a set.

    R8: enums → their `.value`; mappings/lists are walked; everything else
    passes through unchanged (the driver handles str/int/float/bool/None).
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _serialise_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialise_value(item) for item in value]
    if isinstance(value, set):
        return [_serialise_value(item) for item in sorted(value, key=str)]
    if is_dataclass(value) and not isinstance(value, type):
        # architect returns Shape and BuildVerdict; sprint_service logs them
        return {k: _serialise_value(v) for k, v in asdict(value).items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Last resort: a governance record with a stringified value is worth more
    # than a lost one (R2 — this must never be why a decision goes unfiled).
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return "<unserialisable>"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def record_decision(
    supabase: AsyncClient,
    *,
    phase: DecisionPhase,
    agent: str,
    decision: Mapping[str, Any],
    model_version: str,
    feature_id: str | None = None,
    batch_id: str | None = None,
) -> bool:
    """Insert exactly one ``decision_log`` row.

    Returns ``True`` when the row was written; ``False`` when it could not
    be, **never raising** (R2).

    Parameters
    ----------
    supabase:
        Injected async Supabase client (R9).
    phase:
        Must be a :class:`DecisionPhase` member (R3).
    agent:
        Human-readable component name (``screener``, ``pm_agent``, …).
    decision:
        The verdict payload — what was decided *and why* (R6).
    model_version:
        The model id that produced the decision, or :data:`PROGRAMMATIC`
        for deterministic logic (R4).
    feature_id:
        Nullable — omitted for batch/sprint-level decisions (R7).
    batch_id:
        Nullable — present when the decision belongs to a sprint batch.
    """

    # R3: reject non-enum values before touching the database
    if not isinstance(phase, DecisionPhase):
        logger.warning(
            "record_decision called with invalid phase %r (type %s); "
            "row not written",
            phase,
            type(phase).__name__,
        )
        return False

    # R8: deep-serialise the payload so enums/sets/etc. become JSON-safe
    serialised_decision: Any = _serialise_value(decision)

    # R1: only columns that exist on decision_log
    row: dict[str, Any] = {
        "phase": phase.value,
        "agent": agent,
        "decision": serialised_decision,
        "model_version": model_version,
    }

    # R7: nullable columns — include only when provided
    if feature_id is not None:
        row["feature_id"] = feature_id
    if batch_id is not None:
        row["batch_id"] = batch_id

    try:
        await (
            supabase.table(TABLE_DECISION_LOG)
            .insert(row)
            .execute()
        )
    except Exception:  # noqa: BLE001 — R2: never raise into the caller
        logger.warning(
            "Failed to write decision_log row (phase=%s, agent=%s, "
            "feature_id=%s)",
            phase.value,
            agent,
            feature_id,
            exc_info=True,
        )
        return False

    return True