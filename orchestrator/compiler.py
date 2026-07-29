"""Compile a winning feature into the target app's prompt and regenerate.

This module edits the target app's prompt file under
``settings.TARGET_PROMPT_DIR``, shells out to ``settings.COMPILE_COMMAND``,
and leaves the feature in an honest state either way.

It does **not** call an LLM, run ``git``, or make network requests of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Final, Mapping

import redis.asyncio as aioredis
from supabase._async.client import AsyncClient, create_client

from shared.config import settings
from shared.constants import (
    TABLE_BUILD_LOGS,
    TABLE_DECISION_LOG,
    TABLE_FEATURE_REQUESTS,
    REDIS_AGENT_EVENTS,
    BroadcastPhase,
    BuildStatus,
    DecisionPhase,
    DecisionType,
    FeatureStatus,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

Runner = Callable[[str, Path, Mapping[str, str]], Awaitable[tuple[int, str, str]]]
"""Injected seam: (command, cwd, env) -> (returncode, stdout, stderr)."""


@dataclass(frozen=True)
class CompileOutcome:
    """Result of a single compile attempt."""

    feature_id: str
    ok: bool
    version_hash: str | None
    summary: str
    log_tail: str
    duration_s: float


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ANCHOR: Final[str] = "# [COMMUNITY FEATURES INSERTION ZONE]"
_COMPILE_TIMEOUT_SECONDS: Final[int] = 300
_LOG_TAIL_LINES: Final[int] = 80
# 32000 is the project-wide floor (CLAUDE.md): below it long generations are
# truncated mid-file by the provider ceiling and read as a broken compile.
_PDD_MAX_OUTPUT_TOKENS: Final[str] = "32000"
# Both names are fixed by the target project; architect.py pins the same
# blueprint filename. TARGET_PROMPT_DIR is a git working tree, not a prompt
# folder — see module_map, "Target app layout".
_PROMPT_FILENAME: Final[str] = "streaks_demo_typescriptreact.prompt"
_GENERATED_SOURCE_FILENAME: Final[str] = "streaks_demo.tsx"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tail(text: str, n: int = _LOG_TAIL_LINES) -> str:
    """Return the last *n* lines of *text*."""
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _hash_generated_source(directory: Path) -> str:
    """SHA-256 of the single generated source file (R14).

    Deliberately not a directory walk: TARGET_PROMPT_DIR is a git working tree
    holding node_modules/, dist/ and .git/, so rglob would hash tens of
    thousands of files and produce a different digest on every machine. Two
    compiles emitting identical code must produce identical hashes.
    """
    source = directory / _GENERATED_SOURCE_FILENAME
    if not source.is_file():
        return ""
    return hashlib.sha256(source.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Feature Block (R1, R4, R19)
# ---------------------------------------------------------------------------


def build_feature_block(feature: Mapping[str, Any]) -> str:
    """Return the text appended to the target prompt for one feature.

    The block carries the feature's id as a comment (R4) and describes
    behaviour using the community's own title and description (R19).
    """
    fid = feature["id"]
    title = feature.get("title", "Untitled")
    description = feature.get("description", "")
    lines = [
        f"## {title}",
        "",
        description.strip(),
        "",
        f"<!-- feature_id:{fid} -->",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt-file editing (R1–R5)
# ---------------------------------------------------------------------------


def _read_prompt(prompt_path: Path) -> str:
    return prompt_path.read_text(encoding="utf-8")


def _write_prompt_atomic(prompt_path: Path, content: str) -> None:
    """Write *content* atomically via a temp file in the same directory (R3)."""
    fd, tmp = tempfile.mkstemp(
        dir=str(prompt_path.parent), suffix=".tmp", prefix=".prompt_"
    )
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, str(prompt_path))
    except BaseException:
        os.close(fd) if not os.get_inheritable(fd) else None  # noqa: E501
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _insert_block(prompt_text: str, block: str) -> str:
    """Insert *block* below the anchor line (R1, R2)."""
    if _ANCHOR not in prompt_text:
        raise RuntimeError(
            f"Anchor line {_ANCHOR!r} not found in target prompt — "
            "refusing to append (R2)"
        )
    # Split on the anchor, keeping it in the first part.
    idx = prompt_text.index(_ANCHOR)
    anchor_end = idx + len(_ANCHOR)
    # Find the end of the anchor line (include the newline if present).
    nl = prompt_text.find("\n", anchor_end)
    if nl == -1:
        before = prompt_text
        after = ""
    else:
        before = prompt_text[: nl + 1]
        after = prompt_text[nl + 1 :]
    return before + "\n" + block + after


def _feature_already_in_prompt(prompt_text: str, feature_id: str) -> bool:
    """True when a Feature Block for *feature_id* already exists (R4)."""
    marker = f"<!-- feature_id:{feature_id} -->"
    return marker in prompt_text


# ---------------------------------------------------------------------------
# Default runner (R6, R7, R8)
# ---------------------------------------------------------------------------


async def _default_runner(
    command: str, cwd: Path, env: Mapping[str, str]
) -> tuple[int, str, str]:
    """Spawn *command* through a shell with a timeout (R6, R8)."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        env=dict(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=_COMPILE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return (
            -1,
            "",
            f"Compile timed out after {_COMPILE_TIMEOUT_SECONDS}s",
        )
    return (
        proc.returncode or 0,
        (stdout_bytes or b"").decode(errors="replace"),
        (stderr_bytes or b"").decode(errors="replace"),
    )


# ---------------------------------------------------------------------------
# Event publishing (R16)
# ---------------------------------------------------------------------------


async def _publish_event(
    redis: aioredis.Redis,
    event_type: str,
    *,
    feature_id: str,
    phase: str = BroadcastPhase.COMPILING,
) -> None:
    """Publish a thin agent event — no pitch content, no compiler output (R16)."""
    payload = json.dumps(
        {
            "type": event_type,
            "phase": phase,
            "feature_id": feature_id,
        }
    )
    try:
        await redis.publish(REDIS_AGENT_EVENTS, payload)
    except Exception:
        logger.warning("Failed to publish %s event for %s", event_type, feature_id)


# ---------------------------------------------------------------------------
# Database helpers (R10–R15)
# ---------------------------------------------------------------------------


async def _transition_feature(
    supabase: AsyncClient,
    feature_id: str,
    to_status: FeatureStatus,
) -> bool:
    """Move a feature to *to_status* only if it is still IN_SPRINT (R12).

    Returns True when the update matched exactly one row.
    """
    resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .update({"status": str(to_status)})
        .eq("id", feature_id)
        .eq("status", str(FeatureStatus.IN_SPRINT))
        .execute()
    )
    return bool(resp.data)


async def _record_build_log(
    supabase: AsyncClient,
    *,
    feature_id: str,
    status: BuildStatus,
    version_hash: str | None,
    summary: str,
    log_tail: str,
) -> None:
    """Insert one build_logs row (R13)."""
    # build_logs is keyed by build, not by feature: its only columns are
    # version_hash, synthesis_summary, status (+ id, completed_at). The
    # feature link lives in decision_log, which has a feature_id column.
    detail = f"feature {feature_id}: {summary}"
    if log_tail:
        detail = f"{detail}\n{_tail(log_tail, 20)}"
    await supabase.table(TABLE_BUILD_LOGS).insert(
        {
            "status": str(status),
            "version_hash": version_hash or "",
            "synthesis_summary": detail[:2000],
        }
    ).execute()


async def _record_decision(
    supabase: AsyncClient,
    *,
    feature_id: str,
    decision_type: DecisionType,
    summary: str,
) -> None:
    """Insert one decision_log row (R15)."""
    await supabase.table(TABLE_DECISION_LOG).insert(
        {
            "feature_id": feature_id,
            "phase": str(DecisionPhase.COMPILE),
            "agent": "compiler",
            "model_version": "programmatic",
            "decision": json.dumps(
                {"type": str(decision_type), "summary": summary[:2000]}
            ),
        }
    ).execute()


# ---------------------------------------------------------------------------
# Core compile path
# ---------------------------------------------------------------------------


async def compile_feature(
    feature: Mapping[str, Any],
    supabase: AsyncClient,
    redis: aioredis.Redis,
    *,
    runner: Runner | None = None,
) -> CompileOutcome:
    """Append, compile, and resolve one feature."""
    run = runner or _default_runner
    feature_id: str = feature["id"]
    prompt_dir = settings.TARGET_PROMPT_DIR
    # The blueprint is named, never discovered (R2a). Globbing for "*.prompt"
    # and taking the first match would append community features into whatever
    # file happened to sort first — TARGET_PROMPT_DIR is a whole repository.
    prompt_path = prompt_dir / _PROMPT_FILENAME
    if not prompt_path.is_file():
        raise FileNotFoundError(
            f"blueprint not found: {prompt_path}. TARGET_PROMPT_DIR must contain "
            f"{_PROMPT_FILENAME}."
        )

    t0 = time.monotonic()

    # --- Publish start event (R16) ---
    await _publish_event(redis, "compile_started", feature_id=feature_id)

    # --- Read and snapshot the prompt (R5) ---
    original_prompt = _read_prompt(prompt_path)

    # --- Duplicate guard (R4) ---
    if _feature_already_in_prompt(original_prompt, feature_id):
        duration = time.monotonic() - t0
        summary = f"Feature {feature_id} already present in prompt — skipping"
        logger.info(
            "compile feature_id=%s outcome=skipped_duplicate duration=%.2fs",
            feature_id,
            duration,
        )
        # Transition to COMPILED since the feature is already in the prompt.
        await _transition_feature(supabase, feature_id, FeatureStatus.COMPILED)
        return CompileOutcome(
            feature_id=feature_id,
            ok=True,
            version_hash=None,
            summary=summary,
            log_tail="",
            duration_s=duration,
        )

    # --- Build and insert the Feature Block (R1, R19) ---
    block = build_feature_block(feature)
    try:
        new_prompt = _insert_block(original_prompt, block)
    except RuntimeError:
        # Anchor missing (R2) — cannot proceed.
        duration = time.monotonic() - t0
        summary = "Anchor line missing from target prompt"
        logger.error(
            "compile feature_id=%s outcome=failed reason=anchor_missing duration=%.2fs",
            feature_id,
            duration,
        )
        await _publish_event(redis, "compile_failed", feature_id=feature_id)
        # Return to VOTING (R10) — must not leave IN_SPRINT (R11).
        await _transition_feature(supabase, feature_id, FeatureStatus.VOTING)
        await _record_build_log(
            supabase,
            feature_id=feature_id,
            status=BuildStatus.FAILED,
            version_hash=None,
            summary=summary,
            log_tail="",
        )
        await _record_decision(
            supabase,
            feature_id=feature_id,
            decision_type=DecisionType.COMPILE_FAILURE,
            summary=summary,
        )
        return CompileOutcome(
            feature_id=feature_id,
            ok=False,
            version_hash=None,
            summary=summary,
            log_tail="",
            duration_s=duration,
        )

    # --- Write the modified prompt atomically (R3) ---
    _write_prompt_atomic(prompt_path, new_prompt)

    # --- Run the compile command (R6, R7, R8) ---
    # pdd resolves its .env relative to its own cwd — the target repo, which has
    # none — and pydantic-settings never exports our .env into os.environ. So
    # both values are passed deliberately rather than inherited by luck: without
    # the key every candidate model fails with "Required environment value not
    # set", and without the ceiling long output is truncated mid-file.
    child_env: dict[str, str] = {
        **os.environ,
        "PDD_COMMAND_MAX_OUTPUT_TOKENS": _PDD_MAX_OUTPUT_TOKENS,
        "TOKENROUTER_API_KEY": settings.TOKENROUTER_API_KEY.get_secret_value(),
    }
    command = settings.COMPILE_COMMAND  # operator-supplied only (R7)

    try:
        returncode, stdout, stderr = await run(command, prompt_dir, child_env)
    except Exception as exc:
        # Runner itself raised — treat as failure.
        returncode = -1
        stdout = ""
        stderr = str(exc)

    duration = time.monotonic() - t0
    combined_tail = _tail(f"{stdout}\n{stderr}")
    ok = returncode == 0

    if ok:
        # --- Success path ---
        version_hash = _hash_generated_source(prompt_dir)
        summary = f"Compiled feature {feature_id} successfully"
        logger.info(
            "compile feature_id=%s outcome=success version_hash=%s duration=%.2fs",
            feature_id,
            version_hash[:12],
            duration,
        )
        await _publish_event(redis, "compile_succeeded", feature_id=feature_id)

        # Transition to COMPILED (R10, R12).
        transitioned = await _transition_feature(
            supabase, feature_id, FeatureStatus.COMPILED
        )
        if not transitioned:
            logger.warning(
                "Feature %s was no longer IN_SPRINT at transition time", feature_id
            )

        await _record_build_log(
            supabase,
            feature_id=feature_id,
            status=BuildStatus.SUCCESS,
            version_hash=version_hash,
            summary=summary,
            log_tail=combined_tail,
        )
        await _record_decision(
            supabase,
            feature_id=feature_id,
            decision_type=DecisionType.COMPILE_SUCCESS,
            summary=summary,
        )

        return CompileOutcome(
            feature_id=feature_id,
            ok=True,
            version_hash=version_hash,
            summary=summary,
            log_tail=combined_tail,
            duration_s=duration,
        )
    else:
        # --- Failure path: restore the original prompt (R5) ---
        _write_prompt_atomic(prompt_path, original_prompt)

        summary = f"Compile failed for feature {feature_id} (exit {returncode})"
        logger.info(
            "compile feature_id=%s outcome=failed exit=%d duration=%.2fs",
            feature_id,
            returncode,
            duration,
        )
        await _publish_event(redis, "compile_failed", feature_id=feature_id)

        # Return to VOTING (R10, R11, R12).
        transitioned = await _transition_feature(
            supabase, feature_id, FeatureStatus.VOTING
        )
        if not transitioned:
            logger.warning(
                "Feature %s was no longer IN_SPRINT at transition time", feature_id
            )

        await _record_build_log(
            supabase,
            feature_id=feature_id,
            status=BuildStatus.FAILED,
            version_hash=None,
            summary=summary,
            log_tail=combined_tail,
        )
        await _record_decision(
            supabase,
            feature_id=feature_id,
            decision_type=DecisionType.COMPILE_FAILURE,
            summary=summary,
        )

        return CompileOutcome(
            feature_id=feature_id,
            ok=False,
            version_hash=None,
            summary=summary,
            log_tail=combined_tail,
            duration_s=duration,
        )


# ---------------------------------------------------------------------------
# Queue consumer
# ---------------------------------------------------------------------------


async def compile_next(
    supabase: AsyncClient,
    redis: aioredis.Redis,
    *,
    runner: Runner | None = None,
) -> CompileOutcome | None:
    """Compile the oldest IN_SPRINT feature, or return None (R18)."""
    resp = (
        await supabase.table(TABLE_FEATURE_REQUESTS)
        .select("*")
        .eq("status", str(FeatureStatus.IN_SPRINT))
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return None

    feature = resp.data[0]
    try:
        return await compile_feature(feature, supabase, redis, runner=runner)
    except Exception as exc:
        # R11: must not leave IN_SPRINT under any exit path.
        feature_id = feature["id"]
        logger.exception(
            "Unhandled error compiling feature_id=%s — returning to VOTING",
            feature_id,
        )
        try:
            await _transition_feature(supabase, feature_id, FeatureStatus.VOTING)
        except Exception:
            logger.exception(
                "Failed to transition feature %s back to VOTING", feature_id
            )
        try:
            await _record_build_log(
                supabase,
                feature_id=feature_id,
                status=BuildStatus.FAILED,
                version_hash=None,
                summary=f"Unhandled error: {exc!r}"[:2000],
                log_tail="",
            )
        except Exception:
            logger.exception(
                "Failed to record build log for feature %s", feature_id
            )
        try:
            await _record_decision(
                supabase,
                feature_id=feature_id,
                decision_type=DecisionType.COMPILE_FAILURE,
                summary=f"Unhandled error: {exc!r}"[:2000],
            )
        except Exception:
            logger.exception(
                "Failed to record decision for feature %s", feature_id
            )
        try:
            await _publish_event(redis, "compile_failed", feature_id=feature_id)
        except Exception:
            pass
        return CompileOutcome(
            feature_id=feature_id,
            ok=False,
            version_hash=None,
            summary=f"Unhandled error: {exc!r}"[:2000],
            log_tail="",
            duration_s=0.0,
        )


# ---------------------------------------------------------------------------
# Entry point (R17)
# ---------------------------------------------------------------------------


def main() -> None:
    """Compile whatever is waiting, then exit."""

    async def _run() -> None:
        supabase = await create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_KEY.get_secret_value(),
        )
        redis = aioredis.from_url(settings.REDIS_URL)
        try:
            outcome = await compile_next(supabase, redis)
            if outcome is None:
                logger.info("No IN_SPRINT features to compile")
            elif outcome.ok:
                logger.info(
                    "Compiled %s → %s (%.1fs)",
                    outcome.feature_id,
                    outcome.version_hash,
                    outcome.duration_s,
                )
            else:
                logger.info(
                    "Compile failed for %s: %s (%.1fs)",
                    outcome.feature_id,
                    outcome.summary,
                    outcome.duration_s,
                )
        finally:
            await redis.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()