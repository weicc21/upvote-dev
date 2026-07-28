"""Pitch intake and public board reads.

Accepts a pitch into Redis without ever persisting it, and serves the
board from Postgres.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, time
from typing import Any

import redis.asyncio
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from supabase._async.client import AsyncClient

from backend.deps import (
    get_current_user_id,
    get_optional_user_id,
    get_redis,
    get_settings,
    get_supabase,
    raise_error,
)
from shared.config import Settings
from shared.constants import (
    DEFAULT_PENDING_PITCH_TTL_SECONDS,
    DEFAULT_PITCH_COIN_LIMIT,
    REDIS_FEATURE_INTAKE,
    REDIS_PENDING_PITCH,
    REDIS_PITCH_RATE,
    TABLE_FEATURE_REQUESTS,
    FeatureStatus,
)

router = APIRouter(prefix="/api/features", tags=["features"])

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

_VALID_VIEWS = {"pipeline", "shipped", "holding", "vault"}
_VALID_SORTS = {"top", "new"}

# Statuses visible per view
_VIEW_STATUSES: dict[str, set[str]] = {
    "pipeline": {
        FeatureStatus.VOTING,
        FeatureStatus.CONSOLIDATING,
        FeatureStatus.IN_SPRINT,
        FeatureStatus.SPLIT,
        FeatureStatus.COMPILED,
    },
    "shipped": {FeatureStatus.COMPILED},
    "holding": {FeatureStatus.POSTPONED_CONFLICT},
    "vault": {FeatureStatus.ARCHIVED},
}


class PitchBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=60)
    description: str = Field(..., min_length=30, max_length=300)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _next_utc_midnight_iso() -> str:
    """Return the next UTC midnight as an ISO-8601 string."""
    now = datetime.now(UTC)
    tomorrow = (now + __import__("datetime").timedelta(days=1)).date()
    midnight = datetime.combine(tomorrow, time.min, tzinfo=UTC)
    return midnight.isoformat()


def _encode_cursor(sort: str, row: dict[str, Any]) -> str:
    """Build an opaque keyset cursor from the last row."""
    if sort == "new":
        payload = {"created_at": row["created_at"], "id": row["id"]}
    else:
        payload = {
            "upvotes": row.get("upvotes", 0),
            "id": row["id"],
        }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()))  # type: ignore[no-any-return]
    except Exception:
        raise_error(400, "invalid_cursor", "The cursor value is malformed.")


# ---------------------------------------------------------------------------
# GET /api/features/mine  (R20 — declared BEFORE the {feature_id} route)
# ---------------------------------------------------------------------------


@router.get("/mine")
async def list_my_pitches(
    author_id: str = Depends(get_current_user_id),
    rds: redis.asyncio.Redis = Depends(get_redis),  # type: ignore[type-arg]
    db: AsyncClient = Depends(get_supabase),
) -> dict[str, Any]:
    """The caller's own pitches — pending (Redis) + persisted (Postgres)."""

    # --- Redis: SCAN for pending pitches (R15, R16) -----------------------
    prefix = f"pending_pitch:{author_id}:*"
    pending_raw: list[dict[str, Any]] = []
    cur: int | bytes = 0
    while True:
        cur, keys = await rds.scan(cursor=cur, match=prefix, count=100)
        for key in keys:
            raw = await rds.get(key)
            if raw is not None:
                try:
                    pending_raw.append(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    pass
        if cur == 0:
            break

    # --- Postgres: author's board rows (R18) ------------------------------
    resp = (
        await db.table(TABLE_FEATURE_REQUESTS)
        .select("*")
        .eq("author_id", author_id)
        .order("created_at", desc=True)
        .execute()
    )
    features: list[dict[str, Any]] = resp.data or []

    # Strip author_id from response bodies (R6)
    for f in features:
        f.pop("author_id", None)

    # --- Reconcile: drop pending entries already on the board (R17) -------
    board_ids = {f["id"] for f in features}
    pending = [
        p for p in pending_raw if p.get("feature_id") not in board_ids
    ]

    return {"pending": pending, "features": features}


# ---------------------------------------------------------------------------
# GET /api/features/{feature_id}
# ---------------------------------------------------------------------------


@router.get("/{feature_id}")
async def get_feature(
    feature_id: str,
    _user_id: str | None = Depends(get_optional_user_id),
    db: AsyncClient = Depends(get_supabase),
) -> dict[str, Any]:
    """Return a single board row (R8, R13)."""
    resp = (
        await db.table(TABLE_FEATURE_REQUESTS)
        .select("*")
        .eq("id", feature_id)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise_error(404, "not_found", "Feature not found.")
    row = rows[0]
    row.pop("author_id", None)  # R6
    return row


# ---------------------------------------------------------------------------
# GET /api/features  (board listing — R8–R12)
# ---------------------------------------------------------------------------


@router.get("")
async def list_features(
    view: str = Query(...),
    sort: str = Query("top"),
    q: str | None = Query(None),
    status: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    _user_id: str | None = Depends(get_optional_user_id),
    db: AsyncClient = Depends(get_supabase),
) -> dict[str, Any]:
    """Paginated board listing."""

    # Validate view
    if view not in _VALID_VIEWS:
        raise_error(400, "invalid_view", f"view must be one of {sorted(_VALID_VIEWS)}")

    # Validate sort
    if sort not in _VALID_SORTS:
        raise_error(400, "invalid_sort", f"sort must be one of {sorted(_VALID_SORTS)}")

    # Determine which statuses to filter on
    allowed_statuses = _VIEW_STATUSES[view]

    if status is not None:
        # status CSV is only valid for pipeline view (R12)
        if view != "pipeline":
            raise_error(
                400,
                "invalid_parameter",
                "The status filter is only valid for the pipeline view.",
            )
        requested = [s.strip() for s in status.split(",") if s.strip()]
        for s in requested:
            if s not in {v.value for v in FeatureStatus}:
                raise_error(400, "invalid_status", f"Unknown status value: {s}")
            if s not in allowed_statuses:
                raise_error(
                    400,
                    "invalid_status",
                    f"Status '{s}' is not valid for the {view} view.",
                )
        allowed_statuses = set(requested)

    # Build query
    query = db.table(TABLE_FEATURE_REQUESTS).select("*")
    query = query.in_("status", list(allowed_statuses))

    # Sort + keyset pagination (R10)
    if sort == "new":
        sort_col = "created_at"
    else:
        sort_col = "upvotes"

    if cursor is not None:
        decoded = _decode_cursor(cursor)
        if sort == "new":
            # keyset: (created_at, id) descending
            pivot_ts = decoded.get("created_at")
            pivot_id = decoded.get("id")
            if pivot_ts is None or pivot_id is None:
                raise_error(400, "invalid_cursor", "The cursor value is malformed.")
            # Rows where (created_at < pivot) OR (created_at == pivot AND id < pivot_id)
            query = query.or_(
                f"created_at.lt.{pivot_ts},"
                f"and(created_at.eq.{pivot_ts},id.lt.{pivot_id})"
            )
        else:
            # keyset: (upvotes, id) descending
            pivot_votes = decoded.get("upvotes")
            pivot_id = decoded.get("id")
            if pivot_votes is None or pivot_id is None:
                raise_error(400, "invalid_cursor", "The cursor value is malformed.")
            query = query.or_(
                f"upvotes.lt.{pivot_votes},"
                f"and(upvotes.eq.{pivot_votes},id.lt.{pivot_id})"
            )

    query = query.order(sort_col, desc=True).order("id", desc=True)
    # Fetch one extra to know if there's a next page
    query = query.limit(limit + 1)

    resp = await query.execute()
    rows: list[dict[str, Any]] = resp.data or []

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = _encode_cursor(sort, rows[-1])

    # Strip author_id (R6)
    for r in rows:
        r.pop("author_id", None)

    result: dict[str, Any] = {"features": rows}
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    return result


# ---------------------------------------------------------------------------
# POST /api/features  (pitch intake — R1–R7)
# ---------------------------------------------------------------------------


@router.post("", status_code=202)
async def create_pitch(
    body: PitchBody,
    author_id: str = Depends(get_current_user_id),
    rds: redis.asyncio.Redis = Depends(get_redis),  # type: ignore[type-arg]
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Accept a pitch onto the intake queue."""

    coin_limit = getattr(settings, "pitch_coin_limit", DEFAULT_PITCH_COIN_LIMIT)
    rate_key = REDIS_PITCH_RATE.format(author_id=author_id)

    # --- Rate-limit: per-author, per UTC calendar day (R4, R5) ------------
    current = await rds.get(rate_key)
    if current is not None and int(current) >= coin_limit:
        resets_at = _next_utc_midnight_iso()
        raise_error(
            429,
            "out_of_coins",
            "You have used all your Pitch Coins for today. Try again tomorrow.",
            resets_at=resets_at,
        )

    feature_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    # --- Pending-pitch record (R2: MUST exist before LPUSH) ---------------
    pending_key = REDIS_PENDING_PITCH.format(
        author_id=author_id, feature_id=feature_id
    )
    pending_payload = json.dumps(
        {
            "feature_id": feature_id,
            "title": body.title,
            "state": "screening",
            "submitted_at": now.isoformat(),
        }
    )
    pending_ttl = getattr(
        settings, "pending_pitch_ttl_seconds", DEFAULT_PENDING_PITCH_TTL_SECONDS
    )
    await rds.set(pending_key, pending_payload, ex=pending_ttl)

    # --- Enqueue for orchestrator -----------------------------------------
    intake_payload = json.dumps(
        {
            "feature_id": feature_id,
            "author_id": author_id,
            "title": body.title,
            "description": body.description,
            "submitted_at": now.isoformat(),
        }
    )
    await rds.lpush(REDIS_FEATURE_INTAKE, intake_payload)

    # --- Increment coin counter (R5: UTC calendar day) --------------------
    pipe = rds.pipeline(transaction=True)
    pipe.incr(rate_key)
    # Compute seconds until next UTC midnight for the TTL
    next_midnight = datetime.combine(
        (now + __import__("datetime").timedelta(days=1)).date(),
        time.min,
        tzinfo=UTC,
    )
    ttl_seconds = int((next_midnight - now).total_seconds()) + 1
    pipe.expire(rate_key, ttl_seconds)
    await pipe.execute()

    return {"feature_id": feature_id, "state": "screening"}