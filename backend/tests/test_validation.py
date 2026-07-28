"""Deterministic input validation at the pitch gate — R22-R27 of features_python (US-01).

Every invisible codepoint below is written as an escape, never a literal, so the
file stays reviewable in a terminal and a diff.
"""

from __future__ import annotations

import json

import pytest

from shared.constants import REDIS_FEATURE_INTAKE

USER = "11111111-1111-4111-8111-111111111111"

GOOD_TITLE = "Dark mode for the dashboard"
GOOD_DESC = "Add a persisted dark theme toggle in the header that survives a reload."

ZWSP = "\u200b"    # zero-width space
ZWJ = "\u200d"     # zero-width joiner
RLO = "\u202e"     # right-to-left override — can visually reverse text
BELL = "\u0007"    # Cc control
NULLC = "\u0000"   # NUL — a literal here makes the file uncompilable


def _envelope_ok(body: dict) -> bool:
    return "error" in body and {"code", "message"} <= set(body["error"])


async def _queued_payload(fake_redis, fid: str) -> dict:
    for q in await fake_redis.lrange(REDIS_FEATURE_INTAKE, 0, -1):
        if fid in q:
            return json.loads(q)
    raise AssertionError("pitch never reached the intake queue")


# ==========================================================================
# R22 — control characters are stripped
# ==========================================================================

async def test_r22_invisible_codepoints_never_reach_the_queue(make_client, fake_redis) -> None:
    r = make_client().post("/api/features", json={
        "title": f"Dark{ZWSP} mode{ZWJ} toggle",
        "description": f"Add a{RLO} persisted dark theme toggle in the header that survives a reload.",
    })
    assert r.status_code == 202, r.text
    payload = await _queued_payload(fake_redis, r.json()["feature_id"])
    for ch in (ZWSP, ZWJ, RLO, BELL, NULLC):
        assert ch not in payload["title"], f"{ch!r} survived into the queue"
        assert ch not in payload["description"], f"{ch!r} survived into the queue"


async def test_r22_cc_controls_are_stripped(make_client, fake_redis) -> None:
    r = make_client().post("/api/features", json={
        "title": f"Dark{BELL} mode",
        "description": f"Add a persisted{NULLC} dark theme toggle in the header that survives a reload.",
    })
    assert r.status_code == 202, r.text
    payload = await _queued_payload(fake_redis, r.json()["feature_id"])
    assert BELL not in payload["title"] and NULLC not in payload["description"]


async def test_r22_title_is_single_line(make_client, fake_redis) -> None:
    r = make_client().post("/api/features", json={
        "title": "Dark\nmode\ttoggle", "description": GOOD_DESC})
    assert r.status_code == 202, r.text
    payload = await _queued_payload(fake_redis, r.json()["feature_id"])
    assert "\n" not in payload["title"] and "\t" not in payload["title"]


# ==========================================================================
# R23 — markup is refused, not sanitised
# ==========================================================================

@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "<script>alert(1)</script>"),
        ("title", "Dark mode <b>now</b>"),
        ("title", "</div> dark mode"),
        ("description", "<script>fetch('/steal')</script> plus padding to clear the length floor."),
        ("description", "Add a <iframe src=evil></iframe> dark theme toggle to the header area now."),
        ("description", "&lt;script&gt;alert(1)&lt;/script&gt; padded out to clear the length floor."),
    ],
)
async def test_r23_markup_is_rejected(make_client, field: str, value: str) -> None:
    body = {"title": GOOD_TITLE, "description": GOOD_DESC}
    body[field] = value
    r = make_client().post("/api/features", json=body)
    assert r.status_code == 400, f"markup in {field} should be refused: {r.text}"
    assert _envelope_ok(r.json())


async def test_r23_bare_angle_brackets_still_pass(make_client) -> None:
    """`width < 300px` is a comparison, not markup — refusing it would be wrong."""
    r = make_client().post("/api/features", json={
        "title": "Warn when width < 300px",
        "description": "Show a hint when the viewport width < 300px so the layout switch is obvious.",
    })
    assert r.status_code == 202, r.text


# ==========================================================================
# R24 — bounds apply to the cleaned text
# ==========================================================================

async def test_r24_zero_width_padding_does_not_satisfy_the_minimum(make_client) -> None:
    r = make_client().post("/api/features", json={
        "title": "Padded", "description": "too short" + (ZWSP * 40)})
    assert r.status_code == 400, "zero-width padding must not count toward the 30-char floor"


async def test_r24_genuine_content_at_the_boundary_passes(make_client) -> None:
    r = make_client().post("/api/features", json={"title": "x", "description": "y" * 30})
    assert r.status_code == 202, r.text


# ==========================================================================
# R25 — one pitch, one version of the text
# ==========================================================================

async def test_r25_tray_and_queue_hold_the_same_cleaned_text(make_client, fake_redis) -> None:
    r = make_client().post("/api/features", json={
        "title": f"Dark{ZWSP} mode", "description": GOOD_DESC})
    fid = r.json()["feature_id"]
    pending = json.loads(await fake_redis.get(f"pending_pitch:{USER}:{fid}"))
    payload = await _queued_payload(fake_redis, fid)
    assert pending["title"] == payload["title"], "the tray and the queue disagree"
    assert ZWSP not in pending["title"]


# ==========================================================================
# R26 — a refused pitch keeps its coin
# ==========================================================================

@pytest.mark.parametrize(
    "body",
    [
        {"title": "<script>alert(1)</script>", "description": GOOD_DESC},
        {"title": "ok", "description": "nope"},
        {"title": "", "description": GOOD_DESC},
        {"title": "Padded", "description": "too short" + (ZWSP * 40)},
    ],
)
async def test_r26_a_refused_pitch_spends_no_coin(make_client, fake_redis, body: dict) -> None:
    before = await fake_redis.get(f"rate:pitch:{USER}")
    r = make_client().post("/api/features", json=body)
    assert r.status_code == 400, r.text
    assert await fake_redis.get(f"rate:pitch:{USER}") == before, "a refused pitch burned a coin"


async def test_r26_an_accepted_pitch_does_spend_one(make_client, fake_redis) -> None:
    before = await fake_redis.get(f"rate:pitch:{USER}")
    assert make_client().post(
        "/api/features", json={"title": GOOD_TITLE, "description": GOOD_DESC}
    ).status_code == 202
    after = await fake_redis.get(f"rate:pitch:{USER}")
    assert int(after or 0) == int(before or 0) + 1


# ==========================================================================
# R27 — the error never reflects the payload
# ==========================================================================

async def test_r27_offending_text_is_not_echoed(make_client) -> None:
    """Reflecting unscreened input into a response is how a payload reaches a browser."""
    r = make_client().post("/api/features", json={
        "title": "<script>alert('XSSMARKER')</script>", "description": GOOD_DESC})
    assert r.status_code == 400
    assert "XSSMARKER" not in r.text
    assert "<script>" not in r.text


async def test_r27_names_the_field_that_failed(make_client) -> None:
    """Refusing without saying which field leaves the author guessing."""
    r = make_client().post("/api/features", json={"title": GOOD_TITLE, "description": "nope"})
    assert r.status_code == 400
    assert "description" in r.text.lower()


# ==========================================================================
# R28 — the cross-process intake contract
# ==========================================================================

async def test_r28_intake_envelope_has_exactly_the_five_agreed_keys(make_client, fake_redis) -> None:
    """The API and the daemon never import each other, so nothing but a test
    holds this envelope together. Regenerating either side once dropped
    `submitted_at` and stranded every pitch in the queue as 'malformed'."""
    r = make_client().post("/api/features", json={"title": GOOD_TITLE, "description": GOOD_DESC})
    assert r.status_code == 202
    payload = await _queued_payload(fake_redis, r.json()["feature_id"])
    assert set(payload) == {"feature_id", "author_id", "title", "description", "submitted_at"}


async def test_r28_the_daemon_accepts_what_this_api_produces(make_client, fake_redis) -> None:
    """Pin both halves of the contract in one assertion, against the real consumer."""
    from orchestrator import ingestion_service as svc

    r = make_client().post("/api/features", json={"title": GOOD_TITLE, "description": GOOD_DESC})
    payload = await _queued_payload(fake_redis, r.json()["feature_id"])

    expected = next((getattr(svc, n) for n in ("INTAKE_KEYS", "_EXPECTED_KEYS", "_REQUIRED_KEYS")
                     if getattr(svc, n, None) is not None), None)
    assert expected is not None, "the daemon no longer declares its expected key set"
    assert set(payload) == set(expected), (
        f"producer/consumer drift: API writes {sorted(payload)}, "
        f"daemon expects {sorted(expected)}"
    )
