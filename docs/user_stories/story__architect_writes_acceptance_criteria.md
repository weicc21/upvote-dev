<!-- pdd-story-prompts: architect_python.prompt, ingestion_service_python.prompt, sprint_service_python.prompt -->
<!-- pdd-story-dev-units: architect_python.prompt -->

# User Story: architect_writes_acceptance_criteria

**ID:** US-08

## Story

As a system,
I want each selected feature translated from a loose pitch into acceptance criteria and a feature block,
so that a vague community request becomes something a compiler can actually build.

## Acceptance criteria

- The architect re-validates the feature against the target app's own prompt file — its binding architecture constraints, UI paradigm, and existing features — before anything is written (friction analysis: GREEN / YELLOW / RED).
- Friction is judged on four axes: **architecture constraint** (does it need capabilities the app forbids), **UI/UX overlap** (does it fight the committed layout paradigm), **logical contradiction** (would it invalidate a feature already committed), and **code merge friction** (can it be added without reworking what exists).
- Output is a structured feature block with explicit acceptance criteria, written in the target app's prompt language.
- A feature that bundles several independently wantable capabilities is **split** into 2–3 sub-features that go back to the board for their own votes. The test is the vote, not the code size: a bundled card forces an all-or-nothing choice and leaves the sprint unable to tell which part the community wanted.
- A feature that conflicts with the current app is **postponed** with a stated reason rather than force-built, and the reason is visible to the community rather than kept internal.
- After a bounded number of splits or postponements, the feature is either compiled or archived — it never loops forever.

## What counts as a conflict

The target app is a single-file, dependency-free app regenerated in full from one prompt, so it is
unusually easy to change. A feature is postponed only when the app as designed cannot hold it: it
needs a capability the architecture forbids, it fights the committed UI paradigm, or it would
invalidate a feature already there. A feature that merely needs something not yet built is
additive, not conflicting — the compiler rewrites the whole file, so it brings the prerequisite
with it.

The conflict that matters is an **implementation** one: the app committed to a mechanism, and the
feature needs that mechanism to behave in a way it cannot. If an app integrates one payment
provider and a pitch asks for a payment type that provider's interface cannot express, satisfying
it means breaking the committed integration — that is a conflict. "We have no payments yet" is
not. The test is whether a decision already made has to be undone.

## Two stages

The architect runs twice on a feature's life, and the two are separate:

1. **Shape, at intake** (`decide_shape`) — immediately after dedup, every surviving pitch is
   friction-checked against the target app and lands as `VOTING`, `SPLIT`, or
   `POSTPONED_CONFLICT`. The community never votes on something that cannot be built, and never
   votes on one card that is really three.
2. **Spec, at sprint time** (`build_spec`) — only the feature that clears the vote threshold is
   translated into a feature block with acceptance criteria in the target app's prompt language.

Writing a spec for every pitch would spend a reasoning-model call on features that will never win
a vote; deciding shape only at sprint time would let the board fill with unbuildable cards.

## Notes

Backs `orchestrator/architect.py` (`decide_shape` / `build_spec`) and `system_architect_agent`. Transcript: "it's up to the architect… translate into the prompt requirements."
