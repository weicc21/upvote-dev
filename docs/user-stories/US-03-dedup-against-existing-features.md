# US-03 — Compare new pitches against what the app already does

**As a** community member
**I want** my pitch checked against the app's current features and the existing board
**So that** votes concentrate on one canonical request instead of scattering across duplicates.

## Acceptance criteria

- A PM agent inspects the target app's **current** feature set and the **incoming** pitches in the same pass.
- Each surviving pitch is classified as: new & unique, duplicate (merged into an existing request), an extension of a shipped feature, or already shipped.
- Merged duplicates transfer their upvote to the canonical feature rather than creating a new row.
- "Already shipped" pitches are closed with that reason rather than sitting on the board.
- Only new/unique and extension pitches reach the public board, at status `VOTING`.

## Notes

Backs `orchestrator/pm_agent.py`. Transcript: "inspect the current features of the app versus the new incoming features."
