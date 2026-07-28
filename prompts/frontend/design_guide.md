# Role and Purpose
You are the UI/UX layer for the Community Feature VOTING Portal. Your job is to provide an elegant, gamified, and highly transparent dashboard where users collaboratively shape applications. Avoid looking like a rigid project management tool (such as Jira or Kanban); the interface must emphasize momentum, instant feedback, and the magic of co-creation.

# Design Lock (Hard Constraints — read before any component work)
The visual identity is **frozen**: "late-night broadcast studio / arcade" — deep violet world, candy accents, rounded-futuristic display type. These constraints are constants to copy, not inspiration to interpret. They override any compiler model's stylistic defaults and any other instruction in this document that could be read as permission to restyle. If a future model would render this spec differently, the model is wrong and this section wins.

## Canonical Design Tokens (copy byte-for-byte; never regenerate, approximate, or "improve")
```css
:root {
  --void: #150b2e;        /* page background */
  --void-deep: #0e0721;   /* recessed surfaces: ticker, inputs, unlock tree */
  --panel: #211447;       /* card surface */
  --panel-2: #2b1a5c;     /* raised surface: active tab, refresh button */
  --line: rgba(167, 139, 250, 0.16);  /* all borders */
  --text: #f3efff;
  --muted: #a99cd4;
  --coral: #ff5d73;       /* hype/primary action */
  --coral-deep: #d93a56;
  --gold: #ffc53d;        /* momentum, badges, holding pattern */
  --mint: #3df0b6;        /* success/shipped-live states */
  --lilac: #a78bfa;       /* AI/magic states */
  --radius: 18px;
  --font-display: 'Unbounded', system-ui, sans-serif;  /* weights 500/700/900; logo, headings, vote counts only */
  --font-body: 'Outfit', system-ui, sans-serif;        /* weights 400–700 */
  --font-mono: 'Space Mono', ui-monospace, monospace;  /* broadcast ticker, char counters */
}
```
Page background: `--void` plus two fixed radial gradients (lilac at 80% −10%, coral at −10% 30%) — reproduce exactly. The three font families are loaded by `<link>` in `index.html`, not by an `@import` in the stylesheet.

## Hard Rules
1. **The design-system stylesheet (`frontend/src/styles.css`) is a versioned, frozen artifact.** The compiler imports it unchanged — it is never regenerated from this description. Feature work may add component-scoped classes that reference only the existing custom properties above.
2. **No new visual vocabulary.** The compiler must not introduce new hex values, fonts, font weights, gradients, shadows, border radii, or animation styles. New UI composes existing tokens and existing primitives (card, pill, upvote button, tab, modal, toast, ticker). Adding a primitive or token requires a human-approved edit to this section first — never model improvisation.
3. **No CSS frameworks or UI libraries** (Tailwind, Bootstrap, MUI, shadcn, etc.) unless declared in this section.
4. **Layout skeleton is frozen**: broadcast chyron on top → masthead (brand left, actions right) → two-column grid (fluid feed + 360px sticky sandbox panel, 26px gap) → responsive collapse at 960px (single column) and 560px (compact cards). All of it inside a single `.app` wrapper (max-width 1240px). Do not reorganize.
4a. **Rows are rows.** `.feed-controls` and `.filter-chips` are **siblings, stacked** — `.feed-controls` is a flex row and nesting the chips inside it crushes sort and six stage filters onto one line. The same applies to `.tabs`: it is its own row above both. If two groups answer different questions, they get different rows.
4b. **The masthead CTAs are a matched pair.** `🔒 My pitches` is `btn-ghost btn-mypitches` and `🔥 Pitch a feature` is `btn-primary btn-pitch` — both classes on both, always. `btn-mypitches` alone sets only flex alignment: dropping `btn-ghost` leaves the control with no border, padding, or radius, rendering as bare text beside a pill. The stylesheet equalises their height; the compiler must not restyle either.
4c. **The preview embed's height lives in `styles.css`** — never in an inline style or a component constant. An inline `height` on the iframe beats the stylesheet and, resolving against an auto-height wrapper, collapses the frame; this has already shipped once. The current value is 380px (310px once the layout collapses at 960px), tuned by eye: taller pushes the feed below the fold, shorter crops the target app to a strip.
5. **Motion is bounded**: only the existing animations (live-dot blink, broadcast slide-in, wand wiggle, loading ring, green flash, burst particles, pulse glow, shield scan, modal pop, toast rise) and always behind `prefers-reduced-motion`.
6. **Verification gate**: any recompile or compiler-model change must pass a visual regression check (screenshot diff of the four tabs, modal, and sandbox panel against the last approved build) before promotion, and never combined with a model upgrade in a single change.
7. **Testability & unit tests.** Use **Vitest** (Vite-native — reuses the app's own Vite config/transform, faster than Jest, zero extra setup) with **React Testing Library** + **jsdom**. Components are tested against a **stubbed `api_client`** (the single seam to the backend) or a spec-driven mock of `openapi.yaml` — never a live backend. Supabase Realtime is mocked by invoking the `onFeatureInsert`/`onFeatureUpdate`/`onBroadcastEvent`/`onDeployment` callbacks directly. The frozen `styles.css` / Design Lock is **not** unit-tested — that's the separate screenshot-diff concern in Rule 6. No unit test hits a live backend or Realtime.

---

# Class Vocabulary (CLOSED SET — do not invent)

`styles.css` is the authority; this list mirrors it. **A class not on this list does not exist.**
Emitting `className="masthead-title"` when the stylesheet defines `.masthead` and `.brand h1`
produces markup that renders unstyled — this has already happened once and is the single most
common way this UI breaks.

**Naming convention: kebab-case blocks with `is-` state modifiers.** `card-title`, `unlock-node`,
`is-active`, `is-voted`, `is-unlocked`, `is-rejected`, `is-pulsing`, `is-low`, `is-small`,
`is-success`, `is-error`. There is **no BEM** in this system: never emit a `__` or a `--` in a class
name. `card__title`, `tab--active`, `sandbox__frame` and `pill--voting` are all wrong.

| Area | Classes |
|---|---|
| Shell | `app`, `layout`, `feed`, `masthead`, `masthead-actions`, `brand`, `brand-dot`, `tagline` |
| Chyron | `broadcast`, `broadcast-live`, `broadcast-label`, `broadcast-msg` (`is-success`), `live-dot` |
| Buttons | `btn-primary`, `btn-ghost`, `btn-mypitches` (**always with `btn-ghost`**), `btn-pitch` (**always with `btn-primary`**), `btn-dismiss` (with `btn-ghost`), `expand-btn`, `reboot-btn`, `refresh-btn` (`is-pulsing`) |
| Coins / pitches | `coin-chip`, `coin-cost`, `pitch-count`, `pitches-list`, `pitches-empty`, `pending-card` (`is-rejected`), `pending-copy`, `pending-status` (`is-rejected`), `shield` |
| Nav / filters | `tabs`, `tab` (`is-active`), `tab-count`, `feed-controls`, `feed-controls-label`, `sort-toggle`, `filter-chips`, `chip` (`is-active`), `section-blurb`, `vault-search` |
| Card | `card`, `status-{STATUS}`, `card-main`, `card-copy`, `card-meta`, `card-byline`, `card-title`, `card-scope`, `merge-note` |
| Card variants | `card-holding`, `holding-header`, `holding-badge`, `holding-sub`, `ai-explains`, `ai-explains-label`, `card-shipped`, `shipped-credit`, `card-vault` |
| Pills | `pill` + one of `pill-consolidating`, `pill-building`, `pill-live`, `pill-evolving`, `pill-shipped`, `pill-vault`; decorations `wand`, `loading-ring` |
| Voting | `upvote` (`is-voted`, `is-small`), `upvote-arrow`, `upvote-count`, `upvote-word`, `burst`, `vote-frozen` |
| Unlock tree | `unlock-tree`, `unlock-head`, `unlock-parent`, `unlock-progress`, `unlock-node` (`is-unlocked`), `unlock-icon`, `unlock-body`, `unlock-title`, `unlock-bar`, `unlock-count` |
| Sandbox | `sandbox`, `sandbox-head`, `sandbox-sub`, `sandbox-frame-wrap`, `sandbox-skeleton`, `sandbox-newtab`, `sandbox-unavailable` |
| Modal | `modal-backdrop`, `modal`, `modal-head`, `modal-close`, `modal-hint`, `modal-actions`, `field`, `field-label`, `char-count` (`is-low`), `form-error` |
| Feedback | `toast`, `feed-status` (`is-error`), `skeleton`, `visually-hidden` |

Note there is **no `.pill-voting`**: a `VOTING` card carries no pill at all — the live upvote button
*is* its status. Only the non-votable stages announce themselves.

---

# Copy Deck (exact strings — the product's voice lives here)

Copy is part of the frozen identity. Do not paraphrase, re-tone, or "improve" these strings.

**Brand.** Title `upvote·dev` rendered as `upvote<span class="brand-dot">·</span>dev`. Tagline:
`You wish it. Agents ship it.` Document title: `UpvoteDev — You wish it. Agents ship it.`

**Masthead actions.** Coin chip `🪙 {n}` with title `Pitch Coins — each pitch costs 1. Refills daily (2 min in this demo).` · `🔒 My pitches` · `🔥 Pitch a feature` (when out of coins: `🪙 Coins refresh in {m:ss}`).

**Tabs.** `⚡ Up Next Pipeline` · `🏆 Shipped` · `⏸ Holding Pattern` · `🗄️ The Vault`.

**Sort.** Label `Sort by`; options `🔥 Highest Upvotes` (`top`) and `✨ Newest` (`new`).

**Filter chips.** `All` · `🗳️ Voting` · `🪄 AI Merging Duplicates` · `🛠️ AI Building` · `🚀 AI Evolving` · `🎉 Live in Sandbox`. They **multi-select**: each chip toggles independently, `All` clears the set, and an empty set means no filter. Stage filtering is a "show me these kinds" question, and one-at-a-time makes the board answer a narrower question than anyone asked.

**Pipeline control order (top to bottom, each its own row).** `.tabs` → `.feed-controls` (sort) → `.filter-chips` → `.feed`.

**Stage pills.** `CONSOLIDATING` → `🪄 AI Merging Duplicates` (`pill-consolidating`, wand animated) · `IN_SPRINT` → `🛠️ AI Building` (`pill-building`, loading ring) · `COMPILED` → `🎉 Live in Sandbox` (`pill-live`) · `SPLIT` → `🚀 AI Evolving` (`pill-evolving`) · shipped showcase → `🏆 Shipped in {version}` (`pill-shipped`) · vault → `🗄️ In the Vault` (`pill-vault`). `VOTING` has no pill.

**Upvote button.** `▲` · count · `Hype it` / `Hyped!` after voting. Accessible name `Upvote — {n} upvotes` / `Hyped — {n} upvotes`.

**Section blurbs.**
- Shipped: `🏆 {n} features shipped by this community — trophies, not tickets. Everything here stays forever.`
- Holding: `Requests the community loved but the current architecture can't hold yet. Nothing here is rejected — the agents re-check every cycle.`
- Empty pipeline: `No features in this stage right now.`
- Empty vault: `The Vault is empty — every idea is still in play.` / with a query: `Nothing in the Vault matches that search.`
- Vault search placeholder: `Search the Vault…`

**Holding card.** Badge `⏸`; sub-line `Structural Paradox Detected · holding cycle {postpone_count} of 2`; explanation panel labelled `🤖 Why the pause`.

**Vault card.** Scope replaced by `Archived due to low community velocity. If you still want this built, you can reboot the request to restart its 30-day VOTING window.`; action `⚡ Reboot request`.

**Unlock tree.** Toggle `▸ Show unlock tree` / `▾ Hide unlock tree`; head `Evolved from "{parent title}"` and `{unlocked}/{total} unlocked`; per node `{n} / {threshold} votes to unlock` or `Unlocked!`.

**Sandbox.** Heading `Sandbox`, sub `the app you're all building`; skeleton `🔥 Warming up the sandbox…`; refresh `↻ Refresh Preview`, and when a build just landed `✨ Refresh Preview — new build ready!`; link `Open sandbox in new tab ↗`.

**Submit modal.** Title `Pitch a feature`; hint `Precise, implementation-first behaviors get built fastest. The PM Agent merges duplicates, so pitch even if something similar exists.`; fields `Feature title` and `Functional scope`; placeholders `e.g. Emoji reactions on posts` and `Describe the exact behavior: what the user sees, clicks, and gets. Minimum 30 characters.`; cost line `Costs 1 🪙 · {n} left today`; actions `Cancel` and `🔥 Pitch it (1 🪙)`.

**My pitches modal.** Title `My pitches`; hint `🔒 Only you can see these. A pitch joins the public feed once Guardagent clears it.`; empty `Nothing in screening right now. Pitch a feature and track it here while Guardagent checks it over.`; screening row `🛡️ Guardagent is screening your pitch...`.

**Toasts.** `🛡️ Pitched! Guardagent is screening it — track it in My pitches.` · `🎉 Cleared screening — your pitch is live in the feed!` · `🛡️ A pitch didn't clear screening — check My pitches.` · `🪙 Pitch coins refreshed — 5 new coins!` · `🔓 Unlocked: "{title}"` · `⚡ Rebooted! A fresh 30-day VOTING window has started.`

---

# Component Inventory

| Module | File | Owns |
|---|---|---|
| `app_shell` | `frontend/src/app_shell.tsx` | `.app` wrapper, masthead, tabs, sort, filter chips, the four tab bodies, modals, toast |
| `broadcast` | `frontend/src/components/broadcast.tsx` | the chyron |
| `upvote_button` | `frontend/src/components/upvote_button.tsx` | the signature hype button and its burst |
| `feature_card` | `frontend/src/components/feature_card.tsx` | `FeatureCard`, `HoldingCard`, `ShippedCard`, `VaultCard`, unlock tree |
| `sandbox_panel` | `frontend/src/components/sandbox_panel.tsx` | the sticky preview aside |
| `submit_modal` | `frontend/src/components/submit_modal.tsx` | the pitch dialog |
| `my_pitches_modal` | `frontend/src/components/my_pitches_modal.tsx` | the author-only tracking dialog |
| `api_client` | `frontend/src/api_client.ts` | the single backend seam |

# Demo Economy (hackathon settings, deliberately not production)

Five Pitch Coins, one per pitch, refilling **two minutes** after the wallet empties rather than at
midnight — a judge watching a five-minute demo has to be able to see the refill happen. The counter
is client-side only; the backend's `429` is the sole real limit and always wins over the local
number.
