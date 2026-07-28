/**
 * Contract tests for api_client — the single backend seam.
 *
 * `fetch` is stubbed; no request leaves the process. The Supabase client is not
 * exercised here beyond confirming it is not constructed at import time.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import * as api from './api_client';

const SRC = readFileSync(resolve(__dirname, 'api_client.ts'), 'utf8');

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => vi.unstubAllGlobals());

// ===========================================================================
// R4 / R5 — every call resolves; failures are values, not exceptions
// ===========================================================================

describe('R4 — never throws', () => {
  it('resolves ok on a 200', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ features: [], next_cursor: null })));
    const r = await api.listFeatures({ view: 'pipeline' });
    expect(r.ok).toBe(true);
  });

  it('resolves — not rejects — on a network failure', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
    const r = await api.listFeatures({ view: 'pipeline' });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.message).toBeTruthy();
  });

  it('resolves on an unparseable body', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>gateway</html>', { status: 502 })));
    const r = await api.listFeatures({ view: 'pipeline' });
    expect(r.ok).toBe(false);
  });
});

describe('R5 — the backend message is passed through verbatim', () => {
  it('lifts code and message from the error envelope', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      jsonResponse({ error: { code: 'invalid_view', message: 'view must be one of …' } }, 400)));
    const r = await api.listFeatures({ view: 'pipeline' });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.code).toBe('invalid_view');
      expect(r.message).toBe('view must be one of …');
    }
  });
});

// ===========================================================================
// R6 — resets_at is a sibling of error, not inside it
// ===========================================================================

describe('R6 — 429 carries resets_at', () => {
  it('surfaces resets_at from the top level of the body', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      jsonResponse({
        error: { code: 'out_of_coins', message: 'Try again tomorrow.' },
        resets_at: '2026-07-29T00:00:00+00:00',
      }, 429)));
    const r = await api.createFeature({ title: 't', description: 'd'.repeat(40) });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.code).toBe('out_of_coins');
      expect(r.resets_at).toBe('2026-07-29T00:00:00+00:00');
    }
  });
});

// ===========================================================================
// R7 — a failed write is never retried
// ===========================================================================

describe('R7 — writes are single-shot', () => {
  it('does not retry a failed createFeature', async () => {
    const f = vi.fn(async () => jsonResponse({ error: { code: 'x', message: 'y' } }, 500));
    vi.stubGlobal('fetch', f);
    await api.createFeature({ title: 't', description: 'd'.repeat(40) });
    expect(f).toHaveBeenCalledTimes(1);
  });

  it('does not retry a failed upvote', async () => {
    const f = vi.fn(async () => jsonResponse({ error: { code: 'x', message: 'y' } }, 500));
    vi.stubGlobal('fetch', f);
    await api.upvote('f-1');
    expect(f).toHaveBeenCalledTimes(1);
  });
});

// ===========================================================================
// R13 / R14 — query shape and the cursor
// ===========================================================================

describe('R13 — the frozen query names', () => {
  it('sends view and sort, and status as CSV', async () => {
    const f = vi.fn(async (..._a: unknown[]) => jsonResponse({ features: [], next_cursor: null }));
    vi.stubGlobal('fetch', f);
    await api.listFeatures({ view: 'pipeline', sort: 'top', status: ['VOTING', 'SPLIT'] });
    const url = String(f.mock.calls[0]?.[0]);
    expect(url).toContain('view=pipeline');
    expect(url).toContain('sort=top');
    expect(decodeURIComponent(url)).toContain('VOTING,SPLIT');
  });

  it('never sends the wrong parameter names', async () => {
    const f = vi.fn(async (..._a: unknown[]) => jsonResponse({ features: [], next_cursor: null }));
    vi.stubGlobal('fetch', f);
    await api.listFeatures({ view: 'pipeline', sort: 'new' });
    const url = String(f.mock.calls[0]?.[0]);
    for (const wrong of ['newest', 'page=', 'page_size']) {
      expect(url).not.toContain(wrong);
    }
  });
});

describe('R14 — next_cursor reaches the caller', () => {
  it('returns the cursor rather than swallowing it', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      jsonResponse({ features: [], next_cursor: 'cursor-abc' })));
    const r = await api.listFeatures({ view: 'pipeline' });
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.data.next_cursor).toBe('cursor-abc');
  });
});

// ===========================================================================
// R20 / R21 — the sandbox URL is validated before it becomes an iframe src
// ===========================================================================

describe('R21 — sandbox host allowlist', () => {
  it('exports either null or an https onrender.com URL — never anything else', () => {
    const u = api.sandboxUrl;
    if (u !== null) {
      const parsed = new URL(u);
      expect(parsed.protocol).toBe('https:');
      expect(parsed.hostname.endsWith('.onrender.com')).toBe(true);
    }
  });

  it('the validator rejects non-https and foreign hosts', () => {
    // The guard must not be a bare env passthrough — an unchecked iframe src
    // is an injection vector.
    expect(SRC).toMatch(/protocol\s*!==\s*["']https:["']/);
    expect(SRC).toMatch(/onrender\.com/);
  });
});

// ===========================================================================
// Static guarantees — read off the source
// ===========================================================================

describe('R12 — configuration comes from env, not literals', () => {
  it('contains no hardcoded localhost', () => {
    expect(SRC).not.toMatch(/localhost:\d+/);
  });

  it('reads the base URL from import.meta.env', () => {
    expect(SRC).toContain('import.meta.env');
  });
});

describe('R16 — Realtime follows INSERT *and* UPDATE', () => {
  it('subscribes to both events on feature_requests', () => {
    expect(SRC).toContain('INSERT');
    expect(SRC).toContain('UPDATE');
    expect(SRC).toContain('feature_requests');
  });

  it('returns an unsubscribe function', () => {
    expect(SRC).toMatch(/export function subscribe[\s\S]{0,600}=>\s*(void|\{)/);
  });
});

describe('R1 / R3 — the wire types live here', () => {
  it('exports the shapes components depend on', () => {
    expect(SRC).toMatch(/export interface Feature\b/);
    expect(SRC).toMatch(/export interface PendingPitch\b/);
    expect(SRC).toMatch(/export type ApiResult\b/);
  });

  it('types children as a non-optional list', () => {
    expect(SRC).toMatch(/children:\s*Feature\[\]/);
    expect(SRC).not.toMatch(/children\?:/);
  });
});

describe('R11 — no token ever reaches a log', () => {
  it('does not console-log around the auth header', () => {
    expect(SRC).not.toMatch(/console\.(log|info|warn|error)\([^)]*[Tt]oken/);
  });
});
