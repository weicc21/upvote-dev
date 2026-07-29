<!-- pdd-story-prompts: event_relay_python.prompt, broadcast_typescriptreact.prompt, api_client_typescript.prompt, app_shell_typescriptreact.prompt -->
<!-- pdd-story-dev-units: event_relay_python.prompt, broadcast_typescriptreact.prompt -->

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

- The ticker names each agent — Guardagent, PM Agent, Architect Agent, Janitor Agent, Ship Agent — and walks them in pipeline order, so the strip doubles as an explanation of how the system works.
- A "shipped" event is visibly different from the rest and points the visitor at the sandbox preview, because a build landing is the one event worth interrupting a scroll for.

## Notes

Backs `agent_events` pub/sub → `event_relay` → `broadcast_events` → `broadcast` component.

The `broadcast` component currently cycles a scripted array covering those five agents. The live
feed replaces the array through the component's `messages` prop and changes no markup — the ticker
is built, its data source is the part still owed.
