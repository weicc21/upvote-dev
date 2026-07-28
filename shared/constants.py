"""Frozen vocabulary shared by backend, orchestrator, and frontend.

This module declares enums, Redis key names, table names, and default
tunable values.  It is the single source of truth for identifiers that
appear in Postgres schemas, Redis channels, and cross-service payloads.

This module MUST NOT import anything outside the standard library,
read environment variables, open files, or perform any I/O at import time.
It MUST NOT expose mutable module-level state.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

# ---------------------------------------------------------------------------
# Enums — values reproduce Postgres labels byte-for-byte (R1, R8, R9)
# ---------------------------------------------------------------------------


class FeatureStatus(StrEnum):
    """Postgres ``feature_status`` enum — uppercase values."""

    VOTING = "VOTING"
    CONSOLIDATING = "CONSOLIDATING"
    IN_SPRINT = "IN_SPRINT"
    SPLIT = "SPLIT"
    COMPILED = "COMPILED"
    POSTPONED_CONFLICT = "POSTPONED_CONFLICT"
    ARCHIVED = "ARCHIVED"


class BroadcastPhase(StrEnum):
    """Postgres ``broadcast_phase`` enum — lowercase values."""

    SCREENING = "screening"
    SYNTHESIZING = "synthesizing"
    ARCHITECTING = "architecting"
    COMPILING = "compiling"
    DEPLOYED = "deployed"


class DecisionPhase(StrEnum):
    """Postgres ``decision_phase`` enum — lowercase values.

    Used as the ``decision_log.phase`` column.  Not to be confused with
    :class:`DecisionType`, which classifies the decision *inside* the
    ``decision_log.decision`` JSON payload.
    """

    SCREENING = "screening"
    DEDUP = "dedup"
    FRICTION = "friction"
    COMPILE = "compile"
    DEPLOY = "deploy"
    LIFECYCLE = "lifecycle"


class BuildStatus(StrEnum):
    """Postgres ``build_status`` enum — lowercase values."""

    SUCCESS = "success"
    FAILED = "failed"


class DecisionType(StrEnum):
    """Classification recorded inside ``decision_log.decision`` JSON.

    This is **not** a Postgres enum — it is a logical tag that travels in
    JSON payloads.
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
    """Reasons a pitch resolves without becoming a public row.

    Includes ``merged`` (a dedup outcome, not a rejection per se).
    Travels on the Redis ``screening_results`` channel; never reaches
    Postgres.
    """

    SECURITY = "security"
    OFF_TOPIC = "off_topic"
    UNCLEAR = "unclear"
    ALREADY_SHIPPED = "already_shipped"
    MERGED = "merged"


# ---------------------------------------------------------------------------
# Table names — verbatim Postgres identifiers (R2)
# ---------------------------------------------------------------------------

TABLE_FEATURE_REQUESTS: Final[str] = "feature_requests"
TABLE_FEATURE_VOTES: Final[str] = "feature_votes"
TABLE_BROADCAST_EVENTS: Final[str] = "broadcast_events"
TABLE_DEPLOYMENTS: Final[str] = "deployments"
TABLE_DECISION_LOG: Final[str] = "decision_log"
TABLE_BUILD_LOGS: Final[str] = "build_logs"

# ---------------------------------------------------------------------------
# Redis key names / templates (R7)
# ---------------------------------------------------------------------------

REDIS_FEATURE_INTAKE: Final[str] = "feature_intake"
REDIS_AGENT_EVENTS: Final[str] = "agent_events"
REDIS_SCREENING_RESULTS: Final[str] = "screening_results"

# Format templates — contain named placeholders, not pre-formatted.
REDIS_PENDING_PITCH: Final[str] = "pending_pitch:{author_id}:{feature_id}"
REDIS_PITCH_RATE: Final[str] = "rate:pitch:{author_id}"

# ---------------------------------------------------------------------------
# Default tunables (R3)
# ---------------------------------------------------------------------------

DEFAULT_PITCH_COIN_LIMIT: Final[int] = 5
DEFAULT_UPVOTE_THRESHOLD: Final[int] = 10
DEFAULT_SPRINT_CADENCE_SECONDS: Final[int] = 86_400  # daily
DEFAULT_MAX_RETRIES: Final[int] = 3
DEFAULT_PENDING_PITCH_TTL_SECONDS: Final[int] = 900  # 15 minutes