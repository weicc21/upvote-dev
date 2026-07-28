"""Pitch intake and public board reads.

Accepts a pitch into Redis without ever persisting it, and serves the
board from Postgres.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
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
    REDIS_FEATURE_INTAKE,
    REDIS_PENDING_PITCH,
    REDIS_PITCH_RATE,
    TABLE_FEATURE_REQUESTS,
    FeatureStatus,
)

router = APIRouter(prefix="/api/features", tags=["features"])

# ---------------------------------------------------------------------------
# View → status mapping for the board
# ---------------------------------------------------------------------------

_VIEW_STATUSES: dict[str, list[str]] = {
    "pipeline": [
        FeatureStatus.VOTING,
        FeatureStatus.CONSOLIDATING,
        FeatureStatus.IN_SPRINT,
    ],
    "shipped": [FeatureStatus.COMPILED],
    "holding": [FeatureStatus.POSTPONED_CONFLICT, FeatureStatus.SPLIT],
    "vault": [FeatureStatus.ARCHIVED],
}

_VALID_VIEWS = frozenset(_VIEW_STATUSES)

# All canonical status values that the `status` CSV filter may contain
_CANONICAL_STATUSES = frozenset(s.value for s in FeatureStatus)

# ---------------------------------------------------------------------------
# Sanitisation helpers (R22–R27)
# ---------------------------------------------------------------------------

# Characters to strip: Unicode categories Cc and Cf, plus explicit
# zero-width / bidi codepoints.  We keep \n and \t conditionally.
_CONTROL_RE_TITLE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f"  # Cc minus \t(\x09) and \n(\x0a)
    r"\u00ad\u034f\u061c"
    r"\u115f\u1160\u17b4\u17b5"
    r"\u180b-\u180f"
    r"\u200b-\u200f"
    r"\u202a-\u202e"
    r"\u2060-\u206f"
    r"\u3164"
    r"\ufe00-\ufe0f"
    r"\ufeff\uffa0"
    r"\ufff0-\ufff8"
    r"\U000e0001\U000e0020-\U000e007f"
    r"\U000e0100-\U000e01ef"
    r"\t\n"  # title is single-line: strip \t and \n too
    r"]+",
)

_CONTROL_RE_DESC = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f"  # Cc minus \t and \n
    r"\u00ad\u034f\u061c"
    r"\u115f\u1160\u17b4\u17b5"
    r"\u180b-\u180f"
    r"\u200b-\u200f"
    r"\u202a-\u202e"
    r"\u2060-\u206f"
    r"\u3164"
    r"\ufe00-\ufe0f"
    r"\ufeff\uffa0"
    r"\ufff0-\ufff8"
    r"\U000e0001\U000e0020-\U000e007f"
    r"\U000e0100-\U000e01ef"
    r"]+",
)

# HTML / script detection (R23): tag-like constructs and HTML entity escapes
# for the same.  Bare < or > alone do NOT match.
_HTML_RE = re.compile(
    r"<\s*/?\s*[a-zA-Z]"  # <tag, </tag, < tag
    r"|&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);",  # &#60; &#x3c; &lt;
    re.IGNORECASE,
)


def _strip_controls(text: str, *, is_title: bool) -> str:
    """Remove Unicode control / invisible characters (R22)."""
    pattern = _CONTROL_RE_TITLE if is_title else _CONTROL_RE_DESC
    # Also strip Cf-category chars that the regex might miss via unicodedata
    cleaned = pattern.sub("", text)
    # Second pass: any remaining Cc/Cf chars (belt-and-suspenders)
    result: list[str] = []
    for ch in cleaned:
        cat = unicodedata.category(ch)
        if cat == "Cc":
            # Keep \n and \t in description only
            if not is_title and ch in ("\n", "\t"):
                result.append(ch)
            # else drop
        elif cat == "Cf":
            pass  # drop
        else:
            result.append(ch)
    return "".join(result)


def _check_html(text: str, field: str) -> None:
    """Reject text containing HTML / script markup (R23, R27)."""
    if _HTML_RE.search(text):
        raise_error(
            400,
            "validation_failed",
            f"{field} must not contain HTML or script markup.",
        )


def _validate_and_clean(title_raw: str, desc_raw: str) -> tuple[str, str]:
    """Full R22–R27 pipeline. Returns (clean_title, clean_description)."""
    # R22: strip controls first
    title = _strip_controls(title_raw, is_title=True).strip()
    description = _strip_controls(desc_raw, is_title=False).strip()

    # R23: reject HTML (on cleaned text — controls already gone)
    _check_html(title, "title")
    _check_html(description, "description")

    # R24: length bounds on cleaned text
    if len(title) < 1 or len(title) > 60:
        raise_error(
            400,
            "validation_failed",
            "title must be between 1 and 60 characters.",
        )
    if len(description) < 30 or len(description) > 300:
        raise_error(
            400,
            "validation_failed",
            "description must be between 30 and 300 characters.",
        )

    return title, description


# ---------------------------------------------------------------------------
# Keyset cursor helpers (R10)
# ---------------------------------------------------------------------------


def _encode_cursor(sort_value: Any, feature_id: str) -> str:
    """Produce an opaque cursor string from the sort column value + id."""
    import base64

    payload = json.dumps({"v": str(sort_value), "id": feature_id})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """Return (sort_value_str, feature_id) from an opaque cursor."""
    import base64

    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return payload["v"], payload["id"]
    except Exception:
        raise_error(400, "validation_failed", "Invalid cursor.")


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
    rds: aioredis.Redis = Depends(get_redis),  # type: ignore[type-arg]
    cfg: Settings = Depends(get_settings),
) -> dict[str, Any]:
    # --- basic shape check ---
    if not isinstance(body.get("title"), str) or not isinstance(
        body.get("description"), str
    ):
        raise_error(
            400,
            "validation_failed",
            "title and description are required strings.",
        )

    title_raw: str = body["title"]
    desc_raw: str = body["description"]

    # R26: validate/clean BEFORE coin check
    # R22–R25: sanitise and validate
    title, description = _validate_and_clean(title_raw, desc_raw)

    # --- Pitch Coin gate (R4, R5) ---
    rate_key = REDIS_PITCH_RATE.format(author_id=author_id)
    current = await rds.get(rate_key)
    limit = getattr(cfg, "pitch_coin_limit", DEFAULT_PITCH_COIN_LIMIT)

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

    # --- Generate id and timestamp ---
    feature_id = str(uuid.uuid4())
    submitted_at = datetime.now(timezone.utc).isoformat()

    # --- R2: pending record BEFORE LPUSH ---
    pending_key = REDIS_PENDING_PITCH.format(
        author_id=author_id, feature_id=feature_id
    )
    pending_ttl = getattr(
        cfg, "pending_pitch_ttl_seconds", DEFAULT_PENDING_PITCH_TTL_SECONDS
    )
    pending_record = json.dumps(
        {
            "feature_id": feature_id,
            "title": title,  # R25: cleaned text
            "state": "screening",
            "submitted_at": submitted_at,
        }
    )
    await rds.set(pending_key, pending_record, ex=pending_ttl)

    # --- Enqueue intake (R28: exactly five keys) ---
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

    # --- Spend the coin AFTER successful enqueue ---
    pipe = rds.pipeline(transaction=True)
    pipe.incr(rate_key)
    # Compute seconds until next UTC midnight for EXPIRE
    now_utc = datetime.now(timezone.utc)
    next_midnight = now_utc.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    from datetime import timedelta

    next_midnight = next_midnight + timedelta(days=1)
    ttl_seconds = int((next_midnight - now_utc).total_seconds()) + 1
    pipe.expire(rate_key, ttl_seconds)
    await pipe.execute()

    # R1: frozen response shape
    return {"feature_id": feature_id, "state": "screening"}


# ---------------------------------------------------------------------------
# GET /api/features/mine  (US-06)
# R20: declared BEFORE the {feature_id} route
# ---------------------------------------------------------------------------


@router.get(
    "/mine",
    responses={401: {"model": ErrorResponse}},
)
async def list_my_pitches(
    author_id: str = Depends(get_current_user_id),  # R21
    rds: aioredis.Redis = Depends(get_redis),  # type: ignore[type-arg]
    db: AsyncClient = Depends(get_supabase),
) -> dict[str, Any]:
    # --- Postgres: author's board rows (R18) ---
    resp = (
        await db.table(TABLE_FEATURE_REQUESTS)
        .select("*")
        .eq("author_id", author_id)
        .execute()
    )
    features: list[dict[str, Any]] = resp.data or []

    # Strip author_id from each feature row (R6)
    board_feature_ids: set[str] = set()
    cleaned_features: list[dict[str, Any]] = []
    for f in features:
        f.pop("author_id", None)
        board_feature_ids.add(f["id"])
        cleaned_features.append(f)

    # --- Redis: pending pitches via SCAN (R15, R16) ---
    prefix = f"pending_pitch:{author_id}:*"
    pending: list[dict[str, Any]] = []
    cursor_val: int | bytes = 0
    while True:
        cursor_val, keys = await rds.scan(
            cursor=cursor_val, match=prefix, count=100
        )
        for key in keys:
            raw = await rds.get(key)
            if raw is None:
                continue
            record = json.loads(raw)
            # R17: skip if already on the board
            if record.get("feature_id") in board_feature_ids:
                continue
            pending.append(record)
        if cursor_val == 0:
            break

    # R14: frozen shape
    return {"pending": pending, "features": cleaned_features}


# ---------------------------------------------------------------------------
# GET /api/features/{feature_id}  (US-05, single)
# ---------------------------------------------------------------------------


@router.get(
    "/{feature_id}",
    responses={404: {"model": ErrorResponse}},
)
async def get_feature(
    feature_id: str,
    _user_id: str | None = Depends(get_optional_user_id),
    db: AsyncClient = Depends(get_supabase),
) -> dict[str, Any]:
    resp = (
        await db.table(TABLE_FEATURE_REQUESTS)
        .select("*")
        .eq("id", feature_id)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise_error(404, "not_found", "Feature not found.")  # R13

    feature = rows[0]
    feature.pop("author_id", None)  # R6
    return feature


# ---------------------------------------------------------------------------
# GET /api/features  (US-05, list / board)
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
    db: AsyncClient = Depends(get_supabase),
) -> dict[str, Any]:
    # --- Validate view (R9) ---
    if view not in _VALID_VIEWS:
        raise_error(400, "validation_failed", f"Unknown view: {view}")

    # --- Validate sort (R9) ---
    if sort not in ("top", "new"):
        raise_error(400, "validation_failed", f"Unknown sort: {sort}")

    # --- Determine statuses to filter ---
    statuses = list(_VIEW_STATUSES[view])

    # R12: status CSV filter (pipeline view only)
    if status is not None:
        if view != "pipeline":
            raise_error(
                400,
                "validation_failed",
                "status filter is only valid for the pipeline view.",
            )
        requested = [s.strip() for s in status.split(",") if s.strip()]
        for s in requested:
            if s not in _CANONICAL_STATUSES:
                raise_error(400, "validation_failed", f"Unknown status: {s}")
        # Intersect with the view's allowed statuses
        allowed = set(_VIEW_STATUSES[view])
        statuses = [s for s in requested if s in allowed]
        if not statuses:
            return {"features": [], "next_cursor": None}

    # --- Sort column ---
    sort_col = "upvotes" if sort == "top" else "created_at"
    desc = True  # both sorts are descending

    # --- Build query ---
    query = (
        db.table(TABLE_FEATURE_REQUESTS)
        .select("*")
        .in_("status", statuses)
    )

    # --- Keyset pagination (R10) ---
    if cursor is not None:
        cursor_val, cursor_id = _decode_cursor(cursor)
        if sort_col == "upvotes":
            # For descending upvotes: (upvotes, id) < (cursor_val, cursor_id)
            # PostgREST: or=(upvotes.lt.{v}, and(upvotes.eq.{v},id.gt.{cursor_id}))
            # We use a simpler approach: filter upvotes <= cursor_val, then
            # exclude rows we've already seen.
            query = query.lte(sort_col, int(cursor_val))
        else:
            # created_at descending
            query = query.lte(sort_col, cursor_val)

    # Order: sort_col desc, then id asc as tiebreaker
    query = query.order(sort_col, desc=desc).order("id", desc=False)

    # Fetch one extra to detect next page
    query = query.limit(limit + 1)

    resp = await query.execute()
    rows: list[dict[str, Any]] = resp.data or []

    # If we have a cursor, we need to skip rows that match the cursor
    # position exactly (already seen).
    if cursor is not None:
        cursor_val_str, cursor_id = _decode_cursor(cursor)
        filtered: list[dict[str, Any]] = []
        for row in rows:
            row_sort = str(row[sort_col])
            row_id = row["id"]
            if row_sort == cursor_val_str and row_id <= cursor_id:
                continue
            filtered.append(row)
        rows = filtered

    # Determine next_cursor
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = _encode_cursor(last[sort_col], last["id"])

    # R6: strip author_id
    for row in rows:
        row.pop("author_id", None)

    # R11: frozen shape
    result: dict[str, Any] = {"features": rows}
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    else:
        result["next_cursor"] = None

    return result