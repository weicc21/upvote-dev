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
Page background: `--void` plus two fixed radial gradients (lilac at 80% −10%, coral at −10% 30%) — reproduce exactly.

## Hard Rules
1. **The design-system stylesheet (`styles.css`) is a versioned, frozen artifact.** The compiler imports it unchanged — it is never regenerated from this description. Feature work may add component-scoped classes that reference only the existing custom properties above.
2. **No new visual vocabulary.** The compiler must not introduce new hex values, fonts, font weights, gradients, shadows, border radii, or animation styles. New UI composes existing tokens and existing primitives (card, pill, upvote button, tab, modal, toast, ticker). Adding a primitive or token requires a human-approved edit to this section first — never model improvisation.
3. **No CSS frameworks or UI libraries** (Tailwind, Bootstrap, MUI, shadcn, etc.) unless declared in this section.
4. **Layout skeleton is frozen**: broadcast chyron on top → masthead (brand left, actions right) → two-column grid (fluid feed + 360px sticky sandbox panel, 26px gap) → responsive collapse at 960px (single column) and 560px (compact cards). Do not reorganize.
5. **Motion is bounded**: only the existing animations (live-dot blink, wand wiggle, loading ring, green flash, burst particles, pulse glow, shield scan, modal pop, toast rise) and always behind `prefers-reduced-motion`.
6. **Verification gate**: any recompile or compiler-model change must pass a visual regression check (screenshot diff of the four tabs, modal, and sandbox panel against the last approved build) before promotion, and never combined with a model upgrade in a single change.
7. **Testability & unit tests.** Use **Vitest** (Vite-native — reuses the app's own Vite config/transform, faster than Jest, zero extra setup) with **React Testing Library** + **jsdom**. Components are tested against a **stubbed `api_client`** (the single seam to the backend) or a spec-driven mock of `openapi.yaml` — never a live backend. Supabase Realtime is mocked by invoking the `onFeatureInsert`/`onFeatureUpdate`/`onBroadcastEvent`/`onDeployment` callbacks directly. The frozen `styles.css` / Design Lock is **not** unit-tested — that's the separate screenshot-diff concern in Rule 6. No unit test hits a live backend or Realtime.