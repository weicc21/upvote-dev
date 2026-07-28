# User Story: architect_writes_acceptance_criteria

**ID:** US-08

## Story

As a system,
I want each selected feature translated from a loose pitch into acceptance criteria and a feature block,
so that a vague community request becomes something a compiler can actually build.

## Acceptance criteria

- The architect re-validates the feature against the target app before writing anything (friction analysis: GREEN / YELLOW / RED).
- Output is a structured feature block with explicit acceptance criteria, written in the target app's prompt language.
- A feature that is too large is **split** into 2–3 smaller sub-features that go back to the board for their own votes.
- A feature that conflicts with the current app is **postponed** with a stated reason rather than force-built.
- After a bounded number of splits or postponements, the feature is either compiled or archived — it never loops forever.

## Notes

Backs `orchestrator/architect.py` (`decide_shape` / `build_spec`) and `system_architect_agent`. Transcript: "it's up to the architect… translate into the prompt requirements."
