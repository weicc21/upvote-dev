# US-05 — Browse the public board sorted by demand

**As a** visitor
**I want** a public board of feature requests I can sort by popularity and filter by stage
**So that** I can see at a glance what the community wants and where each idea is in the pipeline.

## Acceptance criteria

- The board is readable without an account.
- Sorting by top (most upvoted) and by newest is supported, with pagination.
- Each card shows title, description, upvote count, and current stage (`VOTING`, `IN_SPRINT`, `COMPILED`, …).
- Cards advance through stages live, without a manual refresh.

## Notes

Backs `GET /api/features?view=pipeline&sort=top`, `cards`, `app_shell`, Supabase Realtime.
