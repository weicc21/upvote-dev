"""Pitch intake and public board reads.

Accepts a pitch into Redis without ever persisting it, and serves the
board from Postgres.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query
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
    FeatureStatus,
    REDIS_FEATURE_INTAKE,
    REDIS_PENDING_PITCH,
    REDIS_PITCH_RATE,
    TABLE_FEATURE_REQUESTS,
)

router = APIRouter(prefix="/api/features", tags=["features"])

# ---------------------------------------------------------------------------
# Helpers — text cleaning & validation (R22–R27)
# ---------------------------------------------------------------------------

# Unicode categories Cc (control) and Cf (format), plus explicit zero-width
# and bidi-override codepoints.  We keep \n and \t selectively.
_ZERO_WIDTH_AND_BIDI = frozenset(
    "\u200b\u200c\u200d\u200e\u200f"  # zero-width
    "\u2028\u2029"  # line/paragraph separator
    "\u202a\u202b\u202c\u202d\u202e"  # bidi
    "\u2060\u2061\u2062\u2063\u2064"  # invisible operators
    "\ufeff\ufffe"  # BOM / noncharacter
    "\u00ad"  # soft hyphen
    "\u034f"  # combining grapheme joiner
    "\u061c"  # arabic letter mark
    "\u115f\u1160"  # hangul fillers
    "\u17b4\u17b5"  # khmer inherent vowels
    "\u180e"  # mongolian vowel separator
    "\uffa0"  # halfwidth hangul filler
)

# Tag-like constructs: <tag>, </tag>, <script…, and HTML entity escapes of
# the same.  We intentionally do NOT match bare < or > so that comparisons
# like "width < 300px" pass (R23).
_HTML_TAG_RE = re.compile(
    r"<\s*/?\s*[a-zA-Z]"  # <tag, </tag, < tag
    r"|"
    r"&lt;\s*/?\s*[a-zA-Z]"  # &lt;tag entity-escaped
    r"|"
    r"&#0*60;\s*/?\s*[a-zA-Z]"  # &#60;tag decimal entity
    r"|"
    r"&#x0*3[cC];\s*/?\s*[a-zA-Z]",  # &#x3c;tag hex entity
    re.IGNORECASE,
)


def _strip_control_chars(text: str, *, allow_newline: bool) -> str:
    """Remove Unicode control/format characters (R22).

    When *allow_newline* is True, ``\\n`` and ``\\t`` are preserved
    (description field).  Title is single-line, so they are stripped there.
    """
    out: list[str] = []
    for ch in text:
        if ch in _ZERO_WIDTH_AND_BIDI:
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("C"):  # Cc, Cf, Co, Cs, Cn
            if allow_newline and ch in ("\n", "\t"):
                out.append(ch)
                continue
            continue
        out.append(ch)
    return "".join(out)


def _validate_no_html(text: str, field: str) -> None:
    """Reject text containing HTML/script markup (R23, R27)."""
    if _HTML_TAG_RE.search(text):
        raise_error(
            400,
            "validation_failed",
            f"{field} must not contain HTML or script markup.",
        )


def _clean_and_validate(
    title_raw: str,
    description_raw: str,
) -> tuple[str, str]:
    """Run R22–R27 and return the cleaned (title, description)."""
    # R22 — strip control characters
    title = _strip_control_chars(title_raw, allow_newline=False)
    description = _strip_control_chars(description_raw, allow_newline=True)

    # R23 — reject HTML / script markup (before length check)
    _validate_no_html(title, "title")
    _validate_no_html(description, "description")

    # R24 — length bounds on *cleaned* text
    title = title.strip()
    description = description.strip()

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
# View → status mapping for GET /api/features
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

_VALID_VIEWS = frozenset(_VIEW_STATUSES)

# All canonical status values for CSV validation (R12)
_ALL_STATUS_VALUES = frozenset(s.value for s in FeatureStatus)


# ---------------------------------------------------------------------------
# Keyset cursor helpers
# ---------------------------------------------------------------------------


def _encode_cursor(sort_value: Any, feature_id: str) -> str:
    """Encode a keyset cursor as an opaque base64 string."""
    payload = json.dumps([str(sort_value), feature_id])
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """Decode an opaque cursor into (sort_value, feature_id)."""
    try:
        payload = base64.urlsafe_b64decode(cursor.encode()).decode()
        sort_value, feature_id = json.loads(payload)
        return str(sort_value), str(feature_id)
    except Exception:
        raise_error(400, "validation_failed", "Invalid cursor.")


# ---------------------------------------------------------------------------
# POST /api/features  (US-01)
# ---------------------------------------------------------------------------


@router.post("", status_code=202)
async def create_pitch(
    body: dict[str, Any],
    author_id: str = Depends(get_current_user_id),
    redis: aioredis.Redis = Depends(get_redis),  # type: ignore[type-arg]
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Accept a pitch onto the intake queue."""

    # --- Basic shape validation ---
    raw_title = body.get("title")
    raw_description = body.get("description")

    if not isinstance(raw_title, str) or not isinstance(raw_description, str):
        raise_error(
            400,
            "validation_failed",
            "title and description are required strings.",
        )

    # R26 — clean & validate BEFORE coin check
    # R22–R25
    title, description = _clean_and_validate(raw_title, raw_description)

    # --- Pitch Coin gate (R4, R5) ---
    rate_key = REDIS_PITCH_RATE.format(author_id=author_id)

    current_count_raw = await redis.get(rate_key)
    current_count = int(current_count_raw) if current_count_raw is not None else 0

    coin_limit = getattr(settings, "pitch_coin_limit", DEFAULT_PITCH_COIN_LIMIT)

    if current_count >= coin_limit:
        # R5 — resets_at is next UTC midnight
        now_utc = datetime.now(timezone.utc)
        tomorrow = (now_utc + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        resets_at = tomorrow.isoformat()
        raise_error(
            429,
            "out_of_coins",
            "You have used all your Pitch Coins for today. Try again tomorrow.",
            resets_at=resets_at,
        )

    # --- Generate feature id ---
    feature_id = str(uuid.uuid4())

    # --- R2: pending record BEFORE LPUSH ---
    pending_key = REDIS_PENDING_PITCH.format(
        author_id=author_id, feature_id=feature_id
    )
    pending_ttl = getattr(
        settings, "pending_pitch_ttl_seconds", DEFAULT_PENDING_PITCH_TTL_SECONDS
    )
    pending_record = json.dumps(
        {
            "feature_id": feature_id,
            "title": title,
            "state": "screening",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    await redis.set(pending_key, pending_record, ex=pending_ttl)

    # --- Enqueue to intake (R25 — cleaned text) ---
    intake_payload = json.dumps(
        {
            "feature_id": feature_id,
            "author_id": author_id,
            "title": title,
            "description": description,
        }
    )
    await redis.lpush(REDIS_FEATURE_INTAKE, intake_payload)

    # --- Increment coin counter (R5 — per UTC calendar day) ---
    pipe = redis.pipeline(transaction=True)
    pipe.incr(rate_key)
    # Calculate seconds until next UTC midnight for EXPIRE
    now_utc = datetime.now(timezone.utc)
    tomorrow_midnight = (now_utc + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    ttl_seconds = int((tomorrow_midnight - now_utc).total_seconds())
    if ttl_seconds < 1:
        ttl_seconds = 1
    pipe.expire(rate_key, ttl_seconds)
    await pipe.execute()

    # R1 — frozen response shape
    return {"feature_id": feature_id, "state": "screening"}


# ---------------------------------------------------------------------------
# GET /api/features/mine  (US-06)
# R20 — declared BEFORE the {feature_id} route
# ---------------------------------------------------------------------------


@router.get("/mine")
async def list_my_pitches(
    author_id: str = Depends(get_current_user_id),
    redis: aioredis.Redis = Depends(get_redis),  # type: ignore[type-arg]
    db: AsyncClient = Depends(get_supabase),
    _user: str | None = Depends(get_optional_user_id),  # unused, keeps signature consistent
) -> dict[str, Any]:
    """The author's private view — pending + persisted."""

    # --- Redis: pending pitches (R15, R16) ---
    scan_pattern = f"pending_pitch:{author_id}:*"
    pending_records: list[dict[str, Any]] = []
    cursor_val: int | bytes = 0
    while True:
        cursor_val, keys = await redis.scan(
            cursor=cursor_val, match=scan_pattern, count=100
        )
        for key in keys:
            raw = await redis.get(key)
            if raw is not None:
                try:
                    record = json.loads(raw)
                    pending_records.append(record)
                except (json.JSONDecodeError, TypeError):
                    pass
        if cursor_val == 0:
            break

    # --- Postgres: author's board rows (R18) ---
    response = (
        await db.table(TABLE_FEATURE_REQUESTS)
        .select("*")
        .eq("author_id", author_id)
        .order("created_at", desc=True)
        .execute()
    )
    features: list[dict[str, Any]] = response.data if response.data else []

    # R17 — filter out pending entries whose feature_id already appears in features
    persisted_ids = {f["id"] for f in features}
    pending_filtered = [
        p for p in pending_records if p.get("feature_id") not in persisted_ids
    ]

    # Build response features — R6: never include author_id
    features_out: list[dict[str, Any]] = []
    for f in features:
        feat = dict(f)
        feat.pop("author_id", None)
        # author_handle is nullable, we never populate it (no accounts table)
        if "author_handle" not in feat:
            feat["author_handle"] = None
        features_out.append(feat)

    # Build pending output
    pending_out: list[dict[str, Any]] = []
    for p in pending_filtered:
        entry: dict[str, Any] = {
            "feature_id": p.get("feature_id"),
            "title": p.get("title"),
            "state": p.get("state", "screening"),
            "submitted_at": p.get("submitted_at"),
        }
        # Include optional fields if present
        if "reason" in p:
            entry["reason"] = p["reason"]
        if "shipped_version" in p:
            entry["shipped_version"] = p["shipped_version"]
        if "merged_into_feature_id" in p:
            entry["merged_into_feature_id"] = p["merged_into_feature_id"]
        if "merged_into_title" in p:
            entry["merged_into_title"] = p["merged_into_title"]
        pending_out.append(entry)

    # R14 — both keys always present
    return {"pending": pending_out, "features": features_out}


# ---------------------------------------------------------------------------
# GET /api/features/{feature_id}  (US-05)
# ---------------------------------------------------------------------------


@router.get("/{feature_id}")
async def get_feature(
    feature_id: str,
    db: AsyncClient = Depends(get_supabase),
    _user: str | None = Depends(get_optional_user_id),
) -> dict[str, Any]:
    """Return a single board row (R8, R13)."""
    response = (
        await db.table(TABLE_FEATURE_REQUESTS)
        .select("*")
        .eq("id", feature_id)
        .execute()
    )

    if not response.data:
        raise_error(404, "not_found", "Feature not found.")

    feat = dict(response.data[0])
    # R6 — never return author_id
    feat.pop("author_id", None)
    if "author_handle" not in feat:
        feat["author_handle"] = None
    return feat


# ---------------------------------------------------------------------------
# GET /api/features  (US-05)
# ---------------------------------------------------------------------------


@router.get("")
async def list_features(
    view: str = Query(...),
    sort: str = Query("top"),
    q: str | None = Query(None),
    status: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    db: AsyncClient = Depends(get_supabase),
    _user: str | None = Depends(get_optional_user_id),
) -> dict[str, Any]:
    """List features by view with keyset pagination (R9–R12)."""

    # Validate view
    if view not in _VALID_VIEWS:
        raise_error(400, "validation_failed", f"view must be one of: {', '.join(sorted(_VALID_VIEWS))}.")

    # Validate sort
    if sort not in ("top", "new"):
        raise_error(400, "validation_failed", "sort must be 'top' or 'new'.")

    # Determine statuses to filter by
    allowed_statuses = _VIEW_STATUSES[view]

    if status is not None:
        # R12 — status CSV is only valid in pipeline view
        if view != "pipeline":
            raise_error(
                400,
                "validation_failed",
                "status filter is only valid for the pipeline view.",
            )
        requested = [s.strip() for s in status.split(",") if s.strip()]
        for s in requested:
            if s not in _ALL_STATUS_VALUES:
                raise_error(
                    400,
                    "validation_failed",
                    f"Unknown status value: use one of {', '.join(sorted(_ALL_STATUS_VALUES))}.",
                )
            if s not in [st for st in allowed_statuses]:
                raise_error(
                    400,
                    "validation_failed",
                    f"Status '{s}' is not valid for the pipeline view.",
                )
        allowed_statuses = requested

    # Determine sort column
    sort_column = "upvotes" if sort == "top" else "created_at"
    sort_desc = True  # both top and new sort descending

    # Build query
    query = db.table(TABLE_FEATURE_REQUESTS).select("*")

    # Filter by statuses
    query = query.in_("status", allowed_statuses)

    # Keyset pagination (R10)
    if cursor is not None:
        cursor_value, cursor_id = _decode_cursor(cursor)
        if sort_column == "upvotes":
            # For upvotes desc: (upvotes < cursor_value) OR (upvotes = cursor_value AND id > cursor_id)
            # PostgREST doesn't support OR natively, so we use the .or_ filter
            query = query.or_(
                f"{sort_column}.lt.{cursor_value},"
                f"and({sort_column}.eq.{cursor_value},id.gt.{cursor_id})"
            )
        else:
            # For created_at desc: (created_at < cursor_value) OR (created_at = cursor_value AND id > cursor_id)
            query = query.or_(
                f"{sort_column}.lt.{cursor_value},"
                f"and({sort_column}.eq.{cursor_value},id.gt.{cursor_id})"
            )

    # Order: sort_column desc, then id asc as tiebreaker
    query = query.order(sort_column, desc=sort_desc).order("id", desc=False)

    # Fetch one extra to determine if there's a next page
    query = query.limit(limit + 1)

    response = await query.execute()
    rows = response.data if response.data else []

    has_next = len(rows) > limit
    if has_next:
        rows = rows[:limit]

    # Build next_cursor
    next_cursor: str | None = None
    if has_next and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last[sort_column], last["id"])

    # R6 — strip author_id from every row
    features_out: list[dict[str, Any]] = []
    for row in rows:
        feat = dict(row)
        feat.pop("author_id", None)
        if "author_handle" not in feat:
            feat["author_handle"] = None
        features_out.append(feat)

    # R11 — features + cursor, no total count
    result: dict[str, Any] = {"features": features_out}
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    return result