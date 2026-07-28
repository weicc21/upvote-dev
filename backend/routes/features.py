"""Feature pitch intake and public board routes.

This module accepts pitches onto the Redis intake queue and reads the public
board from Postgres.  It never writes to Postgres, never calls an LLM, and
never screens, deduplicates, classifies, or publishes a pitch.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

import redis.asyncio
from fastapi import APIRouter, Depends, Query
from supabase._async.client import AsyncClient

from backend.deps import (
    ErrorResponse,
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
    FeatureStatus,
    REDIS_FEATURE_INTAKE,
    REDIS_PENDING_PITCH,
    REDIS_PITCH_RATE,
    TABLE_FEATURE_REQUESTS,
)

router = APIRouter(prefix="/api/features", tags=["features"])

# ---------------------------------------------------------------------------
# Helpers — text cleaning & validation
# ---------------------------------------------------------------------------

# Codepoints to strip unconditionally (control + format categories, plus
# explicit zero-width / bidi overrides that unicodedata sometimes classifies
# outside Cc/Cf).
_EXTRA_STRIP = frozenset(
    "\u200b\u200c\u200d\u200e\u200f"  # zero-width
    "\u2028\u2029"  # line/paragraph separator
    "\u202a\u202b\u202c\u202d\u202e"  # bidi
    "\u2066\u2067\u2068\u2069"  # bidi isolates
    "\ufeff"  # BOM / ZWNBSP
    "\ufff9\ufffa\ufffb"  # interlinear annotation
)

# Tag-like constructs — matches <tag, </tag, <script, and HTML entity
# escapes for the same.  Does NOT match bare < or > so "width < 300px" is
# fine.
_HTML_TAG_RE = re.compile(
    r"<\s*/?\s*[a-zA-Z]"  # <tag or </tag
    r"|&lt;\s*/?\s*[a-zA-Z]"  # &lt;tag entity-escaped
    r"|&#0*60;\s*/?\s*[a-zA-Z]"  # &#60;tag decimal entity
    r"|&#[xX]0*3[cC];\s*/?\s*[a-zA-Z]",  # &#x3c;tag hex entity
    re.IGNORECASE,
)


def _strip_control(text: str, *, allow_newline_tab: bool) -> str:
    """Remove Unicode control / format characters.

    When *allow_newline_tab* is True, ``\\n`` and ``\\t`` are preserved
    (description field).  Title is single-line so they are stripped there.
    """
    out: list[str] = []
    for ch in text:
        if ch in _EXTRA_STRIP:
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("Cc") or cat.startswith("Cf"):
            if allow_newline_tab and ch in ("\n", "\t"):
                out.append(ch)
                continue
            continue
        out.append(ch)
    return "".join(out)


def _validate_and_clean(title_raw: str, description_raw: str) -> tuple[str, str]:
    """Apply R22-R27: clean, reject HTML, enforce length on cleaned text.

    Returns (clean_title, clean_description) or raises via ``raise_error``.
    """
    # R22 — strip control characters
    title = _strip_control(title_raw, allow_newline_tab=False)
    description = _strip_control(description_raw, allow_newline_tab=True)

    # R23 — reject HTML / script markup (R27: never echo offending text)
    if _HTML_TAG_RE.search(title):
        raise_error(
            400,
            "validation_failed",
            "title must not contain HTML or script markup.",
        )
    if _HTML_TAG_RE.search(description):
        raise_error(
            400,
            "validation_failed",
            "description must not contain HTML or script markup.",
        )

    # R24 — length bounds on *cleaned* text
    if len(title) < 1 or len(title) > 60:
        raise_error(
            400,
            "validation_failed",
            "title must be between 1 and 60 characters after cleaning.",
        )
    if len(description) < 30 or len(description) > 300:
        raise_error(
            400,
            "validation_failed",
            "description must be between 30 and 300 characters after cleaning.",
        )

    return title, description


# ---------------------------------------------------------------------------
# Keyset cursor helpers
# ---------------------------------------------------------------------------

def _encode_cursor(sort: str, row: dict[str, Any]) -> str:
    """Build an opaque cursor from the last row on the page."""
    import base64

    if sort == "new":
        payload = json.dumps({"ca": row["created_at"], "id": row["id"]})
    else:  # top
        payload = json.dumps(
            {"uv": row["upvotes"], "ca": row["created_at"], "id": row["id"]}
        )
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str, sort: str) -> dict[str, Any]:
    """Decode an opaque cursor. Returns dict with keyset values."""
    import base64

    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        raise_error(400, "invalid_cursor", "The cursor value is malformed.")
    return payload  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# View → status mapping
# ---------------------------------------------------------------------------

_VIEW_STATUSES: dict[str, list[str]] = {
    "pipeline": [
        FeatureStatus.VOTING,
        FeatureStatus.CONSOLIDATING,
        FeatureStatus.IN_SPRINT,
        FeatureStatus.SPLIT,
    ],
    "shipped": [FeatureStatus.COMPILED],
    "holding": [FeatureStatus.POSTPONED_CONFLICT],
    "vault": [FeatureStatus.ARCHIVED],
}


# ---------------------------------------------------------------------------
# Feature serialisation
# ---------------------------------------------------------------------------

_FEATURE_COLUMNS = (
    "id, title, description, status, upvotes, author_id, author_handle, "
    "parent_id, created_at, updated_at"
)


def _row_to_feature(row: dict[str, Any], children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Convert a Postgres row to the public Feature shape (R6: no author_id)."""
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "upvotes": row["upvotes"],
        "author_handle": row.get("author_handle"),  # nullable, never invented
        "parent_id": row.get("parent_id"),
        "created_at": row["created_at"],
        "updated_at": row.get("updated_at"),
        "children": children if children is not None else [],
    }


# ---------------------------------------------------------------------------
# POST /api/features  (US-01)
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=202,
    responses={
        400: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)
async def create_pitch(
    body: dict[str, Any],
    author_id: str = Depends(get_current_user_id),
    rds: redis.asyncio.Redis = Depends(get_redis),  # type: ignore[type-arg]
    cfg: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Accept a pitch onto the intake queue (R1–R7, R22–R28)."""

    # --- basic shape ---
    if "title" not in body or "description" not in body:
        raise_error(400, "validation_failed", "Both title and description are required.")

    title_raw = body.get("title")
    description_raw = body.get("description")

    if not isinstance(title_raw, str) or not isinstance(description_raw, str):
        raise_error(400, "validation_failed", "title and description must be strings.")

    # R26 — validate *before* coin check
    # R22-R25 — clean, reject HTML, enforce length on cleaned text
    title, description = _validate_and_clean(title_raw, description_raw)

    # --- Pitch Coin gate (R4, R5) ---
    rate_key = REDIS_PITCH_RATE.format(author_id=author_id)
    current = await rds.get(rate_key)
    limit = getattr(cfg, "PITCH_COIN_LIMIT", DEFAULT_PITCH_COIN_LIMIT)

    if current is not None and int(current) >= limit:
        # Compute resets_at: next UTC midnight (R5)
        now_utc = datetime.now(timezone.utc)
        tomorrow = now_utc.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # If we're already at midnight exactly, still go to next day
        from datetime import timedelta

        tomorrow = tomorrow + timedelta(days=1)
        resets_at = tomorrow.isoformat()

        raise_error(
            429,
            "out_of_coins",
            "You have used all your Pitch Coins for today. Try again tomorrow.",
            resets_at=resets_at,
        )

    # --- generate id & timestamp ---
    feature_id = str(uuid.uuid4())
    submitted_at = datetime.now(timezone.utc).isoformat()

    # --- R2: pending record BEFORE LPUSH ---
    pending_key = REDIS_PENDING_PITCH.format(
        author_id=author_id, feature_id=feature_id
    )
    pending_record = json.dumps(
        {
            "feature_id": feature_id,
            "title": title,  # R25 — cleaned text
            "state": "screening",
            "submitted_at": submitted_at,
        }
    )
    ttl = getattr(cfg, "PENDING_PITCH_TTL_SECONDS", DEFAULT_PENDING_PITCH_TTL_SECONDS)
    await rds.set(pending_key, pending_record, ex=ttl)

    # --- LPUSH to intake (R28: exactly five keys) ---
    envelope = json.dumps(
        {
            "feature_id": feature_id,
            "author_id": author_id,
            "title": title,  # R25
            "description": description,  # R25
            "submitted_at": submitted_at,
        }
    )
    await rds.lpush(REDIS_FEATURE_INTAKE, envelope)

    # --- Increment coin counter (R5: per UTC calendar day) ---
    new_count = await rds.incr(rate_key)
    if new_count == 1:
        # First pitch today — set expiry to next UTC midnight
        now_utc = datetime.now(timezone.utc)
        from datetime import timedelta

        tomorrow = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        seconds_until_midnight = int((tomorrow - now_utc).total_seconds())
        if seconds_until_midnight <= 0:
            seconds_until_midnight = 86400
        await rds.expire(rate_key, seconds_until_midnight)

    # R1 — frozen response shape
    return {"feature_id": feature_id, "state": "screening"}


# ---------------------------------------------------------------------------
# GET /api/features/mine  (US-06) — R20: declared BEFORE {feature_id}
# ---------------------------------------------------------------------------


@router.get("/mine")
async def list_my_pitches(
    author_id: str = Depends(get_current_user_id),
    rds: redis.asyncio.Redis = Depends(get_redis),  # type: ignore[type-arg]
    supabase: AsyncClient = Depends(get_supabase),
) -> dict[str, Any]:
    """The author's private view: pending (Redis) + persisted (Postgres).

    R14-R21.
    """

    # --- Postgres: author's board rows (R18) ---
    resp = await (
        supabase.table(TABLE_FEATURE_REQUESTS)
        .select(_FEATURE_COLUMNS)
        .eq("author_id", author_id)
        .order("created_at", desc=True)
        .execute()
    )
    pg_rows: list[dict[str, Any]] = resp.data or []
    pg_feature_ids: set[str] = {r["id"] for r in pg_rows}

    features = [_row_to_feature(r) for r in pg_rows]

    # --- Redis: pending pitches (R15, R16) ---
    prefix = f"pending_pitch:{author_id}:*"
    pending: list[dict[str, Any]] = []
    cursor_val: int | bytes = 0
    while True:
        cursor_val, keys = await rds.scan(cursor=cursor_val, match=prefix, count=100)
        for key in keys:
            raw = await rds.get(key)
            if raw is None:
                continue
            record = json.loads(raw)
            # R17 — skip if already on the board
            if record.get("feature_id") in pg_feature_ids:
                continue
            pending.append(record)
        if cursor_val == 0:
            break

    # Sort pending by submitted_at descending for consistency
    pending.sort(key=lambda p: p.get("submitted_at", ""), reverse=True)

    # R14 — frozen shape
    return {"pending": pending, "features": features}


# ---------------------------------------------------------------------------
# GET /api/features/{feature_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{feature_id}",
    responses={404: {"model": ErrorResponse}},
)
async def get_feature(
    feature_id: str,
    _user_id: str | None = Depends(get_optional_user_id),
    supabase: AsyncClient = Depends(get_supabase),
) -> dict[str, Any]:
    """Return a single feature by id (R13, R31)."""

    resp = await (
        supabase.table(TABLE_FEATURE_REQUESTS)
        .select(_FEATURE_COLUMNS)
        .eq("id", feature_id)
        .execute()
    )
    rows: list[dict[str, Any]] = resp.data or []
    if not rows:
        raise_error(404, "not_found", "Feature not found.")

    row = rows[0]

    # Load children if this is a SPLIT parent (R30)
    children: list[dict[str, Any]] = []
    if row.get("status") == FeatureStatus.SPLIT:
        child_resp = await (
            supabase.table(TABLE_FEATURE_REQUESTS)
            .select(_FEATURE_COLUMNS)
            .eq("parent_id", feature_id)
            .order("created_at", desc=False)
            .execute()
        )
        children = [_row_to_feature(c) for c in (child_resp.data or [])]

    return _row_to_feature(row, children=children)


# ---------------------------------------------------------------------------
# GET /api/features  (US-05)
# ---------------------------------------------------------------------------


@router.get(
    "",
    responses={400: {"model": ErrorResponse}},
)
async def list_features(
    view: str = Query(...),
    sort: str = Query("top"),
    q: str | None = Query(None),
    status: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    _user_id: str | None = Depends(get_optional_user_id),
    supabase: AsyncClient = Depends(get_supabase),
) -> dict[str, Any]:
    """List features by view with keyset pagination (R8-R13, R29-R33)."""

    # --- validate view ---
    if view not in _VIEW_STATUSES:
        raise_error(
            400,
            "validation_failed",
            f"view must be one of: {', '.join(_VIEW_STATUSES.keys())}.",
        )

    # --- validate sort ---
    if sort not in ("top", "new"):
        raise_error(400, "validation_failed", "sort must be 'top' or 'new'.")

    # --- determine statuses ---
    allowed_statuses = _VIEW_STATUSES[view]

    if status is not None:
        if view != "pipeline":
            raise_error(
                400,
                "validation_failed",
                "status filter is only valid for the pipeline view.",
            )
        requested = [s.strip() for s in status.split(",") if s.strip()]
        # R12 — reject unknown status values
        valid_enum_values = {e.value for e in FeatureStatus}
        for s in requested:
            if s not in valid_enum_values:
                raise_error(
                    400,
                    "validation_failed",
                    f"Unknown status value: use one of {', '.join(sorted(valid_enum_values))}.",
                )
            if s not in allowed_statuses:
                raise_error(
                    400,
                    "validation_failed",
                    f"Status '{s}' is not valid in the pipeline view.",
                )
        allowed_statuses = requested

    # R29 — root rows only (parent_id is null)
    query = (
        supabase.table(TABLE_FEATURE_REQUESTS)
        .select(_FEATURE_COLUMNS)
        .in_("status", allowed_statuses)
        .is_("parent_id", "null")
    )

    # --- keyset pagination (R10) ---
    if sort == "top":
        if cursor:
            decoded = _decode_cursor(cursor, sort)
            uv = decoded.get("uv")
            ca = decoded.get("ca")
            cid = decoded.get("id")
            # Keyset: (upvotes, created_at DESC, id DESC)
            # "less than" in sort order means: lower upvotes, or same upvotes
            # but older, or same upvotes+created_at but smaller id.
            query = query.or_(
                f"upvotes.lt.{uv},"
                f"and(upvotes.eq.{uv},created_at.lt.{ca}),"
                f"and(upvotes.eq.{uv},created_at.eq.{ca},id.lt.{cid})"
            )
        query = query.order("upvotes", desc=True).order("created_at", desc=True).order("id", desc=True)
    else:  # new
        if cursor:
            decoded = _decode_cursor(cursor, sort)
            ca = decoded.get("ca")
            cid = decoded.get("id")
            query = query.or_(
                f"created_at.lt.{ca},"
                f"and(created_at.eq.{ca},id.lt.{cid})"
            )
        query = query.order("created_at", desc=True).order("id", desc=True)

    # Fetch limit+1 to know if there's a next page
    query = query.limit(limit + 1)
    resp = await query.execute()
    rows: list[dict[str, Any]] = resp.data or []

    has_next = len(rows) > limit
    if has_next:
        rows = rows[:limit]

    # R32 — batch-fetch children for all SPLIT parents on this page
    split_parent_ids = [r["id"] for r in rows if r.get("status") == FeatureStatus.SPLIT]
    children_by_parent: dict[str, list[dict[str, Any]]] = {}

    if split_parent_ids:
        child_resp = await (
            supabase.table(TABLE_FEATURE_REQUESTS)
            .select(_FEATURE_COLUMNS)
            .in_("parent_id", split_parent_ids)
            .order("created_at", desc=False)
            .execute()
        )
        for child_row in (child_resp.data or []):
            pid = child_row["parent_id"]
            children_by_parent.setdefault(pid, []).append(_row_to_feature(child_row))

    # R33 — always include children as a list (empty if none)
    features = [
        _row_to_feature(r, children=children_by_parent.get(r["id"], []))
        for r in rows
    ]

    result: dict[str, Any] = {"features": features}
    if has_next and rows:
        result["next_cursor"] = _encode_cursor(sort, rows[-1])

    return result