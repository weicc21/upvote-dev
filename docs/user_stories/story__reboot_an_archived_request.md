<!-- pdd-story-prompts: app_shell_typescriptreact.prompt, feature_card_typescriptreact.prompt -->
<!-- pdd-story-dev-units: app_shell_typescriptreact.prompt, feature_card_typescriptreact.prompt -->

# User Story: reboot_an_archived_request

**ID:** US-16

## Story

As a community member who still wants an idea that went quiet,
I want to restart the voting window on an archived request,
so that a good idea that arrived at the wrong moment gets a second run instead of being buried forever.

## Acceptance criteria

- Every archived request in the Vault carries a visible reboot control. An archive with no way out is
  a graveyard, and the whole promise of the Vault is that nothing is thrown away.
- Rebooting returns the request to `VOTING` with a fresh voting window, and it reappears on the
  pipeline board where people can vote for it again.
- The reboot is confirmed to the person who pressed it — the request moving out of the tab they are
  looking at is not, on its own, legible as success.
- A rebooted request keeps its title, description, author, and history. It is the same idea getting
  another run, not a new pitch, so it must not cost a Pitch Coin or re-enter screening.
- Its vote count restarts from the reboot rather than resuming an old total, so the new window
  measures current demand rather than crediting interest from months ago. It restarts at **one** —
  the vote of whoever revived it — exactly as a fresh pitch starts with its author's vote, and that
  vote is recorded like any other rather than being a bare number.
- Rebooting requires an identified user but not the original author. The point is present demand: if
  only the author could revive an idea, one whose author has moved on stays archived no matter how
  many people now want it.
- Rebooting something that is not archived is refused as `not_archived` rather than silently
  succeeding — the control only ever appears on the Vault, so a request to revive a live feature
  means the client is out of date.

## Notes

Backs the `reboot-btn` control on `VaultCard` and the reboot handler in `app_shell`.

**Both halves are built.** `VaultCard` renders the control, and `POST /api/features/{id}/reboot`
(`backend/routes/lifecycle.py`) persists it: the row returns to `VOTING`, `created_at` resets, the
count restarts at 1 with the reviver's vote, and prior votes are cleared. The shell only drops the
row from the Vault once the server confirms.

**The backend half is `POST /api/features/{id}/reboot`**, already declared in the frozen
`openapi.yaml`: "ARCHIVED -> VOTING; resets created_at and upvotes to 1; no re-enqueue", returning
the updated `Feature` or `422 not_archived`. Resetting `created_at` is what restarts the window —
both the "newest" sort and the sprint's decay measure from it, so a revived request is genuinely
back at the start of the queue rather than instantly stale.

Rebooting is deliberately open to anyone rather than to the original author. The point is present
demand, not authorship — if only the author can revive an idea, an archived request whose author has
moved on stays archived no matter how many people now want it.
