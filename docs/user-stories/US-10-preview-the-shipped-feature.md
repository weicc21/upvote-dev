# US-10 — See and use the feature I voted for, live

**As a** voter
**I want** an embedded preview of the deployed app once my feature ships
**So that** I can immediately see the thing I asked for actually working.

## Acceptance criteria

- On successful deploy, the feature moves to `COMPILED` and a preview URL is attached.
- The board embeds the running app in a sandbox pane, restricted to an allow-list of deploy hosts.
- The preview reflects the newly built feature, not a stale build.
- If no deploy exists yet, the pane shows an explicit empty state rather than a blank frame.

## Notes

Backs `sandbox`, the deploy webhook, `deployments` table, `SANDBOX_ALLOWED_HOSTS`.
