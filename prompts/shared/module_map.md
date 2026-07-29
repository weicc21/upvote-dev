# FROZEN ASSET — canonical import paths

Included by every prompt. `<include>` drops a dependency's source in unlabelled, so the only
path-shaped hint the compiler otherwise sees is the *prompt* path in `<pdd-dependency>` — and the
prompt tree and the package tree do not have the same names. This file is that mapping, stated
once.

## Package layout

| Code file | Import it as |
|---|---|
| `shared/constants.py` | `from shared.constants import FeatureStatus, RejectionReason, TABLE_…, REDIS_…, DEFAULT_…` |
| `shared/config.py` | `from shared.config import settings, Settings` |
| `backend/deps.py` | `from backend.deps import get_settings, get_supabase, get_redis, get_current_user_id, get_optional_user_id, raise_error, ErrorResponse` |
| `backend/routes/features.py` | `from backend.routes.features import router` |
| `backend/routes/votes.py` | `from backend.routes.votes import router` |
| `backend/main.py` | `backend.main:app` (uvicorn target; never imported by a route) |
| `orchestrator/screener.py` | `from orchestrator.screener import screen_pitch, Verdict` |
| `orchestrator/ingestion_service.py` | `python -m orchestrator.ingestion_service` (daemon entrypoint) |

## Names that look importable and are not

These have each already caused a generated `ImportError`:

- **`orchestration.*`** — `prompts/orchestration/` is the *prompt* directory. The Python package
  is **`orchestrator`**. `from orchestration.screener import …` fails.
- **`backend.dependencies`** — the module is **`backend.deps`**.
- **`backend.features` / `backend.votes`** — the routes live one level down, in
  **`backend.routes.features`** and **`backend.routes.votes`**.
- **`backend.contracts`, `backend.config`, `backend.database`, `backend.models`,
  `backend.auth`** — none exist. Constants come from `shared.constants`, settings from
  `shared.config`, clients from `backend.deps`.
- **`shared.contracts`** — the module is **`shared.constants`**.

## Third-party symbols, pinned to the installed versions

Verified against this project's `.venv`; the plausible-looking alternatives do not exist.

| Use | Correct import | Not |
|---|---|---|
| Supabase async client | `from supabase._async.client import AsyncClient, create_client` | `create_async_client` |
| Redis async client | `import redis.asyncio as aioredis` then `aioredis.from_url(...)` | `aioredis` package, `redis.Redis` |
| Postgres error codes | `from postgrest.exceptions import APIError` | `postgrest.APIError` |
| Settings | `from pydantic_settings import BaseSettings, NoDecode` | `pydantic.BaseSettings` |
| Enums | `from enum import StrEnum` | `(str, Enum)` mixin — see `constants_python.prompt` R8 |

Client shutdown: the Redis client closes with `await client.aclose()`. The Supabase async client
exposes **no** public close in this version — let it be garbage-collected. Never call
`supabase.auth.sign_out()` to "close" it: that is a network round-trip ending a user session, and
a service-role client has none.

## Target app layout (`TARGET_PROMPT_DIR`)

`TARGET_PROMPT_DIR` points at a **whole git working tree**, not a prompt folder:

| path | what it is |
|---|---|
| `streaks_demo_typescriptreact.prompt` | the blueprint the compiler appends Feature Blocks to (`architect._BLUEPRINT_FILENAME` pins the same name) |
| `streaks_demo.tsx` | the generated source — the compile output, and the only thing worth hashing |
| `node_modules/`, `dist/`, `.git/`, `package-lock.json` | present and enormous |

There is no `prompt.md`. Never walk this directory recursively: `rglob("*")` reaches
`node_modules` and `.git`, which makes any hash both slow and non-deterministic across installs and
builds. Read the two named files directly.

## Running COMPILE_COMMAND (US-09)

`COMPILE_COMMAND` comes from `.env` and is operator-supplied, e.g.
`pdd --local --force generate streaks_demo_typescriptreact.prompt --output streaks_demo.tsx`.
Three things about invoking it:

1. **The compiler injects `PDD_COMMAND_MAX_OUTPUT_TOKENS` into the child environment itself**
   (`env={**os.environ, ...}`), rather than relying on the operator's shell or on an inline
   `VAR=value` prefix in the command string. Unset, `llm_invoke` sends no `max_tokens` and inherits
   the provider ceiling, truncating generated source mid-file — a failure that looks like a broken
   compile, not a missing variable.
2. **An inline `VAR=value` prefix only works under a shell.** `shlex.split("VAR=1 pdd …")` makes
   `VAR=1` into `argv[0]` and raises `FileNotFoundError`. Run the command through a shell so
   operators can write prefixes, pipelines and `cd x && …`; the env injection in (1) means nothing
   depends on them doing so.
3. **`COMPILE_COMMAND` MUST NOT be built from pitch content.** Running it through a shell is safe
   only while the string is operator-controlled configuration. Nothing a community member typed may
   ever reach it.

`--force` is required in the command: without it pdd asks `Overwrite existing files? [Y/n]` and, with
no TTY, hangs rather than failing. `--local` keeps execution off the PDD Cloud relay.

## HTTP clients available to Python modules

`httpx` is the only async HTTP client installed (`pyproject.toml` declares it; supabase-py depends on
it). **`aiohttp` and `requests` are NOT installed** — importing either raises `ModuleNotFoundError`
at first use, which surfaces as a pipeline step failing at runtime rather than at import, long after
the tests pass against injected seams.

## PostgREST behaviours this project relies on

- **Repeated `or=` parameters compose as AND.** `.or_(a).or_(b)` is `(a) AND (b)`, which is what lets
  the celebration-window filter sit alongside keyset pagination without either clobbering the other.
- **`id.in.()` with an empty list is rejected.** Branch to a plain `.neq(...)` when the set is empty
  rather than emitting a degenerate `in`.
- **`feature_shipped_meta` cannot be embedded.** Resource embedding needs a foreign key; the link
  from `deployments` to features is a jsonb array, so any query needing `deployed_at` alongside
  feature rows is two round-trips by construction.
- **A view's columns are not the wire field names** — see the `feature_shipped_meta` table above.

## Narrow tables that do NOT carry `feature_id`

| table | columns | notes |
|---|---|---|
| `build_logs` | `id, version_hash, synthesis_summary, status, completed_at` | build diagnostics keyed by build, **not** by feature; pruned on schedule |
| `decision_log` | `id, feature_id, batch_id, phase, agent, decision, model_version, created_at` | the permanent, feature-linked governance record |

Inserting `feature_id` or a `log_tail` into `build_logs` raises
`PGRST204: Could not find the '<col>' column of 'build_logs' in the schema cache`. If a build needs
to be traced back to a feature, that link belongs in `decision_log`; `build_logs.synthesis_summary`
is the only free-text field and it is bounded.

## Database views

`public.feature_shipped_meta` — one row per shipped feature, its most recent deployment.
Its columns are **not** named after the wire fields they populate:

| view column | wire field |
|---|---|
| `feature_id` | (join key) |
| `version` | `Feature.shipped_version` |
| `deployed_at` | `Feature.shipped_at` |
| `preview_url` | — |

Selecting `shipped_version` or `shipped_at` from this view raises
`42703: column feature_shipped_meta.shipped_version does not exist`. The view flattens
`deployments.shipped_feature_ids` (a jsonb array, so PostgREST cannot embed it) into a keyed read;
`schema.sql` is the definition.

## Database functions (called via `supabase.rpc`)

PostgREST resolves a function by its exact named-argument set, so a guessed extra parameter fails
at runtime with `PGRST202 Could not find the function`, not at import.

| Function | Call as | Returns |
|---|---|---|
| `increment_upvotes` | `supabase.rpc("increment_upvotes", {"row_id": <uuid>})` | the new `upvotes` value |

`increment_upvotes` takes **exactly one argument, `row_id`**, and adds one. There is no `inc`,
`amount`, or `delta` parameter. To add more than one vote, call it more than once.

`set_updated_at` is a trigger and is never called directly. No other function exists in
`schema.sql`; if a module needs one, add it to the schema first.

## Which model each agent uses

Pinned per role, so one role can move to a different model without touching the others. A module
MUST read the setting for its own role — `pm_agent` reading `LLM_MODEL_SCREENING` compiles, passes
tests, and silently ignores the operator's PM pin.

| Module | Setting | Why |
|---|---|---|
| `orchestrator/screener.py` | `settings.LLM_MODEL_SCREENING` | classification-shaped, runs on every pitch — fast tier |
| `orchestrator/pm_agent.py` | `settings.LLM_MODEL_PM` | comparison against the board — fast tier |
| `orchestrator/architect.py` (US-08) | `settings.LLM_MODEL_ARCHITECT` | reasoning-heavy spec writing — stronger model |

All three share `settings.LLM_BASE_URL`, `settings.LLM_API_KEY`, `settings.LLM_TEMPERATURE`,
`settings.LLM_TIMEOUT_SECONDS`, and `settings.LLM_MAX_ATTEMPTS`. Temperature is deliberately one
value: every one of these calls is a classification or a structured extraction, and none of them
wants a chatty default.

## React components (frontend)

The project builds with `jsx: "react-jsx"`, so the default `React` import is unnecessary and
`noUnusedLocals` rejects it. Import only the hooks actually used:

```ts
import { useState, useEffect, useCallback } from "react";   // correct
import React, { useState } from "react";                    // fails typecheck
```

Wire types come from `../api_client` (or `./api_client` at the src root) — there is no
`contracts.ts`. Only `app_shell.tsx` imports `./styles.css`, once for the whole app.

## Rule

Import only from the left-hand column above, plus the standard library and declared third-party
packages. A module path that appears nowhere in this file does not exist — do not invent one to
make an import read naturally.
