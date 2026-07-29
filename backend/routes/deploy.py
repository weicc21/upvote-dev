"""backend/routes/deploy.py

Record that a build went live, move the features it carried to shipped,
and tell the board where to point its iframe.

This module DOES NOT trigger a deploy, call the Render API, or run git.
It records what the platform reports.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any
from fnmatch import fnmatch
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, Request, Response
from supabase._async.client import AsyncClient

from backend.deps import get_settings, get_supabase, raise_error
from orchestrator.decisions import PROGRAMMATIC, record_decision
from shared.config import Settings
from shared.constants import (
    DecisionPhase,
    FeatureStatus,
    TABLE_DEPLOYMENTS,
    TABLE_FEATURE_REQUESTS,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _url_in_allowlist(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    """Return True when *url* is https and its host matches the allowlist.

    Entries are wildcard patterns (`*.onrender.com`), so exact membership is
    wrong — it refuses every real deploy. https is required because this value
    is loaded in an iframe on a page the community trusts.
    """
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(fnmatch(host, pattern.lower()) for pattern in allowed_hosts)


# ---------------------------------------------------------------------------
# GET /api/sandbox  (R12–R16)
# ---------------------------------------------------------------------------


@router.get("/api/sandbox")
async def get_sandbox(
    response: Response,
    supabase: AsyncClient = Depends(get_supabase),
    cfg: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return the most recent deployment for the iframe embed."""

    # R16: anonymous, cacheable ~30 s
    response.headers["Cache-Control"] = "public, max-age=30"

    # Fetch the single most-recent deployment
    result = (
        await supabase.table(TABLE_DEPLOYMENTS)
        .select("preview_url, version, created_at")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if result.data:
        row = result.data[0]
        preview_url: str = row["preview_url"]

        # R14/R15: re-validate against the *current* allowlist on read
        if _url_in_allowlist(preview_url, cfg.SANDBOX_ALLOWED_HOSTS):
            return {
                "status": "live",
                "preview_url": preview_url,
                "version": row["version"],
                "deployed_at": row["created_at"],
            }
        # URL no longer passes — fall through to "none"

    # R13: no deployment (or stored URL now outside allowlist)
    return {
        "status": "none",
        "preview_url": cfg.SANDBOX_URL,  # may be None
    }


# ---------------------------------------------------------------------------
# POST /webhooks/render  (R1–R11, R17–R18)
# ---------------------------------------------------------------------------


@router.post("/webhooks/render", status_code=204)
async def render_webhook(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
    supabase: AsyncClient = Depends(get_supabase),
    cfg: Settings = Depends(get_settings),
) -> Response:
    """Handle Render deploy webhooks (server-to-server, no CORS)."""

    # R1: constant-time secret comparison
    expected = cfg.RENDER_WEBHOOK_SECRET.get_secret_value()
    if x_webhook_secret is None or not hmac.compare_digest(
        x_webhook_secret, expected
    ):
        raise_error(401, "unauthorized", "Invalid or missing webhook secret")

    body: dict[str, Any] = await request.json()

    event: str | None = body.get("event")
    render_deploy_id: str | None = body.get("render_deploy_id")
    version: str | None = body.get("version")
    feature_ids: list[str] = body.get("feature_ids", [])
    preview_url: str | None = body.get("preview_url")

    # Basic payload validation
    if not event or not render_deploy_id or version is None:
        raise_error(400, "bad_request", "Missing required webhook fields")

    # ------------------------------------------------------------------
    # deploy_live
    # ------------------------------------------------------------------
    if event == "deploy_live":
        # R4: preview_url required on deploy_live
        if not preview_url:
            raise_error(
                400,
                "bad_request",
                "preview_url is required for deploy_live events",
            )

        # R3: allowlist check
        if not _url_in_allowlist(preview_url, cfg.SANDBOX_ALLOWED_HOSTS):
            raise_error(
                400,
                "bad_request",
                "preview_url host is not in the allowed hosts list",
            )

        # R8: idempotency — check if this deploy was already recorded
        existing = (
            await supabase.table(TABLE_DEPLOYMENTS)
            .select("id")
            .eq("render_deploy_id", render_deploy_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            # Already recorded — answer 204 without duplicating
            return Response(status_code=204)

        # R5: insert deployment row
        await supabase.table(TABLE_DEPLOYMENTS).insert(
            {
                "version": version,
                "render_deploy_id": render_deploy_id,
                "preview_url": preview_url,
                "shipped_feature_ids": feature_ids,
            }
        ).execute()

        # R6/R7: move features IN_SPRINT → COMPILED (only those currently IN_SPRINT)
        for fid in feature_ids:
            try:
                await (
                    supabase.table(TABLE_FEATURE_REQUESTS)
                    .update({"status": FeatureStatus.COMPILED.value})
                    .eq("id", fid)
                    .eq("status", FeatureStatus.IN_SPRINT.value)
                    .execute()
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to move feature %s to COMPILED", fid, exc_info=True
                )

        # R10/R11: file decision — failure must not change the response
        try:
            await record_decision(
                supabase,
                phase=DecisionPhase.DEPLOY,
                agent="deploy_webhook",
                decision={
                    "event": event,
                    "version": version,
                    "render_deploy_id": render_deploy_id,
                    "feature_count": len(feature_ids),
                    "feature_ids": feature_ids,
                },
                model_version=PROGRAMMATIC,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to record deploy decision for %s",
                render_deploy_id,
                exc_info=True,
            )

        return Response(status_code=204)

    # ------------------------------------------------------------------
    # deploy_failed  (R9)
    # ------------------------------------------------------------------
    if event == "deploy_failed":
        # Return named features to VOTING (only those currently IN_SPRINT)
        for fid in feature_ids:
            try:
                await (
                    supabase.table(TABLE_FEATURE_REQUESTS)
                    .update({"status": FeatureStatus.VOTING.value})
                    .eq("id", fid)
                    .eq("status", FeatureStatus.IN_SPRINT.value)
                    .execute()
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to revert feature %s to VOTING", fid, exc_info=True
                )

        # R10/R11: file decision for the failure
        try:
            await record_decision(
                supabase,
                phase=DecisionPhase.DEPLOY,
                agent="deploy_webhook",
                decision={
                    "event": event,
                    "version": version,
                    "render_deploy_id": render_deploy_id,
                    "feature_count": len(feature_ids),
                    "feature_ids": feature_ids,
                },
                model_version=PROGRAMMATIC,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to record deploy_failed decision for %s",
                render_deploy_id,
                exc_info=True,
            )

        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Unknown event — accept silently (platforms may add events)
    # ------------------------------------------------------------------
    return Response(status_code=204)