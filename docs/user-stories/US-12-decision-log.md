# US-12 — Understand why the AI decided what it decided

**As a** community member or maintainer
**I want** every automated decision recorded with its reason
**So that** an AI-governed roadmap stays accountable and arguable rather than arbitrary.

## Acceptance criteria

- Every agent decision is logged: screening rejections, merges, splits, postponements, compile outcomes, archival.
- Each entry records what was decided, by which agent, on which feature, when, and why.
- Decisions are labelled by type so they can be counted and reviewed over time.
- A rejected or postponed author can see the reason attached to their feature.

## Notes

Backs `decision_log`. Transcript: "start labeling this decision… so people can see how it was generated."
