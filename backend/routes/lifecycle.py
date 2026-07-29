"""Lifecycle transitions for feature requests.

This module owns the **reboot** endpoint — the only path that moves a
feature *back* into play from the Vault.  It is a separate router that
shares the ``/api/features`` prefix, exactly as ``votes.py`` does.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from supabase._async.client import AsyncClient

from backend.deps import get_current_user_id, get_supabase, raise_error
from orchestrator.decisions import PROGRAMMATIC, record_decision
from shared.constants import (
    DecisionPhase,
    FeatureStatus,
    TABLE_FEATURE_REQUESTS,
    TABLE_FEATURE_VOTES,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/features", tags=["lifecycle"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _feature_to_response(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a ``feature_requests`` row into the ``Feature`` wire shape.

    Ensures ``children`` is always present as a list (R11) and that every
    expected nullable field has an explicit value rather than being absent.
    """
    row.setdefault("children", [])
    row.setdefault("viewer_has_voted", False)
    row.setdefault("parent_id", None)
    row.setdefault("split_depth", 0)
    row.setdefault("unlock_threshold", None)
    row.setdefault("extends_id", None)
    row.setdefault("extends_title", None)
    row.setdefault("postpone_count", 0)
    row.setdefault("ai_explanation", None)
    row.setdefault("merge_count", None)
    row.setdefault("shipped_version", None)
    row.setdefault("shipped_at", None)
    row.setdefault("author_handle", None)
    row.setdefault("updated_at", None)
    # Never expose the auth id (features route R6). `author_handle` is the only
    # identity the board shows; author_id is internal and must not travel in a
    # response just because it travelled in the row we read.
    row.pop("author_id", None)
    return row


# ---------------------------------------------------------------------------
# POST /api/features/{feature_id}/reboot
# ---------------------------------------------------------------------------


@router.post("/{feature_id}/reboot")
async def reboot_feature(
    feature_id: str,
    user_id: str = Depends(get_current_user_id),  # R2, R14
    supabase: AsyncClient = Depends(get_supabase),  # R14
) -> dict[str, Any]:
    """Move an ``ARCHIVED`` feature back to ``VOTING`` with a fresh window.

    The transition resets ``created_at`` (R8), sets ``upvotes`` to 1 (R5),
    clears old vote rows (R7), and inserts the reviver's vote (R6).  A
    conditional update guards against concurrent reboots (R9).
    """

    # ------------------------------------------------------------------
    # 1. Fetch the feature — 404 if missing (R4)
    # ------------------------------------------------------------------
    feat_resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select("*")
        .eq("id", feature_id)
        .maybe_single()
        .execute()
    )

    feature = feat_resp.data
    if feature is None:
        raise_error(404, "not_found", "Feature not found")

    # ------------------------------------------------------------------
    # 2. Must be ARCHIVED — 422 otherwise (R3)
    # ------------------------------------------------------------------
    if feature["status"] != FeatureStatus.ARCHIVED:
        raise_error(
            422,
            "not_archived",
            "Feature is not archived",
        )

    # ------------------------------------------------------------------
    # 3. Conditional update: ARCHIVED → VOTING (R1, R5, R8, R9, R10)
    #    The `.eq("status", "ARCHIVED")` guard means a concurrent reboot
    #    will match zero rows and we detect the race below.
    # ------------------------------------------------------------------
    now = datetime.now(timezone.utc).isoformat()

    update_resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .update(
            {
                "status": FeatureStatus.VOTING,
                "upvotes": 1,
                "created_at": now,
            }
        )
        .eq("id", feature_id)
        .eq("status", FeatureStatus.ARCHIVED)  # R9: conditional guard
        .execute()
    )

    if not update_resp.data:
        # Another request rebooted it between our read and write (R9).
        raise_error(
            422,
            "not_archived",
            "Feature is no longer archived",
        )

    updated_feature = update_resp.data[0]

    # ------------------------------------------------------------------
    # 4. Delete all prior vote rows for this feature (R7)
    # ------------------------------------------------------------------
    try:
        await (
            supabase.table(TABLE_FEATURE_VOTES)
            .delete()
            .eq("feature_id", feature_id)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to delete old vote rows for feature %s during reboot; "
            "continuing — the reviver's vote insert may still succeed",
            feature_id,
            exc_info=True,
        )

    # ------------------------------------------------------------------
    # 5. Insert the reviver's vote row (R6)
    # ------------------------------------------------------------------
    try:
        await (
            supabase.table(TABLE_FEATURE_VOTES)
            .insert({"feature_id": feature_id, "user_id": user_id})
            .execute()
        )
    except Exception:  # noqa: BLE001
        # If the insert fails (e.g. the delete above didn't clear their
        # old row), the reboot still happened — log and continue.
        logger.warning(
            "Failed to insert reviver vote row for feature %s (user %s); "
            "upvotes count may drift by 1",
            feature_id,
            user_id,
            exc_info=True,
        )

    # ------------------------------------------------------------------
    # 6. File a decision record (R12, R13)
    # ------------------------------------------------------------------
    try:
        await record_decision(
            supabase,
            phase=DecisionPhase.LIFECYCLE,
            agent="reboot",
            decision={
                "action": "reboot",
                "feature_id": feature_id,
                "reviver_user_id": user_id,
                "previous_status": FeatureStatus.ARCHIVED,
                "new_status": FeatureStatus.VOTING,
            },
            model_version=PROGRAMMATIC,
            feature_id=feature_id,
        )
    except Exception:  # noqa: BLE001 — R13: never let decision failure change the response
        logger.warning(
            "Failed to record reboot decision for feature %s",
            feature_id,
            exc_info=True,
        )

    # ------------------------------------------------------------------
    # 7. Build and return the Feature response shape (R11)
    # ------------------------------------------------------------------
    # The reviver just voted, so viewer_has_voted is True for them.
    updated_feature["viewer_has_voted"] = True

    return _feature_to_response(updated_feature)