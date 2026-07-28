// frontend/src/api_client.ts
//
// The single seam between the forum UI and its backend.
// Every HTTP call, the Realtime subscription, the wire types, and the
// anonymous session that makes "no signup" true all live here.

import { createClient, type SupabaseClient, type RealtimeChannel } from "@supabase/supabase-js";

// ---------------------------------------------------------------------------
// R1, R2: Wire types — single definition, snake_case, field-for-field match
// ---------------------------------------------------------------------------

export type Status =
  | "VOTING"
  | "CONSOLIDATING"
  | "IN_SPRINT"
  | "SPLIT"
  | "COMPILED"
  | "POSTPONED_CONFLICT"
  | "ARCHIVED";

export type View = "pipeline" | "shipped" | "holding" | "vault";

export type Sort = "top" | "new";

// R2, R3: Feature mirrors openapi.yaml exactly. children is always Feature[].
export interface Feature {
  id: string;
  title: string;
  description: string;
  status: Status;
  upvotes: number;
  author_handle: string | null;
  parent_id: string | null;
  split_depth: number;
  unlock_threshold: number | null;
  extends_id: string | null;
  extends_title: string | null;
  postpone_count: number;
  ai_explanation: string | null;
  merge_count: number | null;
  shipped_version: string | null;
  shipped_at: string | null;
  viewer_has_voted: boolean;
  children: Feature[];
  created_at: string;
  updated_at: string | null;
}

export interface PendingPitch {
  feature_id: string;
  title: string;
  state: "screening" | "rejected" | "merged";
  reason: "security" | "off_topic" | "unclear" | "already_shipped" | null;
  shipped_version: string | null;
  merged_into_feature_id: string | null;
  merged_into_title: string | null;
  submitted_at: string;
}

// R4: Normalised result — expected failures are values, not exceptions.
export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; code: string; message: string; resets_at?: string };

// ---------------------------------------------------------------------------
// R12: Configuration from Vite env — no hardcoded hosts
// ---------------------------------------------------------------------------

const API_BASE: string = (import.meta.env.VITE_API_BASE_URL as string) ?? "";
const SUPABASE_URL: string = (import.meta.env.VITE_SUPABASE_URL as string) ?? "";
const SUPABASE_ANON_KEY: string = (import.meta.env.VITE_SUPABASE_ANON_KEY as string) ?? "";
const DEV_MODE: boolean = import.meta.env.VITE_DEV_MODE === "true";

// R15: Request timeout (ms)
const REQUEST_TIMEOUT_MS = 15_000;

// ---------------------------------------------------------------------------
// R20, R21: sandboxUrl — validated external URL for the preview iframe
// ---------------------------------------------------------------------------

function validateSandboxUrl(raw: string | undefined): string | null {
  if (!raw) return null;
  try {
    const u = new URL(raw);
    if (u.protocol !== "https:") return null;
    // R21: allowlist — *.onrender.com
    if (u.hostname.endsWith(".onrender.com")) return raw;
    return null;
  } catch {
    return null;
  }
}

export const sandboxUrl: string | null = validateSandboxUrl(
  import.meta.env.VITE_SANDBOX_URL as string | undefined,
);

// ---------------------------------------------------------------------------
// Supabase client (auth + Realtime only — no direct Postgres)
// ---------------------------------------------------------------------------

let supabase: SupabaseClient | null = null;

function getSupabase(): SupabaseClient {
  if (!supabase) {
    supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
      },
    });
  }
  return supabase;
}

// ---------------------------------------------------------------------------
// R10: Dev-mode stable UUID (persisted per browser profile)
// ---------------------------------------------------------------------------

const DEV_USER_STORAGE_KEY = "dev_user_uuid";

function getDevUserId(): string {
  let id = localStorage.getItem(DEV_USER_STORAGE_KEY);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(DEV_USER_STORAGE_KEY, id);
  }
  return id;
}

// ---------------------------------------------------------------------------
// R8: ensureSession — anonymous auth, no signup ceremony
// R11: token never logged, placed in URL, or included in error messages
// ---------------------------------------------------------------------------

let sessionPromise: Promise<string | null> | null = null;

export async function ensureSession(): Promise<string | null> {
  if (DEV_MODE) {
    // In dev mode we don't use Supabase auth at all
    return null;
  }

  if (sessionPromise) return sessionPromise;

  sessionPromise = (async (): Promise<string | null> => {
    const sb = getSupabase();
    const { data: existing } = await sb.auth.getSession();
    if (existing?.session?.access_token) {
      return existing.session.access_token;
    }
    const { data: created, error } = await sb.auth.signInAnonymously();
    if (error || !created?.session?.access_token) {
      // Reset so a future call can retry
      sessionPromise = null;
      return null;
    }
    return created.session.access_token;
  })();

  return sessionPromise;
}

// ---------------------------------------------------------------------------
// Internal fetch helper
// ---------------------------------------------------------------------------

function fail(status: number, code: string, message: string, resets_at?: string): ApiResult<never> {
  return resets_at !== undefined
    ? { ok: false, status, code, message, resets_at }
    : { ok: false, status, code, message };
}

async function apiFetch<T>(
  path: string,
  options: {
    method?: string;
    body?: unknown;
    auth?: boolean;
  } = {},
): Promise<ApiResult<T>> {
  const { method = "GET", body, auth = false } = options;

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };

  // R9, R10: Attach credentials on writes and getMyPitches
  if (auth) {
    if (DEV_MODE) {
      headers["X-Dev-User"] = getDevUserId();
    } else {
      const token = await ensureSession();
      if (token) {
        headers["Authorization"] = `Bearer ${token}`;
      }
    }
  }

  // R15: AbortController for timeout
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });

    clearTimeout(timer);

    if (res.ok) {
      const data = (await res.json()) as T;
      return { ok: true, data };
    }

    // Non-2xx: parse backend error envelope
    // R5: pass message through untouched
    // R6: surface resets_at from 429 (sibling of error, not inside it)
    try {
      const envelope = await res.json();
      const errBody = envelope?.error;
      const code: string = errBody?.code ?? "unknown";
      const message: string = errBody?.message ?? res.statusText;
      const resets_at: string | undefined = envelope?.resets_at;
      return fail(res.status, code, message, resets_at);
    } catch {
      return fail(res.status, "parse_error", res.statusText);
    }
  } catch (err: unknown) {
    clearTimeout(timer);
    if (err instanceof DOMException && err.name === "AbortError") {
      return fail(0, "timeout", "The request timed out. Please try again.");
    }
    const msg =
      err instanceof Error ? err.message : "A network error occurred. Please try again.";
    return fail(0, "network_error", msg);
  }
}

// ---------------------------------------------------------------------------
// R13, R14: listFeatures — exact query param names from openapi.yaml
// ---------------------------------------------------------------------------

export async function listFeatures(params: {
  view: View;
  sort?: Sort;
  q?: string;
  status?: Status[];
  cursor?: string;
  limit?: number;
}): Promise<ApiResult<{ features: Feature[]; next_cursor: string | null }>> {
  const qs = new URLSearchParams();
  qs.set("view", params.view);
  if (params.sort) qs.set("sort", params.sort);
  if (params.q) qs.set("q", params.q);
  if (params.status && params.status.length > 0) qs.set("status", params.status.join(","));
  if (params.cursor) qs.set("cursor", params.cursor);
  if (params.limit !== undefined) qs.set("limit", String(params.limit));

  return apiFetch<{ features: Feature[]; next_cursor: string | null }>(
    `/api/features?${qs.toString()}`,
  );
}

// ---------------------------------------------------------------------------
// getFeature
// ---------------------------------------------------------------------------

export async function getFeature(id: string): Promise<ApiResult<Feature>> {
  return apiFetch<Feature>(`/api/features/${encodeURIComponent(id)}`);
}

// ---------------------------------------------------------------------------
// R9: getMyPitches — read that depends on identity, so it carries the token
// ---------------------------------------------------------------------------

export async function getMyPitches(): Promise<
  ApiResult<{ pending: PendingPitch[]; features: Feature[] }>
> {
  return apiFetch<{ pending: PendingPitch[]; features: Feature[] }>("/api/features/mine", {
    auth: true,
  });
}

// ---------------------------------------------------------------------------
// R7 (MUST NOT retry), R8 (ensure session before write)
// ---------------------------------------------------------------------------

export async function createFeature(input: {
  title: string;
  description: string;
}): Promise<ApiResult<{ feature_id: string; state: "screening" }>> {
  return apiFetch<{ feature_id: string; state: "screening" }>("/api/features", {
    method: "POST",
    body: input,
    auth: true,
  });
}

export async function upvote(
  id: string,
): Promise<ApiResult<{ feature_id: string; upvotes: number }>> {
  return apiFetch<{ feature_id: string; upvotes: number }>(
    `/api/features/${encodeURIComponent(id)}/upvote`,
    { method: "POST", auth: true },
  );
}

// ---------------------------------------------------------------------------
// R16–R19: Realtime subscription
// ---------------------------------------------------------------------------

export function subscribe(handlers: {
  onFeatureInsert?: (row: Feature) => void;
  onFeatureUpdate?: (row: Feature) => void;
}): () => void {
  const sb = getSupabase();

  let channel: RealtimeChannel | null = null;

  // Build channel with both INSERT and UPDATE listeners (R16)
  channel = sb
    .channel("feature_requests_changes")
    .on(
      "postgres_changes" as "postgres_changes",
      { event: "INSERT", schema: "public", table: "feature_requests" },
      (payload) => {
        // R17: hand Realtime rows unmodified — wire shape matches REST shape (R2)
        // R19: row may be incomplete (no children); caller merges
        if (handlers.onFeatureInsert && payload.new) {
          handlers.onFeatureInsert(payload.new as Feature);
        }
      },
    )
    .on(
      "postgres_changes" as "postgres_changes",
      { event: "UPDATE", schema: "public", table: "feature_requests" },
      (payload) => {
        if (handlers.onFeatureUpdate && payload.new) {
          handlers.onFeatureUpdate(payload.new as Feature);
        }
      },
    )
    .subscribe((status) => {
      // R18: reconnect on connection loss — Supabase client handles reconnection
      // automatically. We just ensure we never throw into the caller.
      if (status === "CHANNEL_ERROR") {
        // The Supabase client will attempt reconnection automatically.
        // We intentionally swallow the error so the board degrades to stale,
        // never to broken.
      }
    });

  // Return unsubscribe function
  return () => {
    if (channel) {
      sb.removeChannel(channel);
      channel = null;
    }
  };
}