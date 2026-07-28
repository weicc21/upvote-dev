# User Story: watch_the_agents_work

**ID:** US-11

## Story

As a visitor,
I want a live ticker of what the agents are doing right now,
so that the build process feels transparent and alive instead of a black box.

## Acceptance criteria

- Agents emit human-readable progress events tagged by phase: screening, synthesizing, architecting, compiling, deployed.
- Events stream to every open board without a refresh.
- Event copy names the agent and describes the step in plain language.
- Pitch content never appears in the public ticker — only phase and micro-copy.

## Notes

Backs `agent_events` pub/sub → `event_relay` → `broadcast_events` → `broadcast` component.
