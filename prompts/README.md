# prompts — the source of this repository

The `.prompt` files in this directory are the source code. The Python and TypeScript under
`backend/`, `orchestrator/`, `frontend/`, and `shared/` are generated *output* — reproducible from
these prompts and, by design, not the thing you edit.

This is [Prompt-Driven Development](https://github.com/promptdriven/pdd/blob/main/docs/prompting_guide.md)
as practised with the [`pdd`](https://promptdriven.ai/) CLI.

## The pipeline

```
docs/user_stories/story__*.md    behaviour, human-verifiable, source of truth
        │
        ▼   pdd generate --template architecture/architecture_json
architecture.json                module decomposition: paths, dependencies, priority
        │
        ▼   pdd generate --template generic/generate_prompt   (one per module)
prompts/<area>/<module>_<lang>.prompt
        │
        ▼   pdd sync <module>
generated code + tests
```

## The one rule

**Never hand-patch generated code.** When behaviour is wrong, edit the prompt and regenerate. If
you already patched the code, back-propagate it at the *behaviour* level:

```bash
pdd update prompts/backend/routes/votes_python.prompt backend/routes/votes.py
```

Write the recovered requirement as an observable outcome, never as a transcription of private
helper names or internal calls.

## One prompt = one module, not one story

A single story fans out across services — US-01 "pitch a feature" touches the submit modal, the API
client, the intake route, and the shared constants — and one module serves several stories. That
many-to-many mapping lives in each story's `pdd-story-prompts` header, written by `pdd story link`
and verified by `pdd detect --stories`.

## Layout

`.pddrc` maps each prompt glob to an output directory:

| Prompts | Generated into |
|---|---|
| `prompts/shared/**` | `shared/` |
| `prompts/backend/**` | `backend/` |
| `prompts/orchestration/**` | `orchestrator/` |
| `prompts/frontend/**` | `frontend/src/` |

Naming: `prompts/<area>/<module>_<lang>.prompt`, lowercase language suffix (`_python`,
`_typescript`, `_typescriptreact`), `snake_case` basenames.

Two files here are **frozen assets** — context for generation, never generated themselves:

- `shared/openapi.yaml` — the API contract. Request and response shapes, error envelopes, and
  query parameter names come from here, not from a module's own judgement.
- `frontend/design_guide.md` — the visual identity. No new tokens, fonts, or CSS frameworks;
  `styles.css` is a versioned artifact.

## Anatomy of a prompt

Start from the minimal skeleton and add a section only when its risk actually fires:

| Section | Always? | Purpose |
|---|---|---|
| `<pdd-reason>` | yes | One line on why the module exists |
| `<pdd-interface>` | yes | The frozen public surface, as JSON |
| `<pdd-dependency>` | when it has any | Which prompts this one builds on |
| `<responsibility>` | yes | The single job, in a sentence |
| `<non_responsibilities>` | when scope could blur with a neighbour | What this module explicitly does *not* do |
| `<vocabulary>` | when a term could be read two ways | Definitions the rules rely on |
| `<capabilities>` | when it touches Redis, Postgres, an LLM, or the network | `MAY` / `MUST NOT` on external effects |
| `<contract_rules>` | yes | Numbered `R<n>` with `MUST` / `MUST NOT` |
| `<coverage>` | yes | Each rule mapped to a story file or an honest `TODO` |
| `<pdd>` | rarely | Human-only note; stripped before the model sees it |

Target **10–30% of expected code size**. Under that is too vague; over it means implementation
detail has crept in. Specify interfaces, invariants, and outcomes — let the model decide *how*.

### Verify structure without spending anything

```bash
pdd contracts check prompts/backend/routes/votes_python.prompt
```

Aim for `0 error(s)`. Remaining `UNCHECKED_RULE` warnings are the test backlog, not noise to
silence. Three formatting rules the checker enforces that are easy to trip:

- **One `<contract_rules>` block per prompt.** Splitting rules across two blocks silently drops
  every rule in the first one and hard-fails its coverage entries.
- **One rule per `<coverage>` line.** `R1, R2: TODO …` registers only `R1`.
- **One claim per line in `<non_responsibilities>`,** each with a modal verb (`DOES NOT`,
  `MUST NOT`). The checker is line-based, so a wrapped sentence fails.

## Generating code

Work in `architecture.json` priority order so each module's dependencies exist first.

```bash
export PDD_COMMAND_MAX_OUTPUT_TOKENS=32000   # required — see CLAUDE.md

pdd sync constants --dry-run                 # inspect the plan
pdd sync constants                           # then config, deps, features, votes, …
```

`pdd sync` runs generate → example → test → verify → fix, honouring `target_coverage`, `budget`,
and `max_attempts` from `.pddrc`.

**Every generating command accepts `--estimate`** to price the call without making it. Use it on
any command shape you have not run before.

## Authoring a new prompt

```bash
python3 scripts/pdd_prep.py prompts/backend/routes/votes_python.prompt --stories US-04
```

That builds the two per-module inputs and prints the `pdd generate` command. It exists because
passing the whole architecture costs ~15k input tokens per call for no benefit, and because
`generic/generate_prompt` has **no slot for story files** — without a story pack the generator
never sees an acceptance criterion, only a one-line module description.

Generated prompts arrive 2–3× too long. Budget for an edit pass every time: strip the
`Instructions` / `Testing notes` / `Deliverable` / `Implementation assumptions` sections and any
`<web>` tag, then check invented defaults against the stories and `schema.sql`.

Full step-by-step workflow, including the failure modes worth knowing before you spend money:
**[`docs/pdd-prompt-authoring.md`](../docs/pdd-prompt-authoring.md)**.

## Contributing

1. Change the **prompt**, never the generated file.
2. `pdd contracts check <prompt>` → `0 error(s)`.
3. Regenerate with `pdd sync <module>` and let the tests run.
4. Link the prompt to the stories it serves:
   `pdd story link docs/user_stories/story__<name>.md --prompt <prompt>` (free, no model call).
5. Keep `architecture.json`'s `interface` in sync with the prompt's `<pdd-interface>`.

Read `docs/pdd-prompt-authoring.md` before authoring