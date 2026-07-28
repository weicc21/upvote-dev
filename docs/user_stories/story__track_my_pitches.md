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
- Each entry shows the outcome: pending screening, rejected (with reason category), merged into another feature, voting, in sprint, or shipped.
- Merged pitches link to the canonical feature that absorbed them.
- Rejected pitch records are visible to the author for a limited window, then expire.

## Notes

Backs `my_pitches_modal` and the `pending_pitch:{author_id}:{feature_id}` records.
