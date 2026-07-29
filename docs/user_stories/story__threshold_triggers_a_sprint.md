<!-- pdd-story-prompts: config_python.prompt, constants_python.prompt, sprint_service_python.prompt, architect_python.prompt -->
<!-- pdd-story-dev-units: config_python.prompt, constants_python.prompt, sprint_service_python.prompt, architect_python.prompt -->

# User Story: threshold_triggers_a_sprint

**ID:** US-07

## Story

As a community,
I want a feature that clears the vote threshold to enter a build sprint on its own,
so that popularity converts into shipped code without a human gatekeeper deciding.

## Acceptance criteria

- A sprint runs on a regular cadence (e.g. daily) and picks **the single highest-voted** `VOTING`
  feature. One winner per sprint: the build step appends that feature to the target app's prompt
  and regenerates the whole app, so two winners in one cycle means two compiles racing at one
  prompt file and one sandbox.
- Only features at or above the upvote threshold are eligible; if none qualify, the sprint is a no-op
  that records it ran, rather than an error.
- Before a selected feature enters the sprint, the architect agent re-checks it against the target
  app's **current** prompt and answers a different question from the one it answered at intake: not
  "is this one votable thing?" but "can this be built into the app as it stands right now?"
- A feature the architect judges unbuildable moves to `POSTPONED_CONFLICT` with the architect's
  explanation, and its postpone count increments — it is held, never dropped. The community sees the
  reason on the Holding Pattern tab.
- Selected features that clear that gate move to `IN_SPRINT` and the board shows the change live.
- Only one sprint runs at a time; a second trigger while one is in flight is refused rather than
  queued or run concurrently. Two sprints selecting the same feature would compile it twice.
- A feature that could not be judged waits its turn rather than being tracked. The selected
  feature leaves `VOTING`, so the eligible pool drains from the top and a deferred feature
  becomes the next candidate on its own — a newcomer with more votes delays it by a cycle, it
  cannot displace it permanently, and it keeps accruing votes while it waits.
- End-of-sprint maintenance rolls unimplemented features back to `VOTING` and decays stale backlog
  items to `ARCHIVED`, where the Vault makes them searchable and rebootable
  (see [reboot_an_archived_request](story__reboot_an_archived_request.md)).

## Notes

Backs `sprint_service` and the sprint-stage entry point of `architect`. Transcript: "once a vote goes
over the threshold… we launch the sprint service."

**There is no HTTP trigger and no button.** A sprint starts on its cadence, or from
`scripts/simulate_sprint.py`, which votes a feature over the threshold as several distinct users and
then runs one sprint. A public "build this now" control would hand any visitor the power to spend a
compile, and the demo needs a repeatable script far more than it needs a button. Single-flight is
therefore enforced by a Redis lock and reported as a refusal to the caller, not as a `409`.

**Why the architect runs twice.** At intake it asks whether a pitch is one votable thing or several
(US-08). Here it asks whether the winning feature can actually be built against the blueprint as it
stands — a question whose answer changes as the target app grows, so an intake verdict from days ago
cannot stand in for it. It uses `LLM_MODEL_ARCHITECT`, the same model pin as the intake stage.

Threshold and cadence come from `UPVOTE_THRESHOLD` and `SPRINT_CADENCE_SECONDS`. The demo overrides
the threshold in `.env` (currently `5`) because the shipped default of 10 is unreachable while a
judge is watching.
