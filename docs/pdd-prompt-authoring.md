# Authoring prompts with the PDD CLI

How this repo goes from user stories to `prompts/**/*.prompt`, following
[the PDD prompting guide](https://github.com/promptdriven/pdd/blob/main/docs/prompting_guide.md).

## The mapping (read this first)

PDD's rule is **one prompt = one module**, *not* one prompt per user story.
Stories and prompts are different artifacts on different axes:

```
docs/user_stories/story__*.md     ── behaviour, human-verifiable, source of truth
            │
            │  pdd generate --template architecture/architecture_json
            ▼
     architecture.json            ── module decomposition: name, path, deps, priority
            │
            │  pdd generate --template generic/generate_prompt   (once per module)
            ▼
   prompts/<area>/<module>_<lang>.prompt
            │
            │  pdd sync <basename>
            ▼
   backend/ · orchestrator/ · frontend/src/  + tests
```

One story fans out to several prompts (US-01 "pitch a feature" touches the submit modal, the
API client, the writes route, and the shared contracts). One prompt serves several stories.
That many-to-many link is recorded in each story's `pdd-story-prompts` header by
`pdd story link`, and checked by `pdd detect --stories`.

## Prerequisites

```bash
pdd auth login          # session token expires; cloud grounding needs it
pdd which               # confirm resolved paths/model before spending anything
```

Every generating command below accepts `--estimate` (alias `--dry-run-cost`) to price the call
without making it. Use it the first time you run each step.

### Environment and known failures

`CLAUDE.md` carries the four non-obvious pdd behaviours you must know before spending anything:
the required `PDD_COMMAND_MAX_OUTPUT_TOKENS` export, where models are actually configured, the
architecture template's guaranteed schema failure against TokenRouter (and how to recover output
from the log instead of paying twice), and the inverted `filename`/`filepath` fields. Read that
section first.

Verify a new model id routes before adding it to `~/.pdd/llm_model.csv`:

```bash
curl -s https://api.tokenrouter.com/v1/chat/completions \
  -H "Authorization: Bearer $TOKENROUTER_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"anthropic/claude-opus-4.6","messages":[{"role":"user","content":"say OK"}],"max_tokens":16}'
```

## Step 1 — Generate `architecture.json`

The module decomposition is derived from the PRD plus all 15 stories.

```bash
pdd generate --template architecture/architecture_json \
  -e APP_NAME="Community Feature Voting Portal" \
  -e PRD_FILE=docs/PRD.md \
  -e TECH_STACK_FILE=docs/tech_stack.md \
  -e DOC_FILES="$(ls docs/user_stories/story__*.md | paste -sd, -)" \
  --output architecture.json
```

Each entry carries `reason`, `description`, `dependencies`, `priority`, `filename`, `filepath`.

**Review it by hand before continuing** — this file decides every prompt that follows. Check:

- Module boundaries respect the API ↔ orchestrator split (Redis only, never HTTP).
- `filepath` values land under `backend/`, `orchestrator/`, `frontend/src/`, `shared/`.
- `dependencies` form a DAG with the shared contracts module at the root.
- `priority` orders generation so dependencies come first.

The generator does **not** guarantee that last point. The first accepted run put
`backend/main.py` at priority 4 while it depends on routes at 5–11, and `sprint_service` before
`architect_agent` — 19 backwards edges. Generation walks priority order, so those `<include>`s
would resolve against files that do not exist yet. Verify and re-sort:

```bash
python3 - <<'PY'
import json,heapq
d=json.load(open("architecture.json")); by={m["filename"]:m for m in d}
deps={m["filename"]:set(m["dependencies"]) for m in d}
indeg={n:len(deps[n]) for n in deps}; dep_of={n:[] for n in deps}
for n,ds in deps.items():
    for x in ds: dep_of[x].append(n)
h=[(by[n]["priority"],n) for n in deps if not indeg[n]]; heapq.heapify(h); order=[]
while h:
    _,n=heapq.heappop(h); order.append(n)
    for c in dep_of[n]:
        indeg[c]-=1
        if not indeg[c]: heapq.heappush(h,(by[c]["priority"],c))
assert len(order)==len(d), "cycle in dependencies"
for i,n in enumerate(order,1): by[n]["priority"]=i
d.sort(key=lambda m:m["priority"])
json.dump(d,open("architecture.json","w"),indent=2)
PY
```

Prompt paths also come back as `shared/constants_Python.prompt` — no `prompts/` prefix,
capitalized language. Rewrite `filename` **and every `dependencies` entry** to
`prompts/<area>/<module>_<lang>.prompt` (lowercase) so the `.pddrc` context globs match.

## Step 2 — Reconcile `.pddrc`

`.pddrc` already defines four contexts (`shared`, `backend`, `frontend`, `orchestration`) that
map prompt globs to output dirs. Confirm every `filepath` in `architecture.json` falls under a
context's `generate_output_path`. If the decomposition introduced a new area, add a context —
or regenerate the file:

```bash
pdd generate --template generic/generate_pddrc -e ARCHITECTURE_FILE=architecture.json --output .pddrc
```

Naming convention this repo already uses (keep it — `.pddrc` assumes it):
`prompts/<area>/<module>_<lang>.prompt`, lowercase language suffix
(`_python`, `_typescript`, `_typescriptreact`).

## Step 3 — Generate one prompt per module

Work in `priority` order so a module's dependencies already exist when it is generated.
`scripts/pdd_prep.py` builds the two per-module inputs and prints the command:

```bash
python3 scripts/pdd_prep.py prompts/backend/routes/features_python.prompt --stories US-05
```

It writes:

- **`.pdd/arch_slice.json`** — the module plus its transitive dependencies only. Passing the whole
  34-module architecture costs ~15k input tokens per call and buys a leaf module nothing. Slicing
  took one prompt from $0.48 to $0.14.
- **`.pdd/story_pack.md`** — `docs/PRD.md` plus the full text of every story the module serves.

That second file matters more than it looks. **`generic/generate_prompt` has no `DOC_FILES` slot**,
so without it the generator never sees an acceptance criterion — only the one-line `description`
from `architecture.json`. That is how a pitch limit of 3 reached a prompt when US-01 says 5. The
first module generated *with* a story pack came back using the right table names and the right
limits on the first try.

Story selection is by `US-NN` citations in the module's `reason`/`description`. Those citations are
incomplete — `features` serves both US-01 and US-05 but only cited US-01 — so pass the rest with
`--stories`.

Two inputs the script does not add, worth passing by hand:

- **`-e API_DOC_FILE=prompts/shared/openapi.yaml`** for any module serving HTTP. Omitting it on
  `features` produced `{"detail": …}` error bodies and a `202 {feature_id, status}` — the frozen
  spec requires `ErrorBody` `{code, message}` and `202 {feature_id, state:"screening"}`.
- **`-e DB_SCHEMA_FILE=schema.sql`** for anything touching Postgres.

## Step 4 — Edit each prompt down to the guide's shape

**Every generated prompt needs this pass.** Two units in, the output ran 158 and 298 lines and
came down to 83 and 88 — roughly a 3× cut each time, landing inside the guide's **10–30% of
expected code size**.

Delete, every time:

- **The dangling "Shared Context (canonical data model…)" block** followed by *"use ONLY the field
  names from the data dictionary above"* — with no data dictionary above it. Passing
  `DB_SCHEMA_FILE` does **not** fill it; the schema is processed but its content never reaches the
  prompt. Left in, it points the compiler at a table that isn't there.
- **`Instructions`, `Testing notes`, `Deliverable`, `Implementation assumptions`** — implementation
  steps and restated tests. The guide is explicit: specify interfaces, invariants, and outcomes;
  let the model choose how. `pdd test` writes the tests.
- **`<web>` tags.** The template likes to attach live doc fetches (Python `enum`, pydantic
  settings). Non-deterministic, re-fetched on every regeneration, and a failure point.

Then verify, every time:

- **Invented defaults against your own sources.** Unit 1 shipped a pitch limit of 3 (stories say
  5), threshold 5 (architecture says 10), and hourly cadence (stories say daily).
- **Enum values against `schema.sql`.** It is a physical contract: `feature_status` labels are
  uppercase but `broadcast_phase`, `decision_phase`, and `build_status` are lowercase. A prompt
  rule saying "value equals name" silently breaks every insert on the lowercase three.
- **Table names.** They are `feature_requests` and `feature_votes`, not `features` and `votes`.
- **Scope against `<pdd-interface>`.** Unit 1 invented a lifecycle transition graph nobody asked
  for and no story states.

Keep and escalate:

- Always: `<pdd-reason>`, `<pdd-interface>`, `<responsibility>`, `<contract_rules>`.
- `<non_responsibilities>` wherever two modules could each claim the same job.
- `<capabilities>` for any module touching Redis, Postgres, an LLM, or the network.
- `<coverage>` mapping each rule to a story file, or an honest `TODO`.

**Exactly one `<contract_rules>` block per prompt.** Splitting rules across two blocks — say one
per HTTP route — silently drops every rule in the first block, and its `<coverage>` entries then
hard-fail as `UNKNOWN_COVERAGE_REF`. Number rules straight through `R1..Rn` in a single block and
reference them from the prose sections instead.

`pdd contracts check` is **line-based**, which drives two more formatting rules that are easy to trip:

- **One `<non_responsibilities>` claim per physical line.** A wrapped sentence makes the
  continuation line look like a claim with no modal verb (`MISSING_MODAL`). Keep each on one line
  even when it runs long.
- **One rule per `<coverage>` line.** `R1, R2, R3: TODO …` registers only `R1`; `R2` and `R3` then
  report as `UNCOVERED_MUST_NOT`. Write them out separately.

A clean prompt ends with `0 error(s)` and only `UNCHECKED_RULE` warnings — that residue is the
test backlog for step 8, not something to silence.

Re-sync `architecture.json`'s `interface` from the edited `<pdd-interface>` afterwards, or the two
drift immediately.

Then check structure deterministically (free, no LLM):

```bash
pdd contracts check prompts/backend/routes/writes_python.prompt
pdd contracts check prompts --strict --stories docs/user_stories
```

## Step 5 — Wire dependencies automatically

Replaces hand-written `<include>` blocks with ones derived from the real tree:

```bash
pdd auto-deps prompts/backend/routes/writes_python.prompt backend/ --csv prompts/backend/io_dependencies.csv
```

Prefer `<include mode="interface">` for large dependencies — signatures and docstrings only.

## Step 6 — Link stories to prompts

```bash
pdd story link docs/user_stories/story__pitch_a_feature.md \
  --prompt prompts/backend/routes/writes_python.prompt \
  --prompt prompts/frontend/components/submit_modal_typescriptreact.prompt \
  --prompts-dir prompts

pdd story list --stories-dir docs/user_stories --with-regression-status
```

## Step 7 — Validate coverage

```bash
pdd detect --stories --stories-dir docs/user_stories --prompts-dir prompts
```

This is the gate that answers *"do my prompts actually satisfy my stories?"* — the question
that motivated this whole exercise. Any story with no linked prompt, or acceptance criteria no
prompt covers, surfaces here.

## Step 8 — Generate code and tests

```bash
pdd sync contracts --dry-run     # inspect the plan first
pdd sync contracts               # then per module, in architecture.json priority order
```

`sync` runs generate → example → test → verify → fix, honouring the `target_coverage`,
`budget`, and `max_attempts` in `.pddrc`.

## The standing rule

**Never hand-patch generated code.** When behaviour is wrong, edit the prompt and regenerate.
If you already patched the code, back-propagate it at the *behaviour* level:

```bash
pdd update prompts/backend/routes/writes_python.prompt backend/routes/writes.py
```

Write the recovered requirement as an observable outcome, never as a transcription of private
helper names or internal calls.
