# User Story: prioritise_an_overwhelming_backlog

**ID:** US-13

## Story

As a maintainer drowning in issues and community requests,
I want requests consolidated and ranked by real user demand,
so that I know what to build next instead of guessing at a pile of undifferentiated tickets.

## Acceptance criteria

- Duplicate and near-duplicate requests are collapsed into one entry with a combined vote count (see [dedup_against_existing_features](story__dedup_against_existing_features.md)).
- The board answers "what is most wanted right now?" in one view, without manual triage.
- A request too large to build in one go is shown as its parts, each votable on its own and each showing progress toward the votes that unlock it. A part with no threshold set yet shows its count without inventing a target — a fake goal of zero would render unbuilt work as already unlocked.
- Stale, unvoted requests decay out of the backlog automatically instead of accumulating forever.
- The maintainer does not have to be in the loop for a request to be triaged, ranked, or closed as already-shipped.

## Notes

Outcome story — the problem this product exists to solve. Transcript: "maintainers are overwhelmed, they see so many issues, they don't know what the priority is."
