<!-- pdd-story-prompts: constants_python.prompt, features_python.prompt, ingestion_service_python.prompt, votes_python.prompt -->
<!-- pdd-story-dev-units: constants_python.prompt, features_python.prompt, ingestion_service_python.prompt, votes_python.prompt -->

# User Story: browse_the_pipeline_board

**ID:** US-05

## Story

As a visitor,
I want a public board of feature requests I can sort by popularity and filter by stage,
so that I can see at a glance what the community wants and where each idea is in the pipeline.

## Acceptance criteria

- The board is readable without an account.
- Sorting by top (most upvoted) and by newest is supported, with pagination.
- Each card shows title, description, upvote count, and current stage (`VOTING`, `IN_SPRINT`, `COMPILED`, …).
- Cards advance through stages live, without a manual refresh.

## Notes

Backs `GET /api/features?view=pipeline&sort=top`, `cards`, `app_shell`, Supabase Realtime.
