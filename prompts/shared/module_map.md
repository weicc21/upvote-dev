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

## Rule

Import only from the left-hand column above, plus the standard library and declared third-party
packages. A module path that appears nowhere in this file does not exist — do not invent one to
make an import read naturally.
