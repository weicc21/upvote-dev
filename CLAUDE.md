# upvote-dev

Community-driven feature voting portal under AI governance. Pitches are screened, deduped,
voted on, specified, compiled, and deployed by agents with no human in the loop.

**Prompts are source; code is generated output.** Never hand-patch a generated file — edit the
prompt and regenerate. If code was already patched, back-propagate at the *behaviour* level
(`pdd update <prompt> <code>`), never by transcribing private helper names.

Step-by-step workflow: `docs/pdd-prompt-authoring.md`.

## pdd environment — read before running any generating command

These four cost three failed runs to discover. None are documented upstream.

**1. `export PDD_COMMAND_MAX_OUTPUT_TOKENS=32000` is required for large generations.**
Unset, `llm_invoke` sends no `max_tokens` at all and inherits the provider's default ceiling,
silently truncating long responses mid-JSON. Despite the name it *raises* the cap.

**2. Models live in `~/.pdd/llm_model.csv`, not env vars.** `PDD_MODEL` / `PDD_PROVIDER` are
ignored unless a matching row exists. Routing is TokenRouter through litellm's OpenAI-compatible
transport, so ids look like `openai/anthropic/claude-opus-4.6`. `base_url` comes from that CSV —
`TOKENROUTER_BASE_URL` in `.env` is unused. With `strength: 0.818` pdd interpolates upward by
`model_rank_score` and keeps lower-ranked rows as automatic fallbacks. Reasoning models
(`opus-5`, `fable-5`) route fine but spend the output budget on thinking and return empty content
at small `max_tokens`.

**3. `pdd generate --template architecture/architecture_json` always fails against TokenRouter.**
pdd converts the template schema into a strict structured-output schema (every property
required), sends it to an endpoint that doesn't enforce schemas, then validates the reply against
the strict version — so any omitted *optional* field aborts the run with
`'position' is a required property`. Exit 2, no file written, **but the model output is intact in
the log.** Always redirect stdout and recover rather than paying twice:
`grep "Content attempted for parsing" run.log`. Only this template is affected; `generate_prompt`
emits prose and is unaffected.

**4. In `architecture.json`, `filename` is the *prompt* path and `filepath` is the *code* path.**
Inverted from what the names suggest. The generator also emits prompt paths without the
`prompts/` prefix and with capitalized languages, and does **not** guarantee that `priority`
respects the dependency DAG — verify and topologically re-sort before generating anything.

**5. `pdd connect` serves 404 until you build its frontend yourself — and every reinstall
undoes it.** The `pdd-cli` wheel ships `pdd/frontend/` *sources* but no `dist/`, so
`pdd/server/app.py` skips mounting the static routes and `/` returns 404 ("Frontend not found
at …"). The build is a one-liner, but it lands inside the uv tool venv, so `uv tool
install/upgrade/reinstall pdd-cli` wipes it — which is why it comes back after any dependency
workaround (this env was installed with `--override z3-solver --no-build-package z3-solver`,
recorded in `~/.local/share/uv/tools/pdd-cli/uv-receipt.toml`). Re-run after any reinstall:

```sh
cd "$("$(dirname "$(readlink -f "$(command -v pdd)")")/python" \
      -c 'import pdd,pathlib;print(pathlib.Path(pdd.__file__).parent/"frontend")')" \
  && npm ci && npm run build
```

(Resolve the path with the tool venv's *own* python — a bare `python3 -c 'import pdd'` can't see
into the uv tool venv.)

Confirm with `Serving frontend from: …/frontend/dist` in the startup banner instead of
`Frontend not found`. Unrelated and harmless: `Warning: Network error getting commands:` with an
empty message is the PDD Cloud command-relay poll timing out; it does not affect local use, and
`pdd connect --local-only` skips that poll entirely.

**6. Activate `.venv` before any `pdd` command that runs tests, or every test reports
`No module named pytest`.** pdd picks the interpreter with
`pdd/python_env_detector.py:detect_host_python_executable()`, which tries, in order: `$VIRTUAL_ENV`
→ `$CONDA_PREFIX` → `shutil.which("python")` → `shutil.which("python3")` → its own
`sys.executable`. With no venv activated the first three all miss (there is no bare `python` on
PATH), so it lands on `/opt/homebrew/bin/python3` — Homebrew 3.14, which has neither pytest nor
this project's dependencies. The failure looks like a broken test but no test ever ran, so
`pdd fix` burns its whole retry budget "fixing" correct code. `source .venv/bin/activate` (or any
`uv run …`, which exports `VIRTUAL_ENV`) makes detection hit branch 1 and resolve
`.venv/bin/python` — Python 3.12 with the `dev` dependency group. Confirm with
`python -c 'import pytest'` before invoking pdd.

Always `--estimate` a command shape you haven't run before.

## Layout

Four `.pddrc` contexts map prompt globs to output dirs:

| Prompts | Generated code |
|---|---|
| `prompts/shared/**` | `shared/` |
| `prompts/backend/**` | `backend/` |
| `prompts/orchestration/**` | `orchestrator/` |
| `prompts/frontend/**` | `frontend/src/` |

Naming: `prompts/<area>/<module>_<lang>.prompt`, lowercase language suffix (`_python`,
`_typescript`, `_typescriptreact`), `snake_case` basenames.

`backend/` and `orchestrator/` are import packages of the repo root. Never add a nested
`pyproject.toml` to either — it breaks every absolute import.

## Architectural constraints

- **Supabase Realtime is the only live-update mechanism.** No SSE, no `EventSource`, no
  WebSocket, no streaming HTTP route. Agents publish to Redis `agent_events`; `backend/event_relay.py`
  bridges that into the `broadcast_events` table, which Realtime fans out.
- **Backend and orchestrator never call each other over HTTP** — only Redis queues/pub-sub and
  shared Postgres tables. The backend never calls an LLM.
- **Unscreened or rejected pitch content never reaches Postgres.** It lives in Redis under a TTL.
  The public ticker carries phase and micro-copy only, never pitch content.
- **The frontend visual identity is frozen** — `prompts/frontend/design_guide.md`. No new tokens,
  fonts, or CSS frameworks; `styles.css` is a versioned artifact, never regenerated.
- `prompts/shared/openapi.yaml` is a frozen asset: context only, never generated.

## Stories

`docs/user_stories/story__*.md` are the source of truth for behaviour, and must keep the
`story__` prefix or pdd's tooling cannot discover them. Prompts map to **modules, not stories** —
the many-to-many link lives in each story's `pdd-story-prompts` header, written by
`pdd story link` and checked by `pdd detect --stories`.

US-13/14/15 are outcome stories with no module of their own; expect them to show as uncovered.
