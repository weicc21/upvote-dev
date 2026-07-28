"""Frozen vocabulary shared by backend, orchestrator, and frontend.

This module is the single source of truth for enum labels, table names,
Redis key templates, and tunable defaults. It performs no I/O and imports
nothing outside the standard library.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

# ---------------------------------------------------------------------------
# Postgres enum types — values are byte-for-byte matches of schema.sql labels
# ---------------------------------------------------------------------------


class FeatureStatus(StrEnum):
    """feature_status enum in Postgres. Uppercase values."""

    VOTING = "VOTING"
    CONSOLIDATING = "CONSOLIDATING"
    IN_SPRINT = "IN_SPRINT"
    SPLIT = "SPLIT"
    COMPILED = "COMPILED"
    POSTPONED_CONFLICT = "POSTPONED_CONFLICT"
    ARCHIVED = "ARCHIVED"


class BroadcastPhase(StrEnum):
    """broadcast_phase enum in Postgres. Lowercase values."""

    SCREENING = "screening"
    SYNTHESIZING = "synthesizing"
    ARCHITECTING = "architecting"
    COMPILING = "compiling"
    DEPLOYED = "deployed"


class DecisionPhase(StrEnum):
    """decision_phase enum — the ``decision_log.phase`` column. Lowercase values."""

    SCREENING = "screening"
    DEDUP = "dedup"
    FRICTION = "friction"
    COMPILE = "compile"
    DEPLOY = "deploy"
    LIFECYCLE = "lifecycle"


class BuildStatus(StrEnum):
    """build_status enum in Postgres. Lowercase values."""

    SUCCESS = "success"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Non-Postgres enums — travel on Redis / inside JSON, never as PG enum types
# ---------------------------------------------------------------------------


class DecisionType(StrEnum):
    """Classification recorded inside ``decision_log.decision`` JSON payload.

    This is **not** a Postgres enum — it is a different axis from
    :class:`DecisionPhase`.
    """

    SCREENING_REJECT = "screening_reject"
    MERGE = "merge"
    SPLIT = "split"
    POSTPONE = "postpone"
    COMPILE_SUCCESS = "compile_success"
    COMPILE_FAILURE = "compile_failure"
    ARCHIVAL = "archival"
    ALREADY_SHIPPED = "already_shipped"


class RejectionReason(StrEnum):
    """Every way a pitch resolves without becoming its own public row.

    ``MERGED`` is a dedup outcome rather than a rejection (US-03). These
    values travel on the Redis ``screening_results`` channel and never
    reach Postgres.
    """

    SECURITY = "security"
    OFF_TOPIC = "off_topic"
    UNCLEAR = "unclear"
    ALREADY_SHIPPED = "already_shipped"
    MERGED = "merged"


# ---------------------------------------------------------------------------
# Table names — verbatim matches of schema.sql CREATE TABLE identifiers
# ---------------------------------------------------------------------------

TABLE_FEATURE_REQUESTS: Final[str] = "feature_requests"
TABLE_FEATURE_VOTES: Final[str] = "feature_votes"
TABLE_BROADCAST_EVENTS: Final[str] = "broadcast_events"
TABLE_DEPLOYMENTS: Final[str] = "deployments"
TABLE_DECISION_LOG: Final[str] = "decision_log"
TABLE_BUILD_LOGS: Final[str] = "build_logs"

# ---------------------------------------------------------------------------
# Redis key names / templates
# ---------------------------------------------------------------------------

REDIS_FEATURE_INTAKE: Final[str] = "feature_intake"
REDIS_AGENT_EVENTS: Final[str] = "agent_events"
REDIS_SCREENING_RESULTS: Final[str] = "screening_results"

# Format templates — contain named placeholders, not pre-formatted.
REDIS_PENDING_PITCH: Final[str] = "pending_pitch:{author_id}:{feature_id}"
REDIS_PITCH_RATE: Final[str] = "rate:pitch:{author_id}"

# ---------------------------------------------------------------------------
# Tunable defaults — every one is overridable via environment in shared/config
# ---------------------------------------------------------------------------

DEFAULT_PITCH_COIN_LIMIT: Final[int] = 5
DEFAULT_UPVOTE_THRESHOLD: Final[int] = 10
DEFAULT_SPRINT_CADENCE_SECONDS: Final[int] = 86_400  # daily
DEFAULT_MAX_RETRIES: Final[int] = 3
DEFAULT_PENDING_PITCH_TTL_SECONDS: Final[int] = 900  # 15 minutes

# LLM screening defaults
DEFAULT_LLM_MODEL_SCREENING: Final[str] = "MiniMax-M2.5-highspeed"
DEFAULT_LLM_TEMPERATURE: Final[float] = 0.2
DEFAULT_LLM_TIMEOUT_SECONDS: Final[int] = 30
DEFAULT_LLM_MAX_ATTEMPTS: Final[int] = 2