"""Publish the compiled target app and report the deploy outcome.

Pushes via the target repo's ``deploy.sh``, polls the Render API until the
deploy we caused reaches a terminal status, then posts the result to this
system's own ``POST /webhooks/render``.
"""

from __future__ import annotations

import asyncio
import json
import os
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from shared.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class Runner(Protocol):
    """Minimal subprocess runner — same shape the compiler uses."""

    async def __call__(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Run *cmd* in a shell; return ``(returncode, stdout, stderr)``."""
        ...


HttpGet = Callable[[str, Mapping[str, str]], Awaitable[tuple[int, str]]]
HttpPost = Callable[[str, Mapping[str, str], str], Awaitable[tuple[int, str]]]


@dataclass(frozen=True)
class PublishOutcome:
    """Immutable result of a publish attempt."""

    ok: bool
    commit_sha: str | None
    deploy_id: str | None
    status: str
    detail: str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        "live",
        "build_failed",
        "update_failed",
        "canceled",
        "pre_deploy_failed",
        "deactivated",
    }
)

_RENDER_API_BASE = "https://api.render.com/v1"


# ---------------------------------------------------------------------------
# Default seams (real implementations)
# ---------------------------------------------------------------------------


async def _default_runner(
    cmd: str,
    cwd: Any = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run *cmd* through the system shell."""
    import os

    merged_env: dict[str, str] | None = None
    if env is not None:
        merged_env = {**os.environ, **env}

    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd,
        env=merged_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        (stdout_bytes or b"").decode(errors="replace"),
        (stderr_bytes or b"").decode(errors="replace"),
    )


async def _default_get(
    url: str, headers: Mapping[str, str]
) -> tuple[int, str]:
    """HTTP GET via ``httpx`` — already a declared dependency (R15's default)."""
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=dict(headers))
        return resp.status_code, resp.text


async def _default_post(
    url: str, headers: Mapping[str, str], body: str
) -> tuple[int, str]:
    """HTTP POST via ``httpx``."""
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=dict(headers), content=body)
        return resp.status_code, resp.text


def _render_headers() -> dict[str, str]:
    """Auth headers for the Render API.  Never logged (R9)."""
    api_key = settings.RENDER_API_KEY
    if api_key is None:
        raise RuntimeError("RENDER_API_KEY is not configured")
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key.get_secret_value()}",
    }


def _deploys_url() -> str:
    service_id = settings.RENDER_SERVICE_ID
    if not service_id:
        raise RuntimeError("RENDER_SERVICE_ID is not configured")
    return f"{_RENDER_API_BASE}/services/{service_id}/deploys"


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


async def snapshot_deploy_ids(
    *, get: HttpGet | None = None
) -> set[str]:
    """Return the set of deploy ids Render already knows about (R2)."""
    _get = get or _default_get
    headers = _render_headers()
    url = _deploys_url()

    ids: set[str] = set()
    cursor: str | None = None

    # Paginate — Render returns up to 20 per page by default.
    for _ in range(50):  # safety cap
        page_url = url
        params: list[str] = []
        if cursor:
            params.append(f"cursor={cursor}")
        params.append("limit=100")
        page_url = f"{url}?{'&'.join(params)}"

        status, body = await _get(page_url, headers)
        if status != 200:
            logger.warning(
                "Render deploy list returned %d; snapshot may be incomplete",
                status,
            )
            break

        items = json.loads(body)
        if not isinstance(items, list) or not items:
            break

        for item in items:
            deploy = item.get("deploy", item)
            deploy_id = deploy.get("id")
            if deploy_id:
                ids.add(deploy_id)

        # Render uses cursor-based pagination; the last item's id is the cursor.
        if len(items) < 100:
            break
        last_deploy = items[-1].get("deploy", items[-1])
        cursor = last_deploy.get("id")
        if not cursor:
            break

    logger.info("Snapshot captured %d existing deploy ids", len(ids))
    return ids


async def push_target(*, runner: Runner | None = None) -> str:
    """Commit and push the target app via ``deploy.sh``; return the commit sha (R13, R14)."""
    _runner = runner or _default_runner
    target_dir = str(settings.TARGET_PROMPT_DIR)

    # deploy.sh commits and pushes. SKIP_PUSH is removed rather than left to
    # chance: a daemon started from a shell that had it exported would silently
    # never push, and would report success having shipped nothing (R13).
    # SKIP_COMPILE is set because compiler.py already produced the source —
    # deploy.sh recompiles by default, which would pay for a second pdd call and
    # give the model another chance to re-roll the design behind the lock (R13a).
    push_env = {k: v for k, v in os.environ.items() if k != "SKIP_PUSH"}
    push_env["SKIP_COMPILE"] = "1"
    rc, stdout, stderr = await _runner("bash deploy.sh", target_dir, push_env)
    if rc != 0:
        raise RuntimeError(
            f"deploy.sh exited {rc}: {stderr[:500] if stderr else '(no stderr)'}"
        )

    logger.info("deploy.sh completed successfully")

    # Retrieve the HEAD sha after the push.
    rc2, sha_out, sha_err = await _runner("git rev-parse HEAD", target_dir, push_env)
    sha = sha_out.strip()
    if rc2 != 0 or not sha:
        raise RuntimeError(
            f"Could not determine commit sha after push: rc={rc2} "
            f"stderr={sha_err[:300] if sha_err else '(none)'}"
        )

    logger.info("Pushed commit %s", sha[:12])
    return sha


async def await_deploy(
    commit_sha: str,
    known_ids: set[str],
    *,
    get: HttpGet | None = None,
    timeout_s: float = 900.0,
    interval_s: float = 10.0,
) -> PublishOutcome:
    """Poll until the deploy we caused reaches a terminal status (R1, R5, R6, R7)."""
    _get = get or _default_get
    headers = _render_headers()
    url = _deploys_url()

    deadline = asyncio.get_event_loop().time() + timeout_s
    last_status: str | None = None

    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return PublishOutcome(
                ok=False,
                commit_sha=commit_sha,
                deploy_id=None,
                status="timeout",
                detail=f"No terminal deploy found within {timeout_s}s",
            )

        status_code, body = await _get(f"{url}?limit=20", headers)
        if status_code == 200:
            items = json.loads(body)
            if isinstance(items, list):
                for item in items:
                    deploy = item.get("deploy", item)
                    deploy_id = deploy.get("id", "")
                    deploy_status = deploy.get("status", "")

                    # R1: must be absent from pre-push snapshot AND match our sha.
                    if deploy_id in known_ids:
                        continue

                    commit_info = deploy.get("commit") or {}
                    deploy_commit_id = commit_info.get("id", "")
                    if deploy_commit_id != commit_sha:
                        continue

                    # This is our deploy.
                    if deploy_status != last_status:
                        logger.info(
                            "Deploy %s status: %s", deploy_id[:12], deploy_status
                        )
                        last_status = deploy_status

                    if deploy_status in _TERMINAL_STATUSES:
                        ok = deploy_status == "live"  # R6
                        logger.info(
                            "Deploy %s reached terminal status: %s (ok=%s)",
                            deploy_id[:12],
                            deploy_status,
                            ok,
                        )
                        return PublishOutcome(
                            ok=ok,
                            commit_sha=commit_sha,
                            deploy_id=deploy_id,
                            status=deploy_status,
                            detail=f"Deploy {deploy_id} is {deploy_status}",
                        )
                    # Found our deploy but not terminal yet — keep polling.
                    break
        else:
            logger.warning("Render API returned %d during polling", status_code)

        await asyncio.sleep(min(interval_s, max(remaining, 0)))


async def post_webhook(
    outcome: PublishOutcome,
    feature_ids: Sequence[str],
    *,
    post: HttpPost | None = None,
) -> bool:
    """Post the deploy result to our own webhook endpoint (R8, R9, R10, R11, R12)."""
    _post = post or _default_post

    if not settings.SANDBOX_URL:
        logger.warning("SANDBOX_URL not configured; skipping webhook post")
        return False

    # R11: version is the short commit sha.
    version = (outcome.commit_sha or "unknown")[:12]

    # R8: deploy_live on success, deploy_failed on failure.
    event_type = "deploy_live" if outcome.ok else "deploy_failed"

    payload = {
        # The endpoint and openapi both read `event` (R8).
        "event": event_type,
        "render_deploy_id": outcome.deploy_id or "unknown",
        "version": version,
        "preview_url": settings.SANDBOX_URL,  # R10
        "feature_ids": list(feature_ids),
    }

    # R9: secret in the header the endpoint reads; never logged.
    webhook_secret = settings.RENDER_WEBHOOK_SECRET.get_secret_value()
    headers = {
        "Content-Type": "application/json",
        # FastAPI derives the header name from the endpoint's parameter
        # `x_webhook_secret`, i.e. `X-Webhook-Secret`. Any other spelling is a 401.
        "X-Webhook-Secret": webhook_secret,
    }


    # If SANDBOX_URL is the app itself, the webhook lives on the main backend.
    # But the webhook endpoint is on *this* system, not the sandbox.
    # Our own API — never FORUM_ORIGIN, which is the browser origin of the
    # frontend and answers 404 for /webhooks/render, and never SANDBOX_URL,
    # which is the deployed target app.
    webhook_url = f"{settings.SELF_API_BASE.rstrip('/')}/webhooks/render"

    logger.info("Posting %s webhook for deploy %s", event_type, outcome.deploy_id)

    # R12: at most one retry.
    for attempt in range(1):  # R12: at most once — the endpoint is idempotent, so a retry is pointless
        try:
            status_code, resp_body = await _post(
                webhook_url, headers, json.dumps(payload)
            )
            if 200 <= status_code < 300:
                logger.info("Webhook accepted (status %d)", status_code)
                return True
            logger.warning(
                "Webhook returned %d on attempt %d: %s",
                status_code,
                attempt + 1,
                resp_body[:200],
            )
        except Exception:
            logger.exception("Webhook post failed on attempt %d", attempt + 1)

        if attempt == 0:
            await asyncio.sleep(2)

    logger.error("Webhook not accepted (posted once — R12)")
    return False


async def publish(
    feature_ids: Sequence[str],
    *,
    runner: Runner | None = None,
    get: HttpGet | None = None,
    post: HttpPost | None = None,
) -> PublishOutcome:
    """Push, await the deploy, then report it — the whole sequence (R15, R16, R17)."""
    try:
        # R2: snapshot BEFORE pushing.
        logger.info("Taking pre-push deploy snapshot")
        known_ids = await snapshot_deploy_ids(get=get)

        # Push.
        logger.info("Pushing target app")
        commit_sha = await push_target(runner=runner)

        # Wait for the deploy.
        logger.info("Awaiting deploy for commit %s", commit_sha[:12])
        outcome = await await_deploy(commit_sha, known_ids, get=get)

        # Post webhook.
        logger.info("Posting webhook (ok=%s, status=%s)", outcome.ok, outcome.status)
        await post_webhook(outcome, feature_ids, post=post)

        return outcome

    except Exception as exc:
        # R16: never raise out of publish.
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("Publish failed: %s", detail)
        outcome = PublishOutcome(
            ok=False,
            commit_sha=None,
            deploy_id=None,
            status="error",
            detail=detail,
        )
        # Still try to post the failure webhook so features don't stall (R8).
        try:
            await post_webhook(outcome, feature_ids, post=post)
        except Exception:
            logger.exception("Failed to post failure webhook")
        return outcome