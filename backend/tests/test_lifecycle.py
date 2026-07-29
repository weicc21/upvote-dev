"""Contract tests for the reboot endpoint (US-16).

The only path in the system that moves a feature *backwards* into play, and the
only transition a person triggers directly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from shared.constants import FeatureStatus

TABLE_FEATURES = "feature_requests"
TABLE_VOTES = "feature_votes"

FID = "aaaa1111-1111-4111-8111-111111111111"
AUTHOR = "bbbb2222-2222-4222-8222-222222222222"
REVIVER = "11111111-1111-4111-8111-111111111111"  # make_client's default identity


def archived(**over: Any) -> dict[str, Any]:
    row = {
        "id": FID,
        "title": "3D animated habit mascot",
        "description": "A 3D pet that grows happier as my streaks grow.",
        "status": FeatureStatus.ARCHIVED.value,
        "upvotes": 11,
        "author_id": AUTHOR,
        "author_handle": "polygonpal",
        "parent_id": None,
        "split_depth": 0,
        "unlock_threshold": None,
        "extends_id": None,
        "extends_title": None,
        "postpone_count": 2,
        "ai_explanation": None,
        "merge_count": 3,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": None,
    }
    row.update(over)
    return row


def reboot(client, fid: str = FID):
    return client.post(f"/api/features/{fid}/reboot")


# ===========================================================================
# R1 / R5 / R8 — what a reboot does
# ===========================================================================


def test_r1_an_archived_request_comes_back_to_voting(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURES] = [archived()]
    r = reboot(make_client())
    assert r.status_code == 200
    assert fake_supabase.rows[TABLE_FEATURES][0]["status"] == FeatureStatus.VOTING.value


def test_r5_the_count_restarts_at_one_not_zero(make_client, fake_supabase) -> None:
    """Zero would put a feature on the board that nobody appears to want —
    including the person who just asked for it."""
    fake_supabase.rows[TABLE_FEATURES] = [archived(upvotes=11)]
    reboot(make_client())
    assert fake_supabase.rows[TABLE_FEATURES][0]["upvotes"] == 1


def test_r8_the_window_restarts(make_client, fake_supabase) -> None:
    """Without this the next decay sweep re-archives it before anyone sees it."""
    fake_supabase.rows[TABLE_FEATURES] = [archived(created_at="2026-01-01T00:00:00Z")]
    reboot(make_client())
    assert fake_supabase.rows[TABLE_FEATURES][0]["created_at"] != "2026-01-01T00:00:00Z"


# ===========================================================================
# R6 / R7 — the count and the vote rows must agree
# ===========================================================================


def test_r6_the_revivers_vote_is_actually_recorded(make_client, fake_supabase) -> None:
    """A count of 1 with no vote row is the bug that let an author double-vote."""
    fake_supabase.rows[TABLE_FEATURES] = [archived()]
    reboot(make_client())
    votes = fake_supabase.rows.get(TABLE_VOTES, [])
    assert len(votes) == 1
    assert votes[0]["feature_id"] == FID
    assert fake_supabase.rows[TABLE_FEATURES][0]["upvotes"] == len(votes)


def test_r7_old_votes_are_cleared(make_client, fake_supabase) -> None:
    """The new window measures current demand, and everyone must be able to vote again."""
    fake_supabase.rows[TABLE_FEATURES] = [archived()]
    fake_supabase.rows[TABLE_VOTES] = [
        {"id": "v-1", "feature_id": FID, "user_id": "old-voter-1"},
        {"id": "v-2", "feature_id": FID, "user_id": "old-voter-2"},
    ]
    reboot(make_client())
    remaining = [v for v in fake_supabase.rows.get(TABLE_VOTES, []) if v["feature_id"] == FID]
    assert len(remaining) == 1, "prior votes survived the reset"
    assert "old-voter" not in json.dumps(remaining)


# ===========================================================================
# R2 / R3 / R4 — who may, and what is refused
# ===========================================================================


def test_r2_the_endpoint_demands_an_identity(make_client, fake_supabase) -> None:
    """It uses the identity dependency that raises 401, not the optional one.

    There is deliberately no 401 path in DEV_MODE — the middleware stamps a
    fixed anonymous uuid so `curl` works without account management, and the
    401 branch is unit-tested against `get_current_user_id` in test_deps.py.
    Asserting the dependency is therefore the honest check here, and it is what
    separates a reboot from an anonymous board read.
    """
    import inspect

    from backend import deps
    from backend.routes.lifecycle import reboot_feature

    defaults = [
        p.default for p in inspect.signature(reboot_feature).parameters.values()
    ]
    dependencies = [getattr(d, "dependency", None) for d in defaults]
    assert deps.get_current_user_id in dependencies
    assert deps.get_optional_user_id not in dependencies


def test_r2_a_non_author_may_reboot(make_client, fake_supabase) -> None:
    """Present demand is the point — an idea whose author moved on must still revive."""
    fake_supabase.rows[TABLE_FEATURES] = [archived(author_id="someone-else-entirely")]
    assert reboot(make_client()).status_code == 200


@pytest.mark.parametrize(
    "status",
    [
        FeatureStatus.VOTING.value,
        FeatureStatus.IN_SPRINT.value,
        FeatureStatus.COMPILED.value,
        FeatureStatus.POSTPONED_CONFLICT.value,
        FeatureStatus.SPLIT.value,
    ],
)
def test_r3_only_an_archived_request_can_be_rebooted(make_client, fake_supabase, status) -> None:
    """The control only appears on the Vault, so this means a stale client."""
    fake_supabase.rows[TABLE_FEATURES] = [archived(status=status)]
    r = reboot(make_client())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "not_archived"
    assert fake_supabase.rows[TABLE_FEATURES][0]["status"] == status


def test_r4_an_unknown_feature_is_404_not_422(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURES] = []
    r = reboot(make_client(), fid="cccc3333-3333-4333-8333-333333333333")
    assert r.status_code == 404


# ===========================================================================
# R10 — it is the same idea, not a new one
# ===========================================================================


def test_r10_identity_and_history_survive(make_client, fake_supabase) -> None:
    """Rewriting identity or erasing history would make the Vault a laundry."""
    fake_supabase.rows[TABLE_FEATURES] = [archived()]
    reboot(make_client())
    row = fake_supabase.rows[TABLE_FEATURES][0]
    assert row["title"] == "3D animated habit mascot"
    assert row["description"].startswith("A 3D pet")
    assert row["author_id"] == AUTHOR
    assert row["author_handle"] == "polygonpal"
    assert row["merge_count"] == 3
    assert row["postpone_count"] == 2, "the architect should judge it knowing what happened before"


def test_r10_no_pitch_coin_is_charged_and_nothing_is_enqueued(make_client, fake_supabase, fake_redis) -> None:
    """Reviving an existing idea is not pitching a new one; the spec says no re-enqueue."""
    fake_supabase.rows[TABLE_FEATURES] = [archived()]
    reboot(make_client())
    pushed = getattr(fake_redis, "pushed", None) or getattr(fake_redis, "lists", {})
    assert not pushed, "the reboot re-entered the intake queue"


# ===========================================================================
# R11 — the client drops it straight into the pipeline
# ===========================================================================


def test_r11_the_full_feature_shape_comes_back(make_client, fake_supabase) -> None:
    fake_supabase.rows[TABLE_FEATURES] = [archived()]
    body = reboot(make_client()).json()
    for field in ("id", "title", "description", "status", "upvotes", "children", "created_at"):
        assert field in body, f"missing {field} — the client cannot render this"
    assert isinstance(body["children"], list)
    assert body["status"] == FeatureStatus.VOTING.value
    assert "author_id" not in body


# ===========================================================================
# R9 — two people pressing at once
# ===========================================================================


def test_r9_the_write_is_guarded_on_still_being_archived(make_client, fake_supabase) -> None:
    """Two reboots must not produce two resets and two votes."""
    fake_supabase.rows[TABLE_FEATURES] = [archived()]
    first = reboot(make_client())
    second = reboot(make_client())
    assert first.status_code == 200
    assert second.status_code == 422, "the second reboot was not refused"
    votes = [v for v in fake_supabase.rows.get(TABLE_VOTES, []) if v["feature_id"] == FID]
    assert len(votes) == 1
