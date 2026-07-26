# US-04 — Upvote the features I want built

**As a** community member
**I want** to upvote feature requests on a public board
**So that** the roadmap reflects what users actually want, not what one person guessed.

## Acceptance criteria

- One vote per user per feature; a repeat vote is rejected (`409`), not double-counted.
- The vote count updates immediately in the UI and is reflected for other viewers live.
- Voting requires no more friction than a single click.
- Vote totals are the sole input to what gets built next (see US-07).

## Notes

Backs `POST /api/features/{id}/upvote`, `upvote_button`, the `(feature_id, user_id)` unique constraint.
