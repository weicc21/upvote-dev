<!-- pdd-story-prompts: config_python.prompt, deploy_python.prompt, sandbox_panel_typescriptreact.prompt -->
<!-- pdd-story-dev-units: config_python.prompt, deploy_python.prompt -->

# User Story: preview_the_shipped_feature

**ID:** US-10

## Story

As a voter,
I want an embedded preview of the deployed app once my feature ships,
so that I can immediately see the thing I asked for actually working.

## Acceptance criteria

- On successful deploy, the feature moves to `COMPILED` and a preview URL is attached.
- The board embeds the running app in a sandbox pane, restricted to an allow-list of deploy hosts.
- The preview reflects the newly built feature, not a stale build. A refresh control reloads the embed in place, so a voter picks up a new deploy without reloading the forum, and it announces itself when a build has just landed rather than waiting to be discovered.
- The pane is honest about slow starts: while the embed is still loading it says so, and it gives up waiting rather than spinning forever, because the host sleeps idle instances and a blank white box reads as the app being broken.
- A link opens the running app full-size in a new tab — the embedded pane is deliberately small.
- If no deploy exists yet, the pane shows an explicit empty state rather than a blank frame.

## Notes

Backs `sandbox`, the deploy webhook, `deployments` table, `SANDBOX_ALLOWED_HOSTS`.

The criteria added for the forum UI (coin balance and countdown, stage naming, the four
sections, stage filtering, Vault search, the private pitches dialog, the preview's refresh and
loading states) are **presentation-layer only**. They are satisfied by `frontend/src/**` against
the API as it already stands — `view`, `sort`, `status`, `q`, `cursor` are existing query
parameters and `429` is the existing rate-limit response. No backend or orchestration prompt
acquires an obligation from them, and none needs regenerating.
