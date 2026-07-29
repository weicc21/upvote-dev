"""Feature pitch intake and public board reads.

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
from pydantic import BaseModel, Field
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
    TABLE_FEATURE_VOTES,
)

router = APIRouter(prefix="/api/features", tags=["features"])

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class PitchBody(BaseModel):
    title: str
    description: str


class PitchAccepted(BaseModel):
    feature_id: str
    state: str = "screening"


class PendingPitch(BaseModel):
    feature_id: str
    title: str
    state: str
    reason: str | None = None
    shipped_version: str | None = None
    merged_into_feature_id: str | None = None
    merged_into_title: str | None = None
    submitted_at: str


class FeatureOut(BaseModel):
    id: str
    title: str
    description: str
    status: str
    upvotes: int
    author_handle: str | None = None
    parent_id: str | None = None
    split_depth: int | None = None
    unlock_threshold: int | None = None
    extends_id: str | None = None
    extends_title: str | None = None
    postpone_count: int | None = None
    ai_explanation: str | None = None
    merge_count: int | None = None
    shipped_version: str | None = None
    shipped_at: str | None = None
    viewer_has_voted: bool = False
    children: list[FeatureOut] = Field(default_factory=list)
    created_at: str
    updated_at: str


FeatureOut.model_rebuild()


class FeatureListResponse(BaseModel):
    features: list[FeatureOut]
    next_cursor: str | None = None


class MyPitchesResponse(BaseModel):
    pending: list[PendingPitch]
    features: list[FeatureOut]


# ---------------------------------------------------------------------------
# Text cleaning helpers (R22–R27)
# ---------------------------------------------------------------------------

# Characters to strip: Unicode Cc/Cf categories, zero-width, bidi overrides.
# We keep \n and \t only in description.
_CONTROL_RE_TITLE = re.compile(
    r"[\x00-\x1f\x7f-\x9f"  # C0/C1 controls (includes \n \t \r)
    r"\u200b-\u200f"  # zero-width / bidi marks
    r"\u2028\u2029"  # line/paragraph separators
    r"\u202a-\u202e"  # bidi embedding/override
    r"\u2060-\u2064"  # invisible operators
    r"\u2066-\u2069"  # bidi isolates
    r"\ufeff"  # BOM / zero-width no-break space
    r"\ufff9-\ufffb"  # interlinear annotations
    r"]",
)

_CONTROL_RE_DESC = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"  # C0/C1 minus \t(\x09) \n(\x0a) \r(\x0d)
    r"\u200b-\u200f"
    r"\u2028\u2029"
    r"\u202a-\u202e"
    r"\u2060-\u2064"
    r"\u2066-\u2069"
    r"\ufeff"
    r"\ufff9-\ufffb"
    r"]",
)

# Also strip Cf-category chars that the regex above might miss (e.g. SHY \u00ad).
def _strip_controls(text: str, *, allow_newline: bool) -> str:
    """Remove invisible / control codepoints."""
    if allow_newline:
        text = _CONTROL_RE_DESC.sub("", text)
        # Normalise \r\n -> \n, lone \r -> \n
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = _CONTROL_RE_TITLE.sub("", text)
    # Sweep remaining Cc/Cf by category (except \n \t in desc)
    out: list[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cc":
            if allow_newline and ch in ("\n", "\t"):
                out.append(ch)
            # else drop
        elif cat == "Cf":
            pass  # drop
        else:
            out.append(ch)
    return "".join(out)


# Tag-like constructs: <tag>, </tag>, <script…, and HTML entity escapes of the same.
_MARKUP_RE = re.compile(
    r"<\s*/?\s*[a-zA-Z]"  # <tag, </tag, < tag
    r"|&lt;\s*/?\s*[a-zA-Z]"  # &lt;tag entity-escaped
    r"|&#0*60;\s*/?\s*[a-zA-Z]"  # &#60;tag decimal
    r"|&#[xX]0*3[cC];\s*/?\s*[a-zA-Z]",  # &#x3c;tag hex
    re.IGNORECASE,
)


def _has_markup(text: str) -> bool:
    return bool(_MARKUP_RE.search(text))


def _validate_and_clean(title_raw: str, desc_raw: str) -> tuple[str, str]:
    """R22–R27: clean, reject markup, enforce length on cleaned text."""
    # R22: strip controls
    title = _strip_controls(title_raw, allow_newline=False)
    desc = _strip_controls(desc_raw, allow_newline=True)

    # R23: reject markup (R27: do not echo offending text)
    if _has_markup(title):
        raise_error(400, "validation_failed", "title must not contain HTML or script markup")
    if _has_markup(desc):
        raise_error(400, "validation_failed", "description must not contain HTML or script markup")

    # R24: length bounds on cleaned text
    if len(title) < 1 or len(title) > 60:
        raise_error(
            400,
            "validation_failed",
            "title must be between 1 and 60 characters",
        )
    if len(desc) < 30 or len(desc) > 300:
        raise_error(
            400,
            "validation_failed",
            "description must be between 30 and 300 characters",
        )

    return title, desc


# ---------------------------------------------------------------------------
# Keyset cursor helpers
# ---------------------------------------------------------------------------

import base64


def _encode_cursor(sort: str, row: dict[str, Any]) -> str:
    """Produce an opaque cursor from the last row on the page."""
    if sort == "new":
        payload = {"created_at": row["created_at"], "id": row["id"]}
    else:  # top
        payload = {"upvotes": row["upvotes"], "id": row["id"]}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()))  # type: ignore[no-any-return]
    except Exception:
        raise_error(400, "invalid_cursor", "The cursor value is malformed")


# ---------------------------------------------------------------------------
# View → status mapping
# ---------------------------------------------------------------------------

_VIEW_STATUSES: dict[str, list[str]] = {
    # R39: a SPLIT parent is a pipeline card — its children are open for voting
    # and the unlock tree is a pipeline interaction. Filing it under holding
    # says "blocked" about an idea that was broken into votable parts.
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

# Statuses valid for the `status` CSV filter (pipeline view only)
_PIPELINE_STATUSES = {FeatureStatus.VOTING, FeatureStatus.CONSOLIDATING, FeatureStatus.IN_SPRINT}

# ---------------------------------------------------------------------------
# Feature row → FeatureOut helper
# ---------------------------------------------------------------------------

_FEATURE_COLUMNS = (
    "id, title, description, status, upvotes, author_handle, parent_id, "
    "split_depth, unlock_threshold, extends_id, extends_title, "
    "postpone_count, ai_explanation, merge_count, created_at, updated_at"
)

_SHIPPED_META_VIEW = "feature_shipped_meta"


def _row_to_feature(
    row: dict[str, Any],
    *,
    shipped_map: dict[str, dict[str, Any]] | None = None,
    voted_ids: set[str] | None = None,
    children_map: dict[str, list[dict[str, Any]]] | None = None,
) -> FeatureOut:
    rid = str(row["id"])
    shipped = (shipped_map or {}).get(rid, {})
    child_rows = (children_map or {}).get(rid, [])
    child_features = [
        _row_to_feature(
            c,
            shipped_map=shipped_map,
            voted_ids=voted_ids,
            children_map=None,  # children don't nest further in MVP
        )
        for c in child_rows
    ]
    return FeatureOut(
        id=rid,
        title=row["title"],
        description=row["description"],
        status=row["status"],
        upvotes=row["upvotes"],
        author_handle=row.get("author_handle"),
        parent_id=str(row["parent_id"]) if row.get("parent_id") else None,
        split_depth=row.get("split_depth"),
        unlock_threshold=row.get("unlock_threshold"),
        extends_id=str(row["extends_id"]) if row.get("extends_id") else None,
        extends_title=row.get("extends_title"),
        postpone_count=row.get("postpone_count"),
        ai_explanation=row.get("ai_explanation"),
        merge_count=row.get("merge_count"),
        shipped_version=shipped.get("version"),
        shipped_at=str(shipped["deployed_at"]) if shipped.get("deployed_at") else None,
        viewer_has_voted=rid in (voted_ids or set()),
        children=child_features,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


async def _fetch_shipped_meta(
    supabase: AsyncClient, feature_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Batch-fetch shipped metadata for a list of feature ids (R36)."""
    if not feature_ids:
        return {}
    resp = (
        await supabase.table(_SHIPPED_META_VIEW)
        # The view exposes `version`/`deployed_at`, not the wire names (module_map).
        .select("feature_id, version, deployed_at")
        .in_("feature_id", feature_ids)
        .execute()
    )
    return {str(r["feature_id"]): r for r in (resp.data or [])}


async def _fetch_voted_ids(
    supabase: AsyncClient, feature_ids: list[str], viewer_id: str | None
) -> set[str]:
    """Batch-fetch which features the viewer has voted on (R37)."""
    if not viewer_id or not feature_ids:
        return set()
    resp = (
        await supabase.table(TABLE_FEATURE_VOTES)
        .select("feature_id")
        .eq("user_id", viewer_id)
        .in_("feature_id", feature_ids)
        .execute()
    )
    return {str(r["feature_id"]) for r in (resp.data or [])}


async def _fetch_children(
    supabase: AsyncClient, parent_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Batch-fetch children for a list of parent ids (R32), oldest-first (R30)."""
    if not parent_ids:
        return {}
    resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select(_FEATURE_COLUMNS)
        .in_("parent_id", parent_ids)
        .order("created_at", desc=False)
        .execute()
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for r in resp.data or []:
        pid = str(r["parent_id"])
        result.setdefault(pid, []).append(r)
    return result


# ---------------------------------------------------------------------------
# POST /api/features  (US-01)
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=202,
    response_model=PitchAccepted,
    responses={400: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
async def create_pitch(
    body: PitchBody,
    author_id: str = Depends(get_current_user_id),
    redis: aioredis.Redis = Depends(get_redis),  # type: ignore[type-arg]
    cfg: Settings = Depends(get_settings),
) -> PitchAccepted:
    # R26: validate BEFORE coin check
    # R22–R25: clean and validate
    title, description = _validate_and_clean(body.title, body.description)

    # --- Pitch Coin gate (R4, R5) ---
    rate_key = REDIS_PITCH_RATE.format(author_id=author_id)
    current = await redis.get(rate_key)
    coin_limit = getattr(cfg, "PITCH_COIN_LIMIT", DEFAULT_PITCH_COIN_LIMIT)

    if current is not None and int(current) >= coin_limit:
        # Compute next UTC midnight (R5)
        now_utc = datetime.now(timezone.utc)
        tomorrow = now_utc.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # If we're already past midnight today, go to next day
        from datetime import timedelta

        if tomorrow <= now_utc:
            tomorrow += timedelta(days=1)
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

    # R2: write pending record BEFORE LPUSH
    pending_key = REDIS_PENDING_PITCH.format(
        author_id=author_id, feature_id=feature_id
    )
    pending_ttl = getattr(cfg, "PENDING_PITCH_TTL_SECONDS", DEFAULT_PENDING_PITCH_TTL_SECONDS)
    pending_record = json.dumps(
        {
            "feature_id": feature_id,
            "title": title,
            "state": "screening",
            "submitted_at": submitted_at,
        }
    )
    await redis.set(pending_key, pending_record, ex=pending_ttl)

    # R28: intake envelope with exactly five keys
    envelope = json.dumps(
        {
            "feature_id": feature_id,
            "author_id": author_id,
            "title": title,
            "description": description,
            "submitted_at": submitted_at,
        }
    )
    await redis.lpush(REDIS_FEATURE_INTAKE, envelope)

    # Increment coin counter (R5: per UTC calendar day)
    pipe = redis.pipeline()
    pipe.incr(rate_key)
    # Compute seconds until next UTC midnight for EXPIRE
    now_utc = datetime.now(timezone.utc)
    next_midnight = now_utc.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    from datetime import timedelta

    if next_midnight <= now_utc:
        next_midnight += timedelta(days=1)
    ttl_seconds = int((next_midnight - now_utc).total_seconds()) + 1
    pipe.expire(rate_key, ttl_seconds)
    await pipe.execute()

    return PitchAccepted(feature_id=feature_id, state="screening")


# ---------------------------------------------------------------------------
# GET /api/features/mine  (US-06)  — R20: declared BEFORE {feature_id}
# ---------------------------------------------------------------------------


@router.get(
    "/mine",
    response_model=MyPitchesResponse,
)
async def list_my_pitches(
    author_id: str = Depends(get_current_user_id),
    redis: aioredis.Redis = Depends(get_redis),  # type: ignore[type-arg]
    supabase: AsyncClient = Depends(get_supabase),
    viewer_id: str | None = Depends(get_optional_user_id),
) -> MyPitchesResponse:
    # R15: scan only this author's prefix; R16: use SCAN not KEYS
    prefix = f"pending_pitch:{author_id}:*"
    pending_records: list[PendingPitch] = []
    cursor_val: int | bytes = 0
    while True:
        cursor_val, keys = await redis.scan(cursor=cursor_val, match=prefix, count=100)
        if keys:
            values = await redis.mget(keys)
            for val in values:
                if val is not None:
                    data = json.loads(val)
                    pending_records.append(
                        PendingPitch(
                            feature_id=data["feature_id"],
                            title=data["title"],
                            state=data.get("state", "screening"),
                            reason=data.get("reason"),
                            shipped_version=data.get("shipped_version"),
                            merged_into_feature_id=data.get("merged_into_feature_id"),
                            merged_into_title=data.get("merged_into_title"),
                            submitted_at=data["submitted_at"],
                        )
                    )
        if cursor_val == 0:
            break

    # R18: read Postgres rows for this author
    resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select(_FEATURE_COLUMNS)
        .eq("author_id", author_id)
        .order("created_at", desc=True)
        .execute()
    )
    feature_rows = resp.data or []
    feature_ids = [str(r["id"]) for r in feature_rows]

    # R36, R37, R38: shipped meta and voter info
    shipped_map = await _fetch_shipped_meta(supabase, feature_ids)
    voted_ids = await _fetch_voted_ids(supabase, feature_ids, viewer_id)

    # Fetch children for any rows that might be SPLIT parents (R30, R32, R38)
    children_map = await _fetch_children(supabase, feature_ids)

    # Also need shipped meta and voted ids for children
    all_child_ids = [
        str(c["id"]) for cs in children_map.values() for c in cs
    ]
    if all_child_ids:
        child_shipped = await _fetch_shipped_meta(supabase, all_child_ids)
        shipped_map.update(child_shipped)
        child_voted = await _fetch_voted_ids(supabase, all_child_ids, viewer_id)
        voted_ids.update(child_voted)

    features_out = [
        _row_to_feature(
            r,
            shipped_map=shipped_map,
            voted_ids=voted_ids,
            children_map=children_map,
        )
        for r in feature_rows
    ]

    # R17: filter out pending entries whose feature_id already in features
    persisted_ids = {f.id for f in features_out}
    pending_filtered = [p for p in pending_records if p.feature_id not in persisted_ids]

    return MyPitchesResponse(pending=pending_filtered, features=features_out)


# ---------------------------------------------------------------------------
# GET /api/features/{feature_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{feature_id}",
    response_model=FeatureOut,
    responses={404: {"model": ErrorResponse}},
)
async def get_feature(
    feature_id: str,
    supabase: AsyncClient = Depends(get_supabase),
    viewer_id: str | None = Depends(get_optional_user_id),
) -> FeatureOut:
    resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select(_FEATURE_COLUMNS)
        .eq("id", feature_id)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise_error(404, "not_found", "Feature not found")

    row = rows[0]
    rid = str(row["id"])

    # Shipped meta (R36, R38)
    shipped_map = await _fetch_shipped_meta(supabase, [rid])
    # Voted (R37, R38)
    voted_ids = await _fetch_voted_ids(supabase, [rid], viewer_id)

    # Children (R30, R31, R33)
    children_map = await _fetch_children(supabase, [rid])
    all_child_ids = [str(c["id"]) for cs in children_map.values() for c in cs]
    if all_child_ids:
        child_shipped = await _fetch_shipped_meta(supabase, all_child_ids)
        shipped_map.update(child_shipped)
        child_voted = await _fetch_voted_ids(supabase, all_child_ids, viewer_id)
        voted_ids.update(child_voted)

    return _row_to_feature(
        row,
        shipped_map=shipped_map,
        voted_ids=voted_ids,
        children_map=children_map,
    )


# ---------------------------------------------------------------------------
# GET /api/features  (US-05)
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=FeatureListResponse,
)
async def list_features(
    view: str = Query(...),
    sort: str = Query("top"),
    q: str | None = Query(None),
    status: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    supabase: AsyncClient = Depends(get_supabase),
    viewer_id: str | None = Depends(get_optional_user_id),
) -> FeatureListResponse:
    # Validate view
    if view not in _VIEW_STATUSES:
        raise_error(400, "validation_failed", f"view must be one of: {', '.join(_VIEW_STATUSES)}")

    # Validate sort
    if sort not in ("top", "new"):
        raise_error(400, "validation_failed", "sort must be 'top' or 'new'")

    # R12: status filter only valid for pipeline view
    statuses = _VIEW_STATUSES[view]
    if status is not None:
        if view != "pipeline":
            raise_error(400, "validation_failed", "status filter is only valid for the pipeline view")
        requested = [s.strip() for s in status.split(",") if s.strip()]
        for s in requested:
            if s not in _PIPELINE_STATUSES:
                raise_error(400, "validation_failed", f"Unknown status value: use one of {', '.join(sorted(_PIPELINE_STATUSES))}")
        statuses = requested

    # Build query — R29: root rows only (parent_id is null)
    query = (
        supabase.table(TABLE_FEATURE_REQUESTS)
        .select(_FEATURE_COLUMNS)
        .is_("parent_id", "null")
        .in_("status", statuses)
    )

    # Vault search (q parameter)
    if q and view == "vault":
        query = query.or_(f"title.ilike.%{q}%,description.ilike.%{q}%")

    # R10: keyset pagination
    # We fetch limit+1 to know if there's a next page
    fetch_limit = limit + 1

    if sort == "new":
        if cursor:
            cur = _decode_cursor(cursor)
            # For "new" sort (created_at desc), next page = rows with
            # created_at < cursor.created_at, or same created_at but id < cursor.id
            query = query.or_(
                f"created_at.lt.{cur['created_at']},"
                f"and(created_at.eq.{cur['created_at']},id.lt.{cur['id']})"
            )
        query = query.order("created_at", desc=True).order("id", desc=True)
    else:  # top
        if cursor:
            cur = _decode_cursor(cursor)
            # For "top" sort (upvotes desc), next page = rows with
            # upvotes < cursor.upvotes, or same upvotes but id < cursor.id
            query = query.or_(
                f"upvotes.lt.{cur['upvotes']},"
                f"and(upvotes.eq.{cur['upvotes']},id.lt.{cur['id']})"
            )
        query = query.order("upvotes", desc=True).order("id", desc=True)

    query = query.limit(fetch_limit)
    resp = await query.execute()
    rows = resp.data or []

    has_next = len(rows) > limit
    if has_next:
        rows = rows[:limit]

    if not rows:
        return FeatureListResponse(features=[], next_cursor=None)

    feature_ids = [str(r["id"]) for r in rows]

    # R32: batch-fetch children for all parents on this page
    children_map = await _fetch_children(supabase, feature_ids)

    # Collect all ids (parents + children) for shipped meta and votes
    all_child_ids = [str(c["id"]) for cs in children_map.values() for c in cs]
    all_ids = feature_ids + all_child_ids

    # R36: shipped meta
    shipped_map = await _fetch_shipped_meta(supabase, all_ids)

    # R37: viewer votes
    voted_ids = await _fetch_voted_ids(supabase, all_ids, viewer_id)

    features_out = [
        _row_to_feature(
            r,
            shipped_map=shipped_map,
            voted_ids=voted_ids,
            children_map=children_map,
        )
        for r in rows
    ]

    next_cursor: str | None = None
    if has_next:
        last_row = rows[-1]
        next_cursor = _encode_cursor(sort, last_row)

    return FeatureListResponse(features=features_out, next_cursor=next_cursor)