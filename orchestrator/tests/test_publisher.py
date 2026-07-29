"""Contract tests for the publisher (US-10 automation).

The interesting problem is identity: "is the site live?" is always yes, because
the previous build serves throughout a new one. The fixtures below are shaped
like the real service history — five deploys sharing one commit sha, two of them
failed — which is what makes sha-matching alone insufficient.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from orchestrator import publisher as P
from orchestrator.publisher import await_deploy, post_webhook, publish, snapshot_deploy_ids

SHA = "e6eadf5f1234567890abcdef1234567890abcdef"
OLD_SHA = "1111111111111111111111111111111111111111"


def deploy(did: str, status: str, sha: str = SHA) -> dict[str, Any]:
    return {
        "deploy": {
            "id": did,
            "status": status,
            "commit": {"id": sha, "message": "compiled feature"},
            "createdAt": "2026-07-29T00:00:00Z",
            "finishedAt": None,
        }
    }


# The shape that breaks naive matching: one sha, many deploys, mixed outcomes.
HISTORY_BEFORE_PUSH = [
    deploy("dep-live-old", "live"),
    deploy("dep-deact", "deactivated"),
    deploy("dep-failed-1", "build_failed"),
    deploy("dep-failed-2", "build_failed"),
]


def getter(*pages: list[dict[str, Any]]):
    """Return successive API responses; the last repeats once exhausted."""
    calls: list[str] = []
    seq = list(pages)

    async def _get(url: str, headers: Any) -> tuple[int, str]:
        calls.append(url)
        page = seq.pop(0) if len(seq) > 1 else seq[0]
        return 200, json.dumps(page)

    _get.calls = calls  # type: ignore[attr-defined]
    return _get


def poster(status: int = 204):
    sent: list[dict[str, Any]] = []

    async def _post(url: str, headers: Any, body: str) -> tuple[int, str]:
        sent.append({"url": url, "headers": dict(headers), "body": json.loads(body)})
        return status, ""

    _post.sent = sent  # type: ignore[attr-defined]
    return _post


# ===========================================================================
# R1 / R2 / R3 — which deploy is ours
# ===========================================================================


@pytest.mark.asyncio
async def test_r2_the_snapshot_is_taken_before_the_push() -> None:
    ids = await snapshot_deploy_ids(get=getter(HISTORY_BEFORE_PUSH))
    assert ids == {"dep-live-old", "dep-deact", "dep-failed-1", "dep-failed-2"}


@pytest.mark.asyncio
async def test_r1_an_old_failure_sharing_our_sha_is_not_mistaken_for_ours() -> None:
    """The whole point: five deploys share one sha, two of them failed.

    Matching on commit alone would report a week-old build_failed as this
    build's outcome.
    """
    known = {d["deploy"]["id"] for d in HISTORY_BEFORE_PUSH}
    ours = deploy("dep-new", "live")
    outcome = await await_deploy(
        SHA, known, get=getter(HISTORY_BEFORE_PUSH + [ours]), timeout_s=5, interval_s=0
    )
    assert outcome.ok is True
    assert outcome.deploy_id == "dep-new", "it picked a pre-existing deploy"


@pytest.mark.asyncio
async def test_r1_a_new_deploy_for_a_different_commit_is_not_ours() -> None:
    """Someone else's push must not be reported as our build going live."""
    known = {d["deploy"]["id"] for d in HISTORY_BEFORE_PUSH}
    theirs = deploy("dep-someone-else", "live", sha=OLD_SHA)
    outcome = await await_deploy(
        SHA, known, get=getter(HISTORY_BEFORE_PUSH + [theirs]), timeout_s=1, interval_s=0
    )
    assert outcome.ok is False, "another commit's deploy was claimed as ours"


@pytest.mark.asyncio
async def test_r3_a_healthy_site_is_never_the_signal() -> None:
    """Nothing in this module may fetch the preview URL to decide anything."""
    import pathlib

    src = pathlib.Path(P.__file__).read_text()
    assert "SANDBOX_URL" in src, "the preview url is used as a payload field (R10)"
    # …but never fetched to infer status. Checked as whole words: "ping" also
    # appears inside "Polling" and "mapping", which is the substring trap this
    # project has hit repeatedly.
    import re

    for probe in (r"\bhealthz\b", r"\bhealthcheck\b", r"SANDBOX_URL\s*\)?\s*as\s+probe"):
        assert not re.search(probe, src), f"the module probes the site: {probe}"
    # The preview URL must only ever be a payload value, never a request target.
    assert "get(settings.SANDBOX_URL" not in src
    assert "_get(_sandbox" not in src


# ===========================================================================
# R5 / R6 / R7 — waiting
# ===========================================================================


@pytest.mark.asyncio
async def test_r5_polls_through_non_terminal_statuses() -> None:
    known = {d["deploy"]["id"] for d in HISTORY_BEFORE_PUSH}
    g = getter(
        HISTORY_BEFORE_PUSH + [deploy("dep-new", "build_in_progress")],
        HISTORY_BEFORE_PUSH + [deploy("dep-new", "update_in_progress")],
        HISTORY_BEFORE_PUSH + [deploy("dep-new", "live")],
    )
    outcome = await await_deploy(SHA, known, get=g, timeout_s=10, interval_s=0)
    assert outcome.ok is True
    assert len(g.calls) >= 3  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_r5_a_timeout_is_a_failure_not_an_assumption() -> None:
    """A build that never finishes has not shipped."""
    known = {d["deploy"]["id"] for d in HISTORY_BEFORE_PUSH}
    stuck = HISTORY_BEFORE_PUSH + [deploy("dep-new", "build_in_progress")]
    outcome = await await_deploy(SHA, known, get=getter(stuck), timeout_s=0.05, interval_s=0.01)
    assert outcome.ok is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["build_failed", "update_failed", "canceled", "pre_deploy_failed"])
async def test_r6_a_failed_deploy_is_a_failure(bad: str) -> None:
    known = {d["deploy"]["id"] for d in HISTORY_BEFORE_PUSH}
    outcome = await await_deploy(
        SHA, known, get=getter(HISTORY_BEFORE_PUSH + [deploy("dep-new", bad)]),
        timeout_s=5, interval_s=0,
    )
    assert outcome.ok is False
    assert outcome.status == bad


@pytest.mark.asyncio
async def test_r6_deactivated_is_not_success() -> None:
    """A later deploy superseded ours before it served anyone."""
    known = {d["deploy"]["id"] for d in HISTORY_BEFORE_PUSH}
    outcome = await await_deploy(
        SHA, known, get=getter(HISTORY_BEFORE_PUSH + [deploy("dep-new", "deactivated")]),
        timeout_s=5, interval_s=0,
    )
    assert outcome.ok is False


@pytest.mark.asyncio
async def test_r7_a_late_appearing_deploy_is_still_found() -> None:
    """Render takes a moment to register a push."""
    known = {d["deploy"]["id"] for d in HISTORY_BEFORE_PUSH}
    g = getter(
        HISTORY_BEFORE_PUSH,                                   # not registered yet
        HISTORY_BEFORE_PUSH,                                   # still not
        HISTORY_BEFORE_PUSH + [deploy("dep-new", "live")],      # there it is
    )
    outcome = await await_deploy(SHA, known, get=g, timeout_s=10, interval_s=0)
    assert outcome.ok is True and outcome.deploy_id == "dep-new"


# ===========================================================================
# R8 / R9 / R10 / R11 / R12 — reporting it
# ===========================================================================


@pytest.mark.asyncio
async def test_r8_success_posts_deploy_live_with_the_feature_ids() -> None:
    p = poster()
    ok = await post_webhook(
        P.PublishOutcome(ok=True, commit_sha=SHA, deploy_id="dep-new", status="live", detail=""),
        ["f-1", "f-2"], post=p,
    )
    assert ok is True
    body = p.sent[0]["body"]  # type: ignore[attr-defined]
    assert body["event"] == "deploy_live"
    assert body["render_deploy_id"] == "dep-new"
    assert body["feature_ids"] == ["f-1", "f-2"]
    assert body["preview_url"]


@pytest.mark.asyncio
async def test_r8_failure_posts_deploy_failed_so_features_return_to_voting() -> None:
    """Otherwise they stall in IN_SPRINT and the next sprint cannot see them."""
    p = poster()
    await post_webhook(
        P.PublishOutcome(ok=False, commit_sha=SHA, deploy_id="dep-new", status="build_failed", detail=""),
        ["f-1"], post=p,
    )
    assert p.sent[0]["body"]["event"] == "deploy_failed"  # type: ignore[attr-defined]
    assert p.sent[0]["body"]["feature_ids"] == ["f-1"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_r9_the_secret_travels_in_a_header_never_in_the_body() -> None:
    p = poster()
    await post_webhook(
        P.PublishOutcome(ok=True, commit_sha=SHA, deploy_id="d", status="live", detail=""),
        ["f-1"], post=p,
    )
    sent = p.sent[0]  # type: ignore[attr-defined]
    header_names = {k.lower() for k in sent["headers"]}
    assert any("secret" in n for n in header_names), "the endpoint reads a secret header"
    assert "secret" not in json.dumps(sent["body"]).lower()


def test_r9_no_credential_is_logged() -> None:
    import pathlib

    src = pathlib.Path(P.__file__).read_text()
    for line in src.splitlines():
        if "logger." in line:
            for cred in ("RENDER_API_KEY", "RENDER_WEBHOOK_SECRET", "get_secret_value"):
                assert cred not in line, f"credential in a log line: {line.strip()}"


@pytest.mark.asyncio
async def test_r12_the_webhook_is_posted_at_most_once() -> None:
    """The endpoint is idempotent, so a retry is safe but pointless."""
    p = poster(status=500)
    ok = await post_webhook(
        P.PublishOutcome(ok=True, commit_sha=SHA, deploy_id="d", status="live", detail=""),
        ["f-1"], post=p,
    )
    assert ok is False
    assert len(p.sent) == 1, "the failed post was retried"  # type: ignore[attr-defined]


# ===========================================================================
# R13 / R14 / R15 / R16 — the sequence
# ===========================================================================


@pytest.mark.asyncio
async def test_r16_publish_never_raises_even_when_the_push_fails() -> None:
    """The caller is a pipeline step that must keep going."""

    async def _boom(*_a: Any, **_k: Any) -> tuple[int, str, str]:
        raise RuntimeError("git remote rejected the push")

    outcome = await publish(["f-1"], runner=_boom, get=getter(HISTORY_BEFORE_PUSH), post=poster())
    assert outcome.ok is False


@pytest.mark.asyncio
async def test_r15_the_whole_sequence_runs_with_no_network() -> None:
    """Push, poll and report, against three injected seams."""

    async def _runner(command: str, cwd: Any, env: Any) -> tuple[int, str, str]:
        # deploy.sh, then a sha lookup
        return 0, SHA, ""

    p = poster()
    outcome = await publish(
        ["f-1"],
        runner=_runner,
        get=getter(HISTORY_BEFORE_PUSH, HISTORY_BEFORE_PUSH + [deploy("dep-new", "live")]),
        post=p,
    )
    assert outcome.ok is True
    assert p.sent, "the sequence finished without reporting the deploy"  # type: ignore[attr-defined]


def test_r13_the_target_repos_own_script_does_the_pushing() -> None:
    """Duplicating commit-and-push logic here means two things to keep in step."""
    import pathlib

    src = pathlib.Path(P.__file__).read_text()
    assert "deploy.sh" in src
    assert "SKIP_PUSH" in src, "it must run the script with pushing enabled"
    assert "git push" not in src, "push logic was reimplemented"


def test_module_touches_neither_postgres_nor_redis() -> None:
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(P.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "supabase" not in imported
    assert "redis" not in imported


# ===========================================================================
# R13 / R13a — the environment deploy.sh actually runs in
# ===========================================================================


@pytest.mark.asyncio
async def test_r13a_the_push_does_not_recompile() -> None:
    """compiler.py already produced the source.

    deploy.sh recompiles by default, which would pay for a second pdd call per
    cycle and give the model another chance to re-roll the app's appearance
    behind the blueprint's Design Lock.
    """
    seen: list[dict[str, Any]] = []

    async def _runner(command: str, cwd: Any, env: Any) -> tuple[int, str, str]:
        seen.append({"command": command, "env": dict(env)})
        return 0, SHA, ""

    await publish(
        ["f-1"],
        runner=_runner,
        get=getter(HISTORY_BEFORE_PUSH, HISTORY_BEFORE_PUSH + [deploy("dep-new", "live")]),
        post=poster(),
    )
    deploy_call = next(c for c in seen if "deploy.sh" in c["command"])
    assert deploy_call["env"].get("SKIP_COMPILE") == "1"


@pytest.mark.asyncio
async def test_r13_skip_push_cannot_be_inherited() -> None:
    """A daemon launched from a shell with SKIP_PUSH set must still push."""
    import os

    seen: list[dict[str, Any]] = []

    async def _runner(command: str, cwd: Any, env: Any) -> tuple[int, str, str]:
        seen.append({"command": command, "env": dict(env)})
        return 0, SHA, ""

    os.environ["SKIP_PUSH"] = "1"
    try:
        await publish(
            ["f-1"],
            runner=_runner,
            get=getter(HISTORY_BEFORE_PUSH, HISTORY_BEFORE_PUSH + [deploy("dep-new", "live")]),
            post=poster(),
        )
    finally:
        os.environ.pop("SKIP_PUSH", None)

    deploy_call = next(c for c in seen if "deploy.sh" in c["command"])
    assert "SKIP_PUSH" not in deploy_call["env"], "the publisher would have shipped nothing"
