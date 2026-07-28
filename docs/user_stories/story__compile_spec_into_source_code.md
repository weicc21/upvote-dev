# User Story: compile_spec_into_source_code

**ID:** US-09

## Story

As a system,
I want the accepted feature block written into the target app's prompt file and compiled,
so that the community's vote produces working code, not a ticket.

## Acceptance criteria

- The feature block is appended to the target app's prompt file as a single edit.
- A compile is run against that prompt to regenerate the target app's source.
- Compile failures are captured and surfaced; the feature does not silently claim success.
- On failure the feature returns to `VOTING` rather than being lost.
- The whole path — pitch to compiled code — requires no human code review step to proceed.

## Notes

Backs `compiler_writer` + `TARGET_PROMPT_DIR` / `COMPILE_COMMAND`. Transcript: "one prompt file… creates the source code."
