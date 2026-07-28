<!-- pdd-story-prompts: features_python.prompt, ingestion_service_python.prompt -->
<!-- pdd-story-dev-units: features_python.prompt, ingestion_service_python.prompt -->

# User Story: track_my_pitches

**ID:** US-06

## Story

As a pitch author,
I want a private view of every pitch I submitted and its outcome,
so that I know whether my idea was rejected, merged, queued, or shipped.

## Acceptance criteria

- A "my pitches" view lists the author's pitches across all outcomes, including ones never made public.
- It is a dialog opened from the masthead, marked as private to the author, and it never renders inline with the public feed. Unscreened text sitting in the same column as the board is one CSS mistake away from being published, which is the failure screening exists to prevent.
- The masthead control carries a count of unresolved pitches, so an author knows there is something to look at without opening it.
- Each entry shows the outcome: pending screening, rejected (with reason category), merged into another feature, voting, in sprint, or shipped.
- Merged pitches link to the canonical feature that absorbed them.
- Rejected pitch records are visible to the author for a limited window, then expire.

## Notes

Backs `my_pitches_modal` and the `pending_pitch:{author_id}:{feature_id}` records.

The criteria added for the forum UI (coin balance and countdown, stage naming, the four
sections, stage filtering, Vault search, the private pitches dialog, the preview's refresh and
loading states) are **presentation-layer only**. They are satisfied by `frontend/src/**` against
the API as it already stands — `view`, `sort`, `status`, `q`, `cursor` are existing query
parameters and `429` is the existing rate-limit response. No backend or orchestration prompt
acquires an obligation from them, and none needs regenerating.
