"""Contract tests for the deploy endpoints (US-10).

`GET /api/sandbox` tells the board where to point its iframe.
`POST /webhooks/render` is the only path by which a feature legitimately ships.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from shared.constants import FeatureStatus

TABLE_DEPLOYMENTS = "deployments"
TABLE_FEATURES = "feature_requests"

SECRET = "test-webhook-secret"
GOOD_URL = "https://streaks-demo.onrender.com"
BAD_URL = "https://evil.example.com"

FID = "aaaa1111-1111-4111-8111-111111111111"



@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    """Hermetic settings for both paths.

    Without this the webhook compares against whatever RENDER_WEBHOOK_SECRET
    the developer's .env happens to hold, and the allowlist tests depend on
    their local SANDBOX_ALLOWED_HOSTS.
    """
    from types import SimpleNamespace

    from backend import deps
    from backend.main import app

    stub = SimpleNamespace(
        RENDER_WEBHOOK_SECRET=SimpleNamespace(get_secret_value=lambda: SECRET),
        SANDBOX_ALLOWED_HOSTS=("*.onrender.com",),
        SANDBOX_URL="https://streaks-demo.onrender.com",
        DEV_MODE=True,
    )
    app.dependency_overrides[deps.get_settings] = lambda: stub
    yield
    app.dependency_overrides.pop(deps.get_settings, None)


def deploy_payload(**over: Any) -> dict[str, Any]:
    body = {
        "event": "deploy_live",
        "render_deploy_id": "dep-1",
        "version": "v0.4.2",
        "preview_url": GOOD_URL,
        "feature_ids": [FID],
    }
    body.update(over)
    return body


def in_sprint_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": FID,
        "title": "Streak counter",
        "description": "d",
        "status": FeatureStatus.IN_SPRINT.value,
        "upvotes": 9,
        "author_id": "11111111-1111-4111-8111-111111111111",
        "parent_id": None,
        "created_at": "2026-07-28T00:00:00Z",
    }
    row.update(over)
    return row


def post(client, body: dict[str, Any], secret: str | None = SECRET):
    headers = {"X-Webhook-Secret": secret} if secret is not None else {}
    return client.post("/webhooks/render", json=body, headers=headers)


# ===========================================================================
# R1 / R2 — the webhook is not open to the world
# ===========================================================================


def test_r1_no_secret_is_refused(make_client, fake_supabase) -> None:
    """This endpoint marks features shipped; an anonymous caller must not."""
    r = post(make_client(), deploy_payload(), secret=None)
    assert r.status_code == 401


def test_r1_a_wrong_secret_is_refused(make_client, fake_supabase) -> None:
    r = post(make_client(), deploy_payload(), secret="not-the-secret")
    assert r.status_code == 401


def test_r1_nothing_is_written_on_a_refused_call(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURES] = [in_sprint_row()]
    post(make_client(), deploy_payload(), secret="wrong")
    assert fake_supabase.rows[TABLE_FEATURES][0]["status"] == FeatureStatus.IN_SPRINT.value


# ===========================================================================
# R3 / R4 — the preview URL becomes an iframe src on the public board
# ===========================================================================


def test_r3_a_url_outside_the_allowlist_is_refused(make_client, fake_supabase) -> None:
    """An unchecked host here is a stored injection aimed at every visitor."""
    fake_supabase.rows[TABLE_FEATURES] = [in_sprint_row()]
    r = post(make_client(), deploy_payload(preview_url=BAD_URL))
    assert r.status_code == 400
    assert fake_supabase.rows[TABLE_FEATURES][0]["status"] == FeatureStatus.IN_SPRINT.value


def test_r4_a_live_deploy_without_a_url_is_refused(make_client, fake_supabase) -> None:
    body = deploy_payload()
    del body["preview_url"]
    assert post(make_client(), body).status_code == 400


# ===========================================================================
# R5 / R6 / R7 — what a live deploy does
# ===========================================================================


def test_r5_a_deployments_row_is_written(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURES] = [in_sprint_row()]
    assert post(make_client(), deploy_payload()).status_code == 204
    rows = fake_supabase.rows.get(TABLE_DEPLOYMENTS, [])
    assert len(rows) == 1
    row = rows[0]
    assert row["version"] == "v0.4.2"
    assert row["preview_url"] == GOOD_URL
    shipped = row["shipped_feature_ids"]
    assert FID in (shipped if isinstance(shipped, list) else json.loads(shipped))


def test_r6_the_feature_ships(make_client, fake_supabase) -> None:
    """This is the transition US-10 means by 'on successful deploy'."""
    fake_supabase.rows[TABLE_FEATURES] = [in_sprint_row()]
    post(make_client(), deploy_payload())
    assert fake_supabase.rows[TABLE_FEATURES][0]["status"] == FeatureStatus.COMPILED.value


@pytest.mark.parametrize(
    "status",
    [FeatureStatus.ARCHIVED.value, FeatureStatus.VOTING.value, FeatureStatus.COMPILED.value],
)
def test_r7_only_an_in_sprint_feature_can_ship(make_client, fake_supabase, status) -> None:
    """A replayed id must not resurrect an archived feature or re-ship a live one."""
    fake_supabase.rows[TABLE_FEATURES] = [in_sprint_row(status=status)]
    post(make_client(), deploy_payload())
    assert fake_supabase.rows[TABLE_FEATURES][0]["status"] == status


# ===========================================================================
# R8 — platforms retry
# ===========================================================================


def test_r8_a_redelivered_webhook_inserts_once(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURES] = [in_sprint_row()]
    first = post(make_client(), deploy_payload())
    second = post(make_client(), deploy_payload())
    assert first.status_code == 204 and second.status_code == 204
    assert len(fake_supabase.rows.get(TABLE_DEPLOYMENTS, [])) == 1, "the deploy was double-counted"


# ===========================================================================
# R9 — a failed deploy is not a shipped feature
# ===========================================================================


def test_r9_a_failed_deploy_returns_the_feature_to_voting(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURES] = [in_sprint_row()]
    r = post(make_client(), deploy_payload(event="deploy_failed", preview_url=None))
    assert r.status_code == 204
    assert fake_supabase.rows[TABLE_FEATURES][0]["status"] == FeatureStatus.VOTING.value


def test_r9_a_failed_deploy_records_no_deployment(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURES] = [in_sprint_row()]
    post(make_client(), deploy_payload(event="deploy_failed", preview_url=None))
    assert fake_supabase.rows.get(TABLE_DEPLOYMENTS, []) == []


# ===========================================================================
# R12 / R13 / R14 / R15 — what the board is told
# ===========================================================================


def test_r12_a_live_deployment_is_reported(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_DEPLOYMENTS] = [
        {
            "id": "d-1", "version": "v0.4.2", "render_deploy_id": "dep-1",
            "preview_url": GOOD_URL, "shipped_feature_ids": [FID],
            "created_at": "2026-07-28T00:00:00Z",
        }
    ]
    body = make_client().get("/api/sandbox").json()
    assert body["status"] == "live"
    assert body["preview_url"] == GOOD_URL
    assert body["version"] == "v0.4.2"


def test_r13_a_fresh_install_falls_back_to_the_bootstrap_url(make_client, fake_supabase) -> None:
    """Without this a brand-new install shows an empty frame."""
    body = make_client().get("/api/sandbox").json()
    assert body["status"] == "none"
    assert "preview_url" in body


def test_r14_a_stored_url_is_revalidated_on_read(make_client, fake_supabase) -> None:
    """The allowlist can be tightened after a row was written."""
    fake_supabase.rows[TABLE_DEPLOYMENTS] = [
        {
            "id": "d-1", "version": "v9", "render_deploy_id": "dep-9",
            "preview_url": BAD_URL, "shipped_feature_ids": [],
            "created_at": "2026-07-28T00:00:00Z",
        }
    ]
    body = make_client().get("/api/sandbox").json()
    assert body["status"] == "none", "a now-disallowed host was served to the board"
    assert body.get("preview_url") != BAD_URL


def test_r17_no_feature_detail_leaks_from_either_endpoint(make_client, fake_supabase) -> None:
    """The sandbox response is about a build, not about features."""
    fake_supabase.rows[TABLE_FEATURES] = [in_sprint_row(title="Blockchain check-ins")]
    fake_supabase.rows[TABLE_DEPLOYMENTS] = [
        {
            "id": "d-1", "version": "v1", "render_deploy_id": "dep-1",
            "preview_url": GOOD_URL, "shipped_feature_ids": [FID],
            "created_at": "2026-07-28T00:00:00Z",
        }
    ]
    blob = json.dumps(make_client().get("/api/sandbox").json())
    assert "Blockchain" not in blob
    assert "author_id" not in blob


# ===========================================================================
# R16 — every visitor loads this
# ===========================================================================


def test_r16_the_sandbox_read_is_anonymous(make_client, fake_supabase) -> None:
    """Board reads are anonymous; the preview is part of the board."""
    r = make_client(user_id=None).get("/api/sandbox")
    assert r.status_code == 200


# ===========================================================================
# R10a / R10b — the deploy announces itself (US-11 chain)
# ===========================================================================


def _spy_publish(fake_redis) -> list[tuple[str, str]]:
    """Record what reaches Redis. `fake_redis` is a real fakeredis instance, so
    there is no capture list on it — wrap the method instead."""
    seen: list[tuple[str, str]] = []
    original = fake_redis.publish

    async def _rec(channel, payload):
        seen.append((str(channel), str(payload)))
        return await original(channel, payload)

    fake_redis.publish = _rec  # type: ignore[method-assign]
    return seen


def test_r10a_a_live_deploy_publishes_the_ticker_event(make_client, fake_supabase, fake_redis) -> None:
    """Without this the refresh-preview pulse waits for an event that never arrives.

    Chain: agent_events -> event_relay -> broadcast_events -> Realtime ->
    chyron -> success phase -> pulse.
    """
    seen = _spy_publish(fake_redis)
    fake_supabase.rows[TABLE_FEATURES] = [in_sprint_row()]
    post(make_client(), deploy_payload())
    assert seen, "the deploy left the ticker silent"
    channel, payload = seen[0]
    assert channel == "agent_events"
    # event_relay maps on this exact phase string.
    assert json.loads(payload)["phase"] == "deployed"


def test_r10a_the_ticker_line_carries_no_feature_detail(make_client, fake_supabase, fake_redis) -> None:
    """The ticker is public and micro-copy only."""
    seen = _spy_publish(fake_redis)
    fake_supabase.rows[TABLE_FEATURES] = [in_sprint_row(title="Blockchain check-ins")]
    post(make_client(), deploy_payload())
    blob = " ".join(p for _c, p in seen)
    assert "Blockchain" not in blob
    assert FID not in blob


def test_r10b_a_redis_outage_does_not_fail_the_webhook(make_client, fake_supabase, fake_redis) -> None:
    """The platform must not retry a deploy that was already recorded."""

    async def _boom(*_a, **_k):
        raise RuntimeError("redis unreachable")

    fake_redis.publish = _boom  # type: ignore[method-assign]
    fake_supabase.rows[TABLE_FEATURES] = [in_sprint_row()]
    r = post(make_client(), deploy_payload())
    assert r.status_code == 204
    assert fake_supabase.rows[TABLE_FEATURES][0]["status"] == FeatureStatus.COMPILED.value
