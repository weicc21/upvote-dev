<!-- pdd-story-prompts: config_python.prompt, constants_python.prompt -->
<!-- pdd-story-dev-units: config_python.prompt, constants_python.prompt -->

# User Story: threshold_triggers_a_sprint

**ID:** US-07

## Story

As a community,
I want a feature that clears the vote threshold to enter a build sprint on its own,
so that popularity converts into shipped code without a human gatekeeper deciding.

## Acceptance criteria

- A sprint runs on a regular cadence (e.g. daily) and picks the highest-voted `VOTING` features.
- Only features at or above the upvote threshold are eligible; if none qualify, the sprint is a no-op.
- Selected features move to `IN_SPRINT` and the board shows the change live.
- Only one sprint runs at a time; a second trigger while one is in flight is refused (`409`).
- End-of-sprint maintenance rolls unimplemented features back to `VOTING` and decays stale backlog items.

## Notes

Backs `POST /sprint`, `sprint_service`, `lifecycle`. Transcript: "once a vote goes over the threshold… we launch the sprint service."
