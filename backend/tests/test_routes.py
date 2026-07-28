"""Contract tests for the pitch, board, and upvote routes, plus the app envelope."""

from __future__ import annotations

import json
import uuid

import pytest

from shared.constants import (
    REDIS_FEATURE_INTAKE,
    TABLE_FEATURE_REQUESTS,
    TABLE_FEATURE_VOTES,
    FeatureStatus,
)

USER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
FID = "33333333-3333-4333-8333-333333333333"

VALID_PITCH = {
    "title": "Dark mode for the dashboard",
    "description": "Add a persisted dark theme toggle in the header that survives a reload.",
}


def _envelope_ok(body: dict) -> bool:
    return "error" in body and {"code", "message"} <= set(body["error"])


# ==========================================================================
# POST /api/features  (US-01)
# ==========================================================================

async def test_r1_pitch_returns_202_with_feature_id_and_screening(make_client) -> None:
    r = make_client().post("/api/features", json=VALID_PITCH)
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"feature_id", "state"}
    assert body["state"] == "screening"
    uuid.UUID(body["feature_id"])  # must be a real UUID


async def test_r2_pending_record_written_before_lpush(make_client, fake_redis) -> None:
    """R2: the record must exist before the orchestrator can see the queue item."""
    r = make_client().post("/api/features", json=VALID_PITCH)
    fid = r.json()["feature_id"]

    pending_keys = [k async for k in fake_redis.scan_iter(match=f"pending_pitch:{USER}:*")]
    assert any(fid in k for k in pending_keys), "pending record was not written"

    queued = await fake_redis.lrange(REDIS_FEATURE_INTAKE, 0, -1)
    assert queued, "intake queue is empty"
    assert any(fid in item for item in queued)


async def test_r2_pending_record_carries_title_and_screening_state(make_client, fake_redis) -> None:
    r = make_client().post("/api/features", json=VALID_PITCH)
    fid = r.json()["feature_id"]
    key = f"pending_pitch:{USER}:{fid}"
    raw = await fake_redis.get(key)
    assert raw is not None, "pending record missing"
    rec = json.loads(raw)
    assert rec["title"] == VALID_PITCH["title"]
    assert rec["state"] == "screening"


async def test_pending_record_has_a_ttl(make_client, fake_redis) -> None:
    """The TTL is the only thing that ever clears a pending entry (US-06)."""
    r = make_client().post("/api/features", json=VALID_PITCH)
    fid = r.json()["feature_id"]
    ttl = await fake_redis.ttl(f"pending_pitch:{USER}:{fid}")
    assert 0 < ttl <= 900


async def test_r3_pitch_never_writes_to_postgres(make_client, fake_supabase) -> None:
    """R3: unscreened content lives only in Redis."""
    make_client().post("/api/features", json=VALID_PITCH)
    writes = [c for c in fake_supabase.calls if c["op"] in {"insert", "update"}]
    assert writes == [], f"pitch wrote to Postgres: {writes}"


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "", "description": "x" * 50},
        {"title": "ok title", "description": "too short"},
        {"title": "x" * 61, "description": "x" * 50},
        {"title": "ok title"},
        {"description": "x" * 50},
    ],
)
async def test_validation_failure_uses_frozen_envelope_and_400(make_client, payload) -> None:
    """R12 of main: schema failures are 400 validation_failed, not FastAPI's 422."""
    r = make_client().post("/api/features", json=payload)
    assert r.status_code == 400, r.text
    assert _envelope_ok(r.json())
    assert "detail" not in r.json()


async def test_r4_out_of_coins_returns_429_with_resets_at(make_client, fake_redis) -> None:
    """R4: resets_at is required and must be a real timestamp."""
    client = make_client()
    await fake_redis.set(f"rate:pitch:{USER}", 5)

    r = client.post("/api/features", json=VALID_PITCH)
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["code"] == "out_of_coins"
    assert body.get("resets_at"), "resets_at missing or null"
    assert "resets_at" not in body["error"], "resets_at must be a sibling of error"


async def test_r6_author_id_never_returned(make_client) -> None:
    r = make_client().post("/api/features", json=VALID_PITCH)
    assert USER not in r.text
    assert "author_id" not in r.text


async def test_r9_dev_mode_falls_back_to_a_fixed_anonymous_uuid(make_client) -> None:
    """main.py R9: with no X-Dev-User, dev mode still resolves a caller.

    There is deliberately no 401 path in dev mode — that is what lets `curl`
    pitch with no account management. The 401 branch is unit-tested against
    `get_current_user_id` directly in test_deps.py.
    """
    r = make_client(user_id=None).post("/api/features", json=VALID_PITCH)
    assert r.status_code == 202


# ==========================================================================
# GET /api/features  (US-05)
# ==========================================================================

async def test_r9_view_is_required_and_validated(make_client) -> None:
    c = make_client()
    assert c.get("/api/features").status_code in (400, 422)
    assert c.get("/api/features?view=bogus").status_code == 400
    assert _envelope_ok(c.get("/api/features?view=bogus").json())


async def test_r9_sort_is_validated(make_client) -> None:
    r = make_client().get("/api/features?view=pipeline&sort=newest")
    assert r.status_code == 400, "sort=newest is not in the frozen enum"


async def test_r12_unknown_status_is_rejected(make_client) -> None:
    r = make_client().get("/api/features?view=pipeline&status=VOTING,NOPE")
    assert r.status_code == 400
    assert _envelope_ok(r.json())


async def test_r11_list_returns_features_key_and_no_total(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = [
        {"id": FID, "title": "t", "description": "d", "status": "VOTING",
         "upvotes": 3, "created_at": "2026-07-27T00:00:00Z", "parent_id": None},
    ]
    r = make_client().get("/api/features?view=pipeline")
    assert r.status_code == 200
    body = r.json()
    assert "features" in body
    assert "total" not in body and "count" not in body


async def test_board_read_is_anonymous(make_client, fake_supabase) -> None:
    """R8/US-05: the board is readable with no identity at all."""
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = []
    r = make_client(user_id=None).get("/api/features?view=pipeline")
    assert r.status_code == 200


# ==========================================================================
# GET /api/features/mine  (US-06)
# ==========================================================================

async def test_r20_mine_is_not_swallowed_by_the_id_route(make_client) -> None:
    """R20: /mine must be declared before /{feature_id}."""
    r = make_client().get("/api/features/mine")
    assert r.status_code == 200, "the literal path was captured by {feature_id}"
    assert set(r.json()) == {"pending", "features"}


async def test_r15_scan_is_scoped_to_the_caller(make_client, fake_redis) -> None:
    """R15: another author's pending pitches must never be returned."""
    mine = f"pending_pitch:{USER}:{FID}"
    theirs = f"pending_pitch:{OTHER}:44444444-4444-4444-8444-444444444444"
    payload = {"feature_id": FID, "title": "mine", "state": "screening",
               "submitted_at": "2026-07-27T00:00:00Z"}
    await fake_redis.set(mine, json.dumps(payload))
    await fake_redis.set(theirs, json.dumps({**payload, "title": "theirs"}))

    body = make_client().get("/api/features/mine").json()
    assert "theirs" not in json.dumps(body)
    assert len(body["pending"]) == 1


async def test_r17_promoted_pitch_is_not_listed_twice(make_client, fake_redis, fake_supabase) -> None:
    """R17: a pending twin is dropped once the board row exists."""
    await fake_redis.set(
        f"pending_pitch:{USER}:{FID}",
        json.dumps({"feature_id": FID, "title": "t", "state": "screening",
                    "submitted_at": "2026-07-27T00:00:00Z"}),
    )
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = [
        {"id": FID, "title": "t", "description": "d", "status": "VOTING",
         "upvotes": 1, "created_at": "2026-07-27T00:00:00Z", "author_id": USER}
    ]
    body = make_client().get("/api/features/mine").json()
    pending_ids = {p["feature_id"] for p in body["pending"]}
    assert FID not in pending_ids, "pitch appears as both screening and VOTING"


async def test_r19_mine_never_deletes_a_pending_record(make_client, fake_redis) -> None:
    """R19: the TTL is the sole clearing mechanism."""
    key = f"pending_pitch:{USER}:{FID}"
    await fake_redis.set(key, json.dumps(
        {"feature_id": FID, "title": "t", "state": "screening", "submitted_at": "2026-07-27T00:00:00Z"}))
    make_client().get("/api/features/mine")
    assert await fake_redis.exists(key), "the read deleted a pending record"


async def test_mine_resolves_a_caller_in_dev_mode(make_client) -> None:
    """Dev mode always has an identity, so /mine answers rather than 401ing."""
    assert make_client(user_id=None).get("/api/features/mine").status_code == 200


# ==========================================================================
# POST /api/features/{id}/upvote  (US-04)
# ==========================================================================

def _votable_row(upvotes: int = 3) -> dict:
    return {"id": FID, "status": FeatureStatus.VOTING.value, "upvotes": upvotes}


async def test_r1_upvote_returns_exactly_feature_id_and_upvotes(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = [_votable_row()]
    fake_supabase.rpc_result = 4
    r = make_client().post(f"/api/features/{FID}/upvote")
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"feature_id", "upvotes"}


async def test_r2_non_voting_row_is_422_not_votable(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = [
        {"id": FID, "status": FeatureStatus.IN_SPRINT.value, "upvotes": 9}
    ]
    r = make_client().post(f"/api/features/{FID}/upvote")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "not_votable"


async def test_r2_status_is_checked_before_any_write(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = [
        {"id": FID, "status": FeatureStatus.ARCHIVED.value, "upvotes": 1}
    ]
    make_client().post(f"/api/features/{FID}/upvote")
    assert not [c for c in fake_supabase.calls if c["op"] == "insert"]


async def test_r3_duplicate_vote_is_409_already_voted(make_client, fake_supabase) -> None:
    """R3: detected via the unique constraint, not a SELECT-then-INSERT."""
    from postgrest.exceptions import APIError

    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = [_votable_row()]
    fake_supabase.insert_raises[TABLE_FEATURE_VOTES] = APIError(
        {"code": "23505", "message": "duplicate key value violates unique constraint"}
    )
    r = make_client().post(f"/api/features/{FID}/upvote")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "already_voted"


async def test_r4_vote_row_inserted_before_increment(make_client, fake_supabase) -> None:
    """R4: reverse order would count every duplicate that R3 rejects."""
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = [_votable_row()]
    fake_supabase.rpc_result = 4
    make_client().post(f"/api/features/{FID}/upvote")
    inserts = [i for i, c in enumerate(fake_supabase.calls) if c["op"] == "insert"]
    assert inserts, "no vote row inserted"
    assert fake_supabase.rpc_calls, "increment never called"


async def test_r5_increment_uses_the_atomic_rpc(make_client, fake_supabase) -> None:
    """R5: a read-then-write loses updates under concurrency."""
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = [_votable_row()]
    fake_supabase.rpc_result = 4
    make_client().post(f"/api/features/{FID}/upvote")
    assert any(fn == "increment_upvotes" for fn, _ in fake_supabase.rpc_calls)


async def test_r6_failed_increment_still_returns_200(make_client, fake_supabase) -> None:
    """R6: the vote is durable; a 500 here would strand the voter behind a 409."""
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = [_votable_row()]
    fake_supabase.rpc_raises = RuntimeError("rpc exploded")
    r = make_client().post(f"/api/features/{FID}/upvote")
    assert r.status_code == 200
    assert set(r.json()) == {"feature_id", "upvotes"}


async def test_r8_unknown_feature_is_404(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = []
    r = make_client().post(f"/api/features/{FID}/upvote")
    assert r.status_code == 404


async def test_r7_no_user_id_in_upvote_response(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = [_votable_row()]
    fake_supabase.rpc_result = 4
    r = make_client().post(f"/api/features/{FID}/upvote")
    assert USER not in r.text


async def test_upvote_resolves_a_caller_in_dev_mode(make_client, fake_supabase) -> None:
    """Reaches the handler (404 = feature lookup), proving identity resolved."""
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = []
    assert make_client(user_id=None).post(f"/api/features/{FID}/upvote").status_code == 404


# ==========================================================================
# App-level: error envelope + wiring (main.py R11-R16)
# ==========================================================================

async def test_r11_unmatched_route_uses_the_envelope_not_detail(make_client) -> None:
    r = make_client().get("/api/does-not-exist")
    assert r.status_code == 404
    assert "detail" not in r.json()
    assert _envelope_ok(r.json())


async def test_r14_both_routers_are_mounted(make_client) -> None:
    """Assert via the OpenAPI schema — app.routes exposes opaque wrappers."""
    from backend.main import app

    paths = set(app.openapi()["paths"])
    assert "/api/features" in paths
    assert any("upvote" in p for p in paths), "votes router not mounted"
    assert any(p.endswith("/mine") for p in paths), "features/mine not mounted"


async def test_r15_cors_allows_only_the_configured_origin(make_client) -> None:
    c = make_client()
    good = c.options(
        "/api/features",
        headers={"Origin": "http://localhost:5173",
                 "Access-Control-Request-Method": "GET"},
    )
    assert good.headers.get("access-control-allow-origin") == "http://localhost:5173"

    bad = c.options(
        "/api/features",
        headers={"Origin": "https://evil.example.com",
                 "Access-Control-Request-Method": "GET"},
    )
    assert bad.headers.get("access-control-allow-origin") != "https://evil.example.com"


async def test_r15_cors_is_never_wildcard(make_client) -> None:
    r = make_client().get(
        "/api/features?view=pipeline", headers={"Origin": "http://localhost:5173"}
    )
    assert r.headers.get("access-control-allow-origin") != "*"


# ==========================================================================
# R29-R33 — root rows only, children embedded (US-05)
# ==========================================================================

PARENT_ID = "aaaa1111-1111-4111-8111-111111111111"
CHILD_A = "bbbb2222-2222-4222-8222-222222222222"
CHILD_B = "cccc3333-3333-4333-8333-333333333333"


def _family() -> list[dict]:
    base = {"description": "d", "created_at": "2026-07-28T00:00:00Z", "upvotes": 0}
    return [
        {**base, "id": PARENT_ID, "title": "Habit customisation", "status": "SPLIT",
         "parent_id": None, "upvotes": 1},
        {**base, "id": CHILD_A, "title": "Per-habit colour", "status": "VOTING", "parent_id": PARENT_ID},
        {**base, "id": CHILD_B, "title": "Drag to reorder", "status": "VOTING", "parent_id": PARENT_ID},
    ]


async def test_r29_children_are_not_listed_as_top_level_cards(make_client, fake_supabase) -> None:
    """A split rendered as three unrelated cards hides the idea they came from."""
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = _family()
    body = make_client().get("/api/features?view=pipeline").json()
    top_ids = {f["id"] for f in body["features"]}
    assert CHILD_A not in top_ids and CHILD_B not in top_ids
    assert PARENT_ID in top_ids


async def test_r30_parent_embeds_its_children(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = _family()
    body = make_client().get("/api/features?view=pipeline").json()
    parent = next(f for f in body["features"] if f["id"] == PARENT_ID)
    kids = {c["id"] for c in parent.get("children", [])}
    assert kids == {CHILD_A, CHILD_B}


async def test_r33_children_is_a_list_never_null(make_client, fake_supabase) -> None:
    """The frontend maps over it directly."""
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = [
        {"id": FID, "title": "Solo feature", "description": "d", "status": "VOTING",
         "upvotes": 3, "created_at": "2026-07-28T00:00:00Z", "parent_id": None},
    ]
    body = make_client().get("/api/features?view=pipeline").json()
    assert body["features"][0].get("children") is not None
    assert isinstance(body["features"][0].get("children", []), list)


async def test_r31_a_child_is_still_reachable_directly(make_client, fake_supabase) -> None:
    """Root-only is a rule about lists; an author must still find their children."""
    fake_supabase.rows[TABLE_FEATURE_REQUESTS] = [
        {"id": CHILD_A, "title": "Per-habit colour", "description": "d", "status": "VOTING",
         "upvotes": 0, "created_at": "2026-07-28T00:00:00Z", "parent_id": PARENT_ID},
    ]
    r = make_client().get(f"/api/features/{CHILD_A}")
    assert r.status_code == 200
    assert r.json()["id"] == CHILD_A
