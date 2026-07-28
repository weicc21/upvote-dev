"""One-vote-per-user upvote endpoint.

The vote row in ``feature_votes`` is the durable truth; the ``upvotes``
column on ``feature_requests`` is a denormalised cache maintained here.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from supabase._async.client import AsyncClient

from backend.deps import get_current_user_id, get_supabase, raise_error
from shared.constants import (
    FeatureStatus,
    TABLE_FEATURE_REQUESTS,
    TABLE_FEATURE_VOTES,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/features", tags=["votes"])


@router.post("/{id}/upvote")
async def upvote_feature(
    id: str,
    user_id: str = Depends(get_current_user_id),
    supabase: AsyncClient = Depends(get_supabase),
) -> dict[str, Any]:
    """Record one vote per user per feature and return the updated count.

    Ordering invariant (R4): the vote row is inserted **before** the
    cached count is incremented so that a unique-constraint violation on
    a repeat vote never inflates the total.
    """

    # ------------------------------------------------------------------
    # 1. Fetch the feature — 404 if missing, 422 if not votable (R8, R2)
    # ------------------------------------------------------------------
    feature_resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select("id, status, upvotes")
        .eq("id", id)
        .maybe_single()
        .execute()
    )

    feature = feature_resp.data
    if feature is None:
        raise_error(404, "not_found", "Feature not found")

    if feature["status"] != FeatureStatus.VOTING:
        raise_error(
            422,
            "not_votable",
            "Feature is not in VOTING status",
        )

    # ------------------------------------------------------------------
    # 2. Insert vote row — let the unique constraint reject repeats (R3)
    # ------------------------------------------------------------------
    try:
        await (
            supabase.table(TABLE_FEATURE_VOTES)
            .insert({"feature_id": id, "user_id": user_id})
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        exc_str = str(exc)
        # PostgREST surfaces unique-violation as 409 or code 23505.
        if "duplicate" in exc_str.lower() or "23505" in exc_str or "409" in exc_str:
            raise_error(409, "already_voted", "You have already voted for this feature")
        raise  # unexpected — let it propagate

    # ------------------------------------------------------------------
    # 3. Increment the cached count via server-side RPC (R5)
    #    If this fails the vote is still recorded; return 200 with a
    #    best-effort count (R6).
    # ------------------------------------------------------------------
    new_upvotes: int = feature["upvotes"] + 1  # fallback estimate

    try:
        rpc_resp = await supabase.rpc(
            "increment_upvotes",
            {"row_id": id},
        ).execute()

        # The RPC returns the new count (single integer or wrapped row).
        rpc_data = rpc_resp.data
        if isinstance(rpc_data, int):
            new_upvotes = rpc_data
        elif isinstance(rpc_data, list) and len(rpc_data) > 0:
            first = rpc_data[0]
            if isinstance(first, dict) and "upvotes" in first:
                new_upvotes = int(first["upvotes"])
            elif isinstance(first, int):
                new_upvotes = first
        elif isinstance(rpc_data, dict) and "upvotes" in rpc_data:
            new_upvotes = int(rpc_data["upvotes"])
    except Exception:  # noqa: BLE001
        # R6: vote is durable; log the drift and return 200 anyway.
        logger.warning(
            "increment_upvotes RPC failed for feature %s; "
            "returning best-effort count",
            id,
        )
        # Re-read the current count as a better fallback.
        try:
            fallback_resp = (
                await supabase.table(TABLE_FEATURE_REQUESTS)
                .select("upvotes")
                .eq("id", id)
                .single()
                .execute()
            )
            if fallback_resp.data:
                new_upvotes = int(fallback_resp.data["upvotes"])
        except Exception:  # noqa: BLE001
            pass  # stick with the pre-increment estimate

    # ------------------------------------------------------------------
    # 4. Return the frozen response shape (R1) — no user_id (R7)
    # ------------------------------------------------------------------
    return {"feature_id": id, "upvotes": new_upvotes}