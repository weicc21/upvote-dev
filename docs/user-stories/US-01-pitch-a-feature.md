# US-01 — Pitch a feature for the target app

**As a** community member of the target app
**I want** to submit a feature idea in a short form (title + description)
**So that** my idea enters the public queue instead of dying in a chat thread.

## Acceptance criteria

- A submit form accepts a title and a description and returns immediately (`202`) with a `feature_id`.
- The pitch is queued for AI screening; it does not appear on the public board until it survives screening.
- Submission requires no account setup ceremony — an identified user is enough.
- A per-author daily pitch limit (Pitch Coins) prevents flooding; exceeding it returns a clear "out of coins" response, not a silent drop.
- The author sees a pending state for their pitch while screening runs.

## Notes

Backs `POST /api/features`, `submit_modal`, and the `feature_intake` Redis queue.
