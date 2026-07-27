# PRD — Community Feature Voting Portal

**Status:** scope document for `pdd generate --template architecture/architecture_json`.
Behaviour detail is **not** duplicated here — it lives in the 15 stories under
`docs/user_stories/`, which are the source of truth. This file gives the architecture
template the shape of the system: actors, the end-to-end loop, boundaries, and what is
explicitly out of scope.

## Problem

A maintainer of an app receives more feature requests than they can triage. Requests
arrive as duplicates across chat threads and issue trackers, carry no reliable demand
signal, and validating any one of them costs a design-and-build cycle.

## Product

A public board where a community pitches features, votes on them, and the winning
features are specified, built, and deployed by AI agents with no human in the loop —
then shown back to the voters as a running app they can click.

## Actors

| Actor | Uses the system to |
|---|---|
| Community member | Pitch a feature, upvote features, track their own pitches |
| Visitor | Read the board, watch the live agent ticker, use the deployed preview |
| Maintainer / PM | Read a demand-ranked roadmap; read the decision log |
| Agents (system) | Screen, dedup, select, specify, compile, deploy |

## The loop

1. **Pitch** — a member submits title + description. Accepted immediately (`202`), rate-limited
   per author per day ("Pitch Coins"), and queued. Nothing is public yet. (US-01)
2. **Screen** — a daemon filters each pitch for relevance, safety, and coherence. Rejections are
   categorised and held for the author only, with a TTL. Rejected content never reaches
   Postgres. (US-02)
3. **Dedup** — a PM agent compares survivors against the target app's shipped features *and*
   the live board: new/unique, duplicate (merged, transferring its upvote), extension, or
   already-shipped. Only new/unique and extensions reach the board at `VOTING`. (US-03)
4. **Vote** — one vote per user per feature, one click, live counts for every viewer. Vote
   totals are the only input to what gets built. (US-04, US-05)
5. **Sprint** — on a cadence, the highest-voted features at or above threshold move to
   `IN_SPRINT`. One sprint at a time. End-of-sprint maintenance rolls back unimplemented
   features and decays stale ones. (US-07)
6. **Specify** — an architect agent runs friction analysis (GREEN/YELLOW/RED) and emits a
   feature block with explicit acceptance criteria, or splits an oversized feature back onto
   the board, or postpones a conflicting one — with a bounded number of retries before
   archival. (US-08)
7. **Compile** — the feature block is appended to the target app's prompt file and compiled.
   Failures are surfaced and return the feature to `VOTING`. (US-09)
8. **Ship** — on successful deploy the feature reaches `COMPILED` with a preview URL, embedded
   in a host-allow-listed sandbox pane on the board. (US-10)

Running throughout: a live agent ticker carrying phase + micro-copy but never pitch content
(US-11), and a decision log recording every automated decision with its reason (US-12).

## Feature lifecycle

`VOTING → CONSOLIDATING → IN_SPRINT → SPLIT | COMPILED | POSTPONED_CONFLICT | ARCHIVED`

Case-sensitive; no other values or casings exist.

## System boundaries

- Three deployable units — public API, orchestration daemons, frontend SPA — plus a shared
  constants layer. See `docs/tech_stack.md`.
- The API and the orchestrator communicate **only** through Redis queues/pub-sub and shared
  Postgres tables. Never HTTP between them.
- The API never calls an LLM. All agent work happens in the orchestrator.
- Live updates reach the browser through Supabase Realtime and nothing else — there is no
  SSE or WebSocket endpoint. See `docs/tech_stack.md`.
- Unscreened or rejected pitch content lives in Redis under a TTL and never touches Postgres.
- The public ticker carries phase and micro-copy only — never pitch content.

## Non-goals

- No human review, approval, or moderation step anywhere in the loop.
- No account-creation ceremony; an identified (possibly anonymous) session is enough.
- No separate research tooling, panel recruitment, or survey system — votes *are* the signal. (US-14)
- No design-mockup stage; the sprint output is a running build, not a wireframe. (US-15)
- The target app being built is out of scope — this product writes its prompt file and invokes
  its compiler, nothing more.

## Success criteria

A pitch with enough votes reaches a clickable deployed preview with no human intervening at
any step, and every decision made along the way is attributable and readable. (US-13)
