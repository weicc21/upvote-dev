-- =============================================================================
-- upvote·dev — Supabase PostgreSQL schema
--
-- HOW TO USE: paste this whole file into the Supabase dashboard SQL editor
--   (Database -> SQL Editor -> New query -> Run). It is idempotent — safe to
--   run more than once. No external tooling required.
--
-- Identifier/enum names here are a PHYSICAL CONTRACT shared verbatim with the
-- backend, the orchestrator, prompts/shared/constants_python.prompt, and openapi.yaml.
-- Do not rename a column, type, or enum label here without changing them there.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. Extensions
-- -----------------------------------------------------------------------------
create extension if not exists pgcrypto;   -- provides gen_random_uuid()

-- NOTE: pgvector is deliberately NOT enabled and no `embedding` column exists —
-- retrieval-based dedup is deferred to future roadmap.


-- -----------------------------------------------------------------------------
-- 2. Enum types (case-sensitive labels, verbatim — no other values or casings)
--    Guarded so re-running the script does not error on an existing type.
-- -----------------------------------------------------------------------------
do $$ begin
  create type feature_status as enum
    ('VOTING','CONSOLIDATING','IN_SPRINT','SPLIT','COMPILED','POSTPONED_CONFLICT','ARCHIVED');
exception when duplicate_object then null;
end $$;

do $$ begin
  create type broadcast_phase as enum
    ('screening','synthesizing','architecting','compiling','deployed');
exception when duplicate_object then null;
end $$;

do $$ begin
  create type decision_phase as enum
    ('screening','dedup','friction','compile','deploy','lifecycle');
exception when duplicate_object then null;
end $$;

do $$ begin
  create type build_status as enum ('success','failed');
exception when duplicate_object then null;
end $$;


-- -----------------------------------------------------------------------------
-- 3. Tables (dependency order: feature_requests first)
-- -----------------------------------------------------------------------------

-- The core entity. Permanent — never pruned (dedup memory, provenance, trophies).
create table if not exists public.feature_requests (
  id               uuid        primary key default gen_random_uuid(),
  title            varchar(60) not null,
  description      varchar(300) not null,          -- 30-char minimum enforced by the backend, not the DB
  upvotes          bigint      not null default 0,
  status           feature_status not null,
  parent_id        uuid        references public.feature_requests(id),  -- split children
  split_depth      integer     not null default 0,
  unlock_threshold integer,                          -- nullable; set by orchestration on split children
  postpone_count   integer     not null default 0,
  ai_explanation   text,                             -- Architect reasoning shown on Holding Pattern cards
  merge_count      integer,                          -- openapi Feature.merge_count: duplicates folded into this row (CONSOLIDATING display, US-03)
  extends_id       uuid        references public.feature_requests(id),  -- extension of an already-COMPILED feature
  extends_title    varchar(60),                      -- openapi Feature.extends_title: denormalized base title for the "builds on" chip
  author_id        uuid        not null,             -- NO FK: account management out of scope (DEV_MODE X-Dev-User ids)
  author_handle    varchar(60),                       -- display name for cards (nullable); never the auth id. Denormalized — no accounts table in MVP.
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
-- Additive migrations for an already-created table (idempotent re-run).
alter table public.feature_requests add column if not exists author_handle varchar(60);
alter table public.feature_requests add column if not exists merge_count   integer;
alter table public.feature_requests add column if not exists extends_title varchar(60);
comment on table public.feature_requests is
  'Core entity: community feature pitches and their lifecycle. Permanent, never pruned. Sole creator is the orchestrator.';

-- One row per vote. UNIQUE(feature_id,user_id) is the one-vote-per-user rule.
create table if not exists public.feature_votes (
  id         uuid        primary key default gen_random_uuid(),  -- surrogate PK; PostgREST/Realtime want one. The unique below is the real rule.
  feature_id uuid        not null references public.feature_requests(id),
  user_id    uuid        not null,                  -- NO FK: any UUID the backend resolves (DEV_MODE X-Dev-User)
  created_at timestamptz not null default now(),
  constraint feature_votes_feature_user_unique unique (feature_id, user_id)
);
-- Additive migration: give an already-created feature_votes a surrogate PK.
do $$ begin
  if not exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'feature_votes' and column_name = 'id'
  ) then
    alter table public.feature_votes add column id uuid not null default gen_random_uuid();
    alter table public.feature_votes add primary key (id);
  end if;
end $$;
comment on table public.feature_votes is
  'One row per vote; UNIQUE(feature_id,user_id) makes the database enforce one vote per user (+1 max per identity).';

-- Public ticker copy. High-churn — pruned on schedule (see retention note below).
create table if not exists public.broadcast_events (
  id         uuid           primary key default gen_random_uuid(),
  phase      broadcast_phase not null,
  agent_name varchar(64)    not null,
  message    text           not null,               -- human-readable micro-copy, never raw logs
  created_at timestamptz    not null default now()
);
comment on table public.broadcast_events is
  'AI Creator Broadcast ticker copy relayed from the orchestrator. Pruned on schedule.';

-- Deploy-live records. The INSERT is the single source of truth that a build is live.
create table if not exists public.deployments (
  id                  uuid        primary key default gen_random_uuid(),
  version             varchar(32) not null,
  render_deploy_id    varchar(64),
  preview_url         text,
  shipped_feature_ids jsonb       not null,          -- array of feature UUIDs covered by this deploy
  created_at          timestamptz not null default now()
);
comment on table public.deployments is
  'Deploy-live records; an INSERT drives the frontend "Refresh Preview" pulse and the IN_SPRINT->COMPILED transition.';

-- Permanent governance/outcome dataset. Write-only; never pruned.
create table if not exists public.decision_log (
  id            uuid           primary key default gen_random_uuid(),
  feature_id    uuid           references public.feature_requests(id),  -- nullable for batch-level decisions
  batch_id      uuid,
  phase         decision_phase not null,
  agent         varchar(64)    not null,             -- which agent or webhook produced the decision
  decision      jsonb          not null,             -- full verdict payload: classification, scores, reasoning
  model_version varchar(64)    not null,             -- LLM version; deterministic steps record 'programmatic'
  created_at    timestamptz    not null default now()
);
comment on table public.decision_log is
  'Permanent, write-only governance + outcome dataset. Never pruned.';

-- Compiler diagnostics. High-churn — pruned on schedule.
create table if not exists public.build_logs (
  id               uuid         primary key default gen_random_uuid(),
  version_hash     varchar(64)  not null,
  synthesis_summary text,
  status           build_status not null,
  completed_at     timestamptz  not null default now()
);
comment on table public.build_logs is
  'Compiler build diagnostics. Pruned on schedule.';


-- -----------------------------------------------------------------------------
-- 4. Indexes
-- -----------------------------------------------------------------------------
-- sort=new: every feed view filters on (status, created_at).
create index if not exists idx_feature_requests_status_created_at
  on public.feature_requests (status, created_at);

-- sort=top (the DEFAULT sort, openapi.yaml): keyset pagination needs the sort column in the
-- index, plus a unique tiebreaker — `upvotes` is not unique, so a cursor on it alone cannot
-- resume deterministically. Page with (upvotes, id) < (cursor_upvotes, cursor_id).
create index if not exists idx_feature_requests_status_upvotes
  on public.feature_requests (status, upvotes desc, id desc);

-- Split parents embed their children by parent_id; list views filter to root rows only.
create index if not exists idx_feature_requests_parent_id
  on public.feature_requests (parent_id);

-- Feature.viewer_has_voted: "which of these did the caller vote for" is a user_id-leading
-- lookup. The unique constraint's (feature_id, user_id) index cannot serve it.
create index if not exists idx_feature_votes_user_id
  on public.feature_votes (user_id);

-- /api/sandbox reads the latest deployment; the shipped view and the 48h celebration window
-- both order by deploy recency.
create index if not exists idx_deployments_created_at
  on public.deployments (created_at desc);

-- Feature.shipped_version / shipped_at resolve feature -> deployment through the jsonb array,
-- which needs a GIN index to avoid a full scan per board load.
create index if not exists idx_deployments_shipped_feature_ids
  on public.deployments using gin (shipped_feature_ids jsonb_path_ops);

-- Ticker hydration reads the most recent broadcast_events.
create index if not exists idx_broadcast_events_created_at
  on public.broadcast_events (created_at desc);

-- US-12: decisions are "labelled by type so they can be counted and reviewed over time".
-- DecisionType lives inside the decision payload (constants_python.prompt), not as a column,
-- so counting by type needs an expression index.
create index if not exists idx_decision_log_decision_type
  on public.decision_log ((decision ->> 'type'));
create index if not exists idx_decision_log_phase_created_at
  on public.decision_log (phase, created_at desc);


-- -----------------------------------------------------------------------------
-- 4b. Views
-- -----------------------------------------------------------------------------
-- `Feature.shipped_version` and `Feature.shipped_at` are specified in openapi.yaml as coming
-- "from deployments join", but the link is deployments.shipped_feature_ids — a jsonb array,
-- not a foreign key, so PostgREST resource embedding cannot express it and the backend is
-- restricted to the Supabase query builder (no raw SQL). This view flattens the array into
-- one row per shipped feature so the join becomes an ordinary keyed read.
--
-- distinct on keeps only the most recent deploy per feature: a feature can appear in several
-- deployments, and the trophy/celebration copy always means the latest one.
create or replace view public.feature_shipped_meta as
select distinct on (s.feature_id)
       s.feature_id,
       s.version,
       s.preview_url,
       s.deployed_at
from (
  select (elem.value #>> '{}')::uuid as feature_id,
         d.version,
         d.preview_url,
         d.created_at                as deployed_at
  from public.deployments d
  cross join lateral jsonb_array_elements(d.shipped_feature_ids) as elem(value)
) s
order by s.feature_id, s.deployed_at desc;

comment on view public.feature_shipped_meta is
  'One row per shipped feature -> its most recent deployment. Backs Feature.shipped_version / shipped_at and the 48h COMPILED celebration window on the pipeline view.';


-- -----------------------------------------------------------------------------
-- 5. updated_at trigger (keeps feature_requests.updated_at current)
-- -----------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_feature_requests_set_updated_at on public.feature_requests;
create trigger trg_feature_requests_set_updated_at
  before update on public.feature_requests
  for each row execute function public.set_updated_at();


-- -----------------------------------------------------------------------------
-- 5b. increment_upvotes — the atomic vote counter
-- -----------------------------------------------------------------------------
-- PostgREST cannot express a column-referencing update (`upvotes = upvotes + 1`);
-- it only sends literal values. Without this function the backend is forced into
-- a read-then-write, which loses updates whenever two people vote at once — the
-- exact race the upvote contract forbids. One statement, evaluated server-side,
-- so concurrent callers serialise on the row lock.
--
-- Returns the new count so the endpoint can answer with it and never needs a
-- second round-trip.
create or replace function public.increment_upvotes(row_id uuid)
returns bigint
language sql
volatile
as $$
  update public.feature_requests
     set upvotes = upvotes + 1
   where id = row_id
  returning upvotes;
$$;

comment on function public.increment_upvotes(uuid) is
  'Atomically increments feature_requests.upvotes and returns the new value. Called via supabase.rpc() by the upvote endpoint.';

grant execute on function public.increment_upvotes(uuid) to anon, authenticated;


-- -----------------------------------------------------------------------------
-- 6. Realtime — publish feature_requests, broadcast_events, deployments only.
--    Guarded: creates the supabase_realtime publication if missing and adds
--    each table only when not already a member.
-- -----------------------------------------------------------------------------
do $$
begin
  if not exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    create publication supabase_realtime;
  end if;

  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'feature_requests'
  ) then
    execute 'alter publication supabase_realtime add table public.feature_requests';
  end if;

  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'broadcast_events'
  ) then
    execute 'alter publication supabase_realtime add table public.broadcast_events';
  end if;

  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'deployments'
  ) then
    execute 'alter publication supabase_realtime add table public.deployments';
  end if;
end $$;


-- -----------------------------------------------------------------------------
-- 7. Grants — decision_log is write-only history.
--    NOTE: the Supabase service role (used by the backend/orchestrator) bypasses
--    grants and RLS, so this expresses intent and constrains the anon/authenticated
--    roles; full enforcement arrives with real roles/RLS below.
-- -----------------------------------------------------------------------------
revoke update, delete on public.decision_log from anon, authenticated;

-- The board is readable without an account (US-05) and vote counts must reach other viewers
-- live (US-04). Both are anon-role reads: Realtime only delivers a row change to a client
-- whose role can SELECT the table, so these grants are what make the published tables in
-- section 6 actually visible. Reads only — every write goes through the service role.
grant select on public.feature_requests    to anon, authenticated;
grant select on public.feature_votes       to anon, authenticated;
grant select on public.broadcast_events    to anon, authenticated;
grant select on public.deployments         to anon, authenticated;
grant select on public.feature_shipped_meta to anon, authenticated;


-- =============================================================================
-- Retention (operational, not schema-enforced — implement as scheduled jobs):
--   * feature_requests, decision_log : NEVER pruned.
--   * broadcast_events, build_logs   : prune rows older than ~60 days.
-- =============================================================================

-- =============================================================================
-- Row-Level Security — deliberately NOT enabled for the hackathon (DEV_MODE;
-- see prompts/backend/deps_python.prompt). When real auth (Supabase JWT) lands, reinstate the
-- auth.users FKs and enable RLS. Reference policy set, left commented out:
--
--   alter table public.feature_requests add constraint feature_requests_author_fk
--     foreign key (author_id) references auth.users(id);
--   alter table public.feature_votes   add constraint feature_votes_user_fk
--     foreign key (user_id)   references auth.users(id);
--
--   alter table public.feature_requests enable row level security;
--   alter table public.feature_votes    enable row level security;
--
--   -- Public reads of the feed:
--   create policy feature_requests_read  on public.feature_requests for select using (true);
--   -- Writes only as the authenticated author:
--   create policy feature_votes_insert   on public.feature_votes    for insert
--     with check (auth.uid() = user_id);
-- =============================================================================
