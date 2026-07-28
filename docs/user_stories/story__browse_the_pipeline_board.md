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
- A stage is named in the community's language and says which agent is acting — "AI Merging Duplicates", "AI Building", "AI Evolving", "Live in Sandbox" — never the raw enum. A card still open for voting needs no stage label at all: the live vote button is its status.
- The board is split into four sections — the pipeline, a permanent Shipped showcase, a Holding Pattern for ideas the architecture cannot take yet, and a searchable Vault of archived requests — so a visitor can tell "not yet" apart from "no".
- The pipeline can be narrowed to one or more stages from the board itself, and narrowing asks the server rather than hiding rows from the page already loaded.
- The Vault is searchable by title, and an archived request can be rebooted for a second run (see [reboot_an_archived_request](story__reboot_an_archived_request.md)).
- Cards advance through stages live, without a manual refresh.

## Notes

Backs `GET /api/features?view=pipeline&sort=top`, `cards`, `app_shell`, Supabase Realtime.

The criteria added for the forum UI (coin balance and countdown, stage naming, the four
sections, stage filtering, Vault search, the private pitches dialog, the preview's refresh and
loading states) are **presentation-layer only**. They are satisfied by `frontend/src/**` against
the API as it already stands — `view`, `sort`, `status`, `q`, `cursor` are existing query
parameters and `429` is the existing rate-limit response. No backend or orchestration prompt
acquires an obligation from them, and none needs regenerating.
