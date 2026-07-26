# User stories

One story per file, derived from the product walkthrough transcript and grounded in the
current `prompts/` + `orchestrator/` + `backend/` implementation.

## The loop (pitch → vote → build → ship)

| # | Story | Actor |
|---|---|---|
| [01](US-01-pitch-a-feature.md) | Pitch a feature for the target app | Community member |
| [02](US-02-ai-screens-incoming-pitches.md) | AI screens incoming pitches before they go public | System |
| [03](US-03-dedup-against-existing-features.md) | Compare new pitches against what the app already does | System |
| [04](US-04-upvote-a-feature.md) | Upvote the features I want built | Community member |
| [05](US-05-browse-the-pipeline-board.md) | Browse the public board sorted by demand | Visitor |
| [06](US-06-track-my-pitches.md) | Track what happened to my own pitches | Pitch author |
| [07](US-07-threshold-triggers-a-sprint.md) | Top-voted features automatically trigger a build sprint | System |
| [08](US-08-architect-writes-acceptance-criteria.md) | Architect agent turns a winning pitch into a buildable spec | System |
| [09](US-09-compile-spec-into-source-code.md) | Compile the spec into real source code | System |
| [10](US-10-preview-the-shipped-feature.md) | See and use the feature I voted for, live | Voter |
| [11](US-11-watch-the-agents-work.md) | Watch the AI pipeline work in real time | Visitor |
| [12](US-12-decision-log.md) | Understand why the AI decided what it decided | Community / maintainer |

## Outcome stories (why the product exists)

| # | Story | Actor |
|---|---|---|
| [13](US-13-prioritise-an-overwhelming-backlog.md) | Cut through an overwhelming issue backlog | Maintainer |
| [14](US-14-demand-signal-instead-of-user-research.md) | Get a demand signal without running user research | PM / solo builder |
| [15](US-15-working-prototype-instead-of-mockups.md) | Validate with a working prototype instead of a design mockup | PM / designer |
