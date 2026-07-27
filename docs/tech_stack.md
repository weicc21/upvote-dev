# Tech stack

Facts taken from `pyproject.toml`, `.env.example`, `.pddrc`, and the existing source tree.
This file exists to shape `architecture.json` and per-module prompts — it is context, not a spec.

## Runtime

| Concern | Choice |
|---|---|
| Language (backend + orchestration) | Python ≥ 3.11 |
| Web framework | FastAPI (`fastapi[standard]`), served by Uvicorn |
| Language (frontend) | TypeScript + React, built with Vite |
| Persistent store | Supabase (Postgres) via `supabase-py`; Supabase Realtime for live board updates |
| Ephemeral store / queues | Redis (`redis>=5.0`, asyncio client) — list queues + pub/sub |
| LLM access | OpenAI-compatible client (`openai>=1.30`) pointed at `LLM_BASE_URL` / TokenRouter / OpenRouter |
| Deploy target | Render (deploy webhook → `deployments` table → sandbox preview) |
| Package manager | `uv` (see `uv.lock`) |

## Repository layout

Three deployable units, each its own `.pddrc` context:

| Context | Prompts | Generated output |
|---|---|---|
| `backend` | `prompts/backend/**` | `backend/` — FastAPI app, routes, event relay |
| `orchestration` | `prompts/orchestration/**` | `orchestrator/` — ingestion daemon, PM agent, architect, sprint service, compiler writer |
| `frontend` | `prompts/frontend/**` | `frontend/src/` — React app shell, cards, modals, sandbox, ticker |
| `shared` | `prompts/shared/**` | `shared/` — canonical constants, frozen `openapi.yaml` |

`backend/` and `orchestrator/` are import packages of the repo root (`backend.*`,
`orchestrator.*`). Do not add a nested `pyproject.toml` to either — it would break
every absolute import.

## Process topology

Long-running processes, started per `pyproject.toml` console scripts:

- `uvicorn backend.main:app` — the public HTTP API + webhook receiver
- `ingestion-service` — daemon; `BRPOP` on `feature_intake`, runs screening + dedup
- `sprint-service` — cadence-driven; selects top-voted features, runs architect + compiler

The backend and the orchestrator **never call each other over HTTP**. They communicate
only through Redis (`feature_intake` list, `agent_events` / `screening_results` pub/sub)
and shared Postgres tables.

## Testing

- Python: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`) + `fakeredis`; Supabase stubbed.
- Frontend: Vitest + React Testing Library + jsdom, against a stubbed `api_client`.
  Never against a live backend or live Realtime.

## Live updates — Supabase Realtime only

There is exactly **one** live-update mechanism. Do not introduce a second.

- The browser subscribes to Supabase Realtime for feature rows (board cards advancing
  through stages) and for the `broadcast_events` table (the agent ticker).
- Agents publish to the Redis `agent_events` pub/sub channel. A backend **event relay**
  task consumes that channel and writes rows into `broadcast_events`, which is what
  Realtime fans out to clients.
- **No SSE, no `EventSource`, no WebSocket endpoint, no `/api/events` route.** The backend
  exposes no streaming HTTP endpoint of any kind.

## Required modules

These exist for reasons not derivable from the acceptance criteria alone. Keep them as
separate modules; do not fold them into their callers.

| Module | Why it must be its own module |
|---|---|
| `backend/deps.py` | The single dependency-injection seam. Providers (`get_supabase`, `get_redis`) that every route imports and that tests override with `fakeredis` / a stubbed Supabase client. Without it, routes construct their own clients and no route is unit-testable. |
| `backend/event_relay.py` | Background task bridging Redis `agent_events` → the `broadcast_events` table. Separate from route handling because it is a long-lived consumer, not a request path. |
| `orchestrator/lifecycle.py` | End-of-sprint maintenance: roll unimplemented features back to `VOTING`, decay stale backlog items. A scheduled sweep over all features, distinct from the sprint service's "select and build the top N" job. |

## Prompt naming and layout

Prompts live under `prompts/`, mirrored into the `.pddrc` contexts, with a **lowercase**
language suffix:

```
prompts/shared/<module>.prompt                     -> shared/
prompts/backend/<module>_python.prompt             -> backend/
prompts/backend/routes/<module>_python.prompt      -> backend/routes/
prompts/orchestration/<module>_python.prompt       -> orchestrator/
prompts/frontend/<module>_typescript.prompt        -> frontend/src/
prompts/frontend/components/<module>_typescriptreact.prompt -> frontend/src/components/
```

Module and file basenames are `snake_case` (`submit_modal.tsx`, not `SubmitPitchModal.tsx`).

## Hard constraints

- The frontend visual identity is **frozen** — see `prompts/frontend/design_guide.md`.
  No new tokens, fonts, or CSS frameworks; `styles.css` is a versioned artifact, never regenerated.
- `prompts/shared/openapi.yaml` is a frozen asset: reference/context only, never generated.
- Unscreened pitch content lives in Redis with a TTL and **never** reaches Postgres.
