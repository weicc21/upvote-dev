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
  measures current demand rather than crediting interest from months ago.

## Notes

Backs the `reboot-btn` control on `VaultCard` and the reboot handler in `app_shell`.

**Only the presentation half is built.** The control renders and the shell moves the row to `VOTING`
locally with a toast, but nothing persists: a reload puts the request back in the Vault. This is a
deliberate split, not an oversight — the UI half is what the Vault tab needs to not read as a dead
end, and it is safe to ship ahead of the write because it touches no other flow.

**The backend half is owed** and lands as a status transition on the features route: an endpoint
that moves `ARCHIVED` → `VOTING`, resets the voting window and the vote count, and clears prior
`feature_votes` rows so the new window starts clean. It belongs with US-07, which is where archiving
and voting windows acquire real rules — reboot cannot define "a fresh 30-day window" before
something defines the window. Until then the criteria above describing durability are **not met**,
and this story should be read as one story with a finished front half.

Rebooting is deliberately open to anyone rather than to the original author. The point is present
demand, not authorship — if only the author can revive an idea, an archived request whose author has
moved on stays archived no matter how many people now want it.
