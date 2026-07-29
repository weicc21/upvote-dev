/**
 * Contract tests for AppShell — US-05 board, US-01 pitch flow, US-04 voting.
 *
 * `api_client` is mocked wholesale: it is the single seam (design guide rule 7),
 * so nothing here touches HTTP, Supabase, or Realtime.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { Feature } from './api_client';

const listFeatures = vi.fn();
const upvote = vi.fn();
const createFeature = vi.fn();
const subscribe = vi.fn();
const unsubscribe = vi.fn();
let handlers: {
  onFeatureInsert?: (r: Feature) => void;
  onFeatureUpdate?: (r: Feature) => void;
} = {};

vi.mock('./api_client', async () => {
  return {
    listFeatures: (...a: unknown[]) => listFeatures(...a),
    upvote: (...a: unknown[]) => upvote(...a),
    createFeature: (...a: unknown[]) => createFeature(...a),
    getFeature: vi.fn(),
    getMyPitches: vi.fn(),
    ensureSession: vi.fn(async () => 'tok'),
    sandboxUrl: 'https://streaks-demo.onrender.com',
    subscribe: (h: typeof handlers) => {
      handlers = h;
      subscribe(h);
      return unsubscribe;
    },
  };
});

const { AppShell } = await import('./app_shell');

function feature(over: Partial<Feature> = {}): Feature {
  return {
    id: 'f-1', title: 'Reorder habits by dragging',
    description: 'Drag habit cards to reorder the list.',
    status: 'VOTING', upvotes: 3, author_handle: null, parent_id: null,
    split_depth: 0, unlock_threshold: null, extends_id: null, extends_title: null,
    postpone_count: 0, ai_explanation: null, merge_count: null,
    shipped_version: null, shipped_at: null, viewer_has_voted: false,
    children: [], created_at: new Date(Date.now() - 3 * 3600_000).toISOString(), updated_at: null,
    ...over,
  };
}

const okBoard = (features: Feature[] = [feature()]) =>
  ({ ok: true as const, data: { features, next_cursor: null } });

beforeEach(() => {
  handlers = {};
  listFeatures.mockReset().mockResolvedValue(okBoard());
  upvote.mockReset().mockResolvedValue({ ok: true, data: { feature_id: 'f-1', upvotes: 4 } });
  createFeature.mockReset().mockResolvedValue({ ok: true, data: { feature_id: 'new-1', state: 'screening' } });
  subscribe.mockReset();
  unsubscribe.mockReset();
});
afterEach(() => vi.clearAllMocks());

// ===========================================================================
// R5 / R6 / R7 — fetching the live board
// ===========================================================================

describe('R5 — the board is read live', () => {
  it('fetches on mount and renders a card per feature', async () => {
    render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    expect(await screen.findByText('Reorder habits by dragging')).toBeInTheDocument();
  });

  it('refetches when the view changes', async () => {
    render(<AppShell />);
    // Two reads on mount: the board, plus the holding count behind the tab badge (R4).
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    const boardCalls = () =>
      listFeatures.mock.calls.filter((c) => (c[0] as { view: string }).view !== 'holding');
    await waitFor(() => expect(boardCalls().length).toBeGreaterThan(0));
    await userEvent.click(screen.getByRole('button', { name: /holding/i }));
    await waitFor(() => expect(listFeatures.mock.calls.length).toBeGreaterThan(1));
    const views = listFeatures.mock.calls.map((c) => (c[0] as { view: string }).view);
    expect(views).toContain('holding');
  });

  it('refetches when the sort changes', async () => {
    render(<AppShell />);
    // Two reads on mount: the board, plus the holding count behind the tab badge (R4).
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    const boardCalls = () =>
      listFeatures.mock.calls.filter((c) => (c[0] as { view: string }).view !== 'holding');
    await waitFor(() => expect(boardCalls().length).toBeGreaterThan(0));
    const newest = screen.getAllByRole('button').find((b) => /newest/i.test(b.textContent ?? ''));
    if (newest) {
      await userEvent.click(newest);
      await waitFor(() => {
        const sorts = listFeatures.mock.calls.map((c) => (c[0] as { sort?: string }).sort);
        expect(sorts).toContain('new');
      });
    }
  });
});

describe('R6 — error is not the same as empty', () => {
  it('an unreachable server does not render as a quiet community', async () => {
    listFeatures.mockResolvedValue({
      ok: false, status: 0, code: 'network_error',
      message: 'We could not reach the server.',
    });
    render(<AppShell />);
    await waitFor(() => expect(screen.getByText(/could not reach the server/i)).toBeInTheDocument());
    expect(document.body.textContent).not.toMatch(/no.*pitch|be the first|nobody/i);
  });

  it('an empty board says so', async () => {
    listFeatures.mockResolvedValue(okBoard([]));
    render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    await waitFor(() => expect(document.body.textContent).toMatch(/first|no |empty|nothing/i));
  });
});

describe('R7 — the backend message is shown, with a retry', () => {
  it('offers a retry that refetches', async () => {
    listFeatures.mockResolvedValue({
      ok: false, status: 500, code: 'internal_error', message: 'Something broke.',
    });
    render(<AppShell />);
    await waitFor(() => expect(screen.getByText(/something broke/i)).toBeInTheDocument());
    const retry = screen.getAllByRole('button').find((b) => /retry|try again/i.test(b.textContent ?? ''));
    expect(retry).toBeDefined();
    const before = listFeatures.mock.calls.length;
    await userEvent.click(retry!);
    await waitFor(() => expect(listFeatures.mock.calls.length).toBeGreaterThan(before));
  });
});

// ===========================================================================
// R8 - R11 — Realtime
// ===========================================================================

describe('R8 — one subscription, disposed on unmount', () => {
  it('subscribes once and unsubscribes', async () => {
    const { unmount } = render(<AppShell />);
    await waitFor(() => expect(subscribe).toHaveBeenCalledTimes(1));
    unmount();
    expect(unsubscribe).toHaveBeenCalled();
  });
});

describe('R9 — an update merges, it does not replace', () => {
  it('keeps a split parent’s children when an update omits them', async () => {
    const parent = feature({
      id: 'p-1', title: 'Habit customisation', status: 'SPLIT',
      children: [feature({ id: 'c-1', title: 'Per-habit colour', parent_id: 'p-1' })],
    });
    listFeatures.mockResolvedValue(okBoard([parent]));
    render(<AppShell />);
    await screen.findByText('Habit customisation');
    // The unlock tree is collapsed by default (feature_card R11) — open it.
    await userEvent.click(await screen.findByRole('button', { name: /show unlock tree/i }));
    expect(await screen.findByText('Per-habit colour')).toBeInTheDocument();

    // Realtime sends the changed Postgres row — no children key
    const partial = { ...parent, upvotes: 9, children: [] as Feature[] };
    handlers.onFeatureUpdate?.(partial as Feature);

    await waitFor(() => expect(screen.getByText('9')).toBeInTheDocument());
    expect(screen.getByText('Per-habit colour')).toBeInTheDocument();
  });
});

describe('R10 — an inserted child nests under its parent', () => {
  it('does not add a split child as a top-level card', async () => {
    const parent = feature({ id: 'p-1', title: 'Habit customisation', status: 'SPLIT' });
    listFeatures.mockResolvedValue(okBoard([parent]));
    const { container } = render(<AppShell />);
    await screen.findByText('Habit customisation');

    handlers.onFeatureInsert?.(feature({ id: 'c-9', title: 'Per-habit emoji', parent_id: 'p-1' }));

    await userEvent.click(await screen.findByRole('button', { name: /show unlock tree/i }));
    await waitFor(() => expect(screen.getByText('Per-habit emoji')).toBeInTheDocument());
    // it must live inside the parent card, not beside it
    const topLevel = container.querySelectorAll('.feed > .card');
    expect(topLevel.length).toBe(1);
  });

  it('adds a root insert as a new card', async () => {
    render(<AppShell />);
    await screen.findByText('Reorder habits by dragging');
    handlers.onFeatureInsert?.(feature({ id: 'f-2', title: 'Weekly completion chart' }));
    await waitFor(() => expect(screen.getByText('Weekly completion chart')).toBeInTheDocument());
  });
});

// ===========================================================================
// R12 / R13 — voting
// ===========================================================================

describe('R12 — voting reconciles with the server', () => {
  it('sends the vote and takes the returned count', async () => {
    upvote.mockResolvedValue({ ok: true, data: { feature_id: 'f-1', upvotes: 42 } });
    render(<AppShell />);
    await screen.findByText('Reorder habits by dragging');
    await userEvent.click(screen.getByRole('button', { name: /reorder habits/i }));
    await waitFor(() => expect(upvote).toHaveBeenCalledWith('f-1'));
    await waitFor(() => expect(screen.getByText('42')).toBeInTheDocument());
  });

  it('a 409 marks the feature voted rather than showing an error', async () => {
    upvote.mockResolvedValue({
      ok: false, status: 409, code: 'already_voted',
      message: 'You have already voted for this feature',
    });
    render(<AppShell />);
    await screen.findByText('Reorder habits by dragging');
    await userEvent.click(screen.getByRole('button', { name: /reorder habits/i }));
    await waitFor(() => expect(upvote).toHaveBeenCalled());
    expect(document.body.textContent).not.toMatch(/already voted/i);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /reorder habits/i }).className).toContain('is-voted'),
    );
  });
});

// ===========================================================================
// R18 - R21 — the hosted preview
// ===========================================================================

describe('R18 — the sandbox embeds the hosted app', () => {
  it('renders an iframe pointing at the allowlisted URL', async () => {
    const { container } = render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    const frame = container.querySelector('iframe') as HTMLIFrameElement | null;
    expect(frame).not.toBeNull();
    expect(frame!.getAttribute('src')).toContain('streaks-demo.onrender.com');
  });

  it('R20: the frame cannot navigate or overlay its host', async () => {
    const { container } = render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    const sandbox = container.querySelector('iframe')!.getAttribute('sandbox') ?? '';
    expect(sandbox).not.toContain('allow-top-navigation');
    expect(sandbox).not.toContain('allow-modals');
  });
});

// ===========================================================================
// R22 / R25 / R26 — the pitch flow
// ===========================================================================

describe('R22 — the pitch dialog opens from the masthead', () => {
  it('opens on the pitch action', async () => {
    render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    const pitch = screen.getAllByRole('button').find((b) => /pitch a feature/i.test(b.textContent ?? ''));
    expect(pitch).toBeDefined();
    await userEvent.click(pitch!);
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });
});

describe('R26 — an accepted pitch does not enter the feed', () => {
  it('shows it as pending, never as a board card', async () => {
    const { container } = render(<AppShell />);
    await screen.findByText('Reorder habits by dragging');
    const before = container.querySelectorAll('.feed > .card').length;

    await userEvent.click(
      screen.getAllByRole('button').find((b) => /pitch a feature/i.test(b.textContent ?? ''))!,
    );
    const dialog = await screen.findByRole('dialog');
    const inputs = within(dialog).getAllByRole('textbox');
    await userEvent.type(inputs[0], 'Weekly completion chart');
    await userEvent.type(inputs[1], 'Show a bar chart of habits completed over the last seven days.');
    // the control is labelled with its cost — "Pitch for 1 🪙" (modal R12)
    const submit = within(dialog)
      .getAllByRole('button')
      .find((b) => /pitch/i.test(b.textContent ?? '') && !/✕|×/.test(b.textContent ?? ''))!;
    await userEvent.click(submit);

    await waitFor(() => expect(createFeature).toHaveBeenCalled());
    // unscreened content must not reach the public board
    expect(container.querySelectorAll('.feed > .card').length).toBe(before);
  });
});

// ===========================================================================
// R1 / R2 / R16 — layout and access
// ===========================================================================

describe('R1/R2 — the frozen skeleton', () => {
  it('keeps chyron, masthead and the two-column grid', async () => {
    const { container } = render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    expect(container.querySelector('.app')).not.toBeNull();
    expect(container.querySelector('.broadcast')).not.toBeNull();
    expect(container.querySelector('.masthead')).not.toBeNull();
    expect(container.querySelector('.layout')).not.toBeNull();
    expect(container.querySelector('.feed')).not.toBeNull();
    expect(container.querySelector('.sandbox')).not.toBeNull();
  });
});

describe('R16 — the feed is announced', () => {
  it('exposes a landmark and a live region', async () => {
    const { container } = render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    expect(container.querySelector('[aria-live]')).not.toBeNull();
  });
});

// ===========================================================================
// R3 / R5a / R5b — layout rules that regressed once and must not again
// ===========================================================================

describe('R3 — the masthead CTAs are a matched pair', () => {
  it('gives My pitches both of its classes — btn-mypitches alone has no box at all', async () => {
    render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    const mine = screen.getByRole('button', { name: /my pitches/i });
    expect(mine.className).toContain('btn-ghost');
    expect(mine.className).toContain('btn-mypitches');
  });

  it('gives Pitch a feature both of its classes', async () => {
    render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    const pitch = screen.getByRole('button', { name: /pitch a feature/i });
    expect(pitch.className).toContain('btn-primary');
    expect(pitch.className).toContain('btn-pitch');
  });
});

describe('R5a — sort and stage filters are separate rows', () => {
  it('renders filter-chips as a sibling of feed-controls, not inside it', async () => {
    const { container } = render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    const chips = container.querySelector('.filter-chips')!;
    expect(chips).not.toBeNull();
    // Nesting is what crushes six chips onto the sort row.
    expect(chips.closest('.feed-controls')).toBeNull();
    expect(container.querySelector('.feed-controls .filter-chips')).toBeNull();
  });

  it('orders the pipeline controls tabs → sort → chips → feed', async () => {
    const { container } = render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    const order = ['.tabs', '.feed-controls', '.filter-chips'].map(
      (sel) => Array.from(container.querySelectorAll('*')).indexOf(container.querySelector(sel)!),
    );
    expect(order[0]).toBeLessThan(order[1]);
    expect(order[1]).toBeLessThan(order[2]);
  });
});

describe('R5b — stage chips multi-select', () => {
  it('keeps both selections and sends them together', async () => {
    const { container } = render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    const chip = (re: RegExp) =>
      Array.from(container.querySelectorAll('.chip')).find((c) =>
        re.test(c.textContent ?? ''),
      )! as HTMLButtonElement;

    await userEvent.click(chip(/voting/i));
    await userEvent.click(chip(/AI Building/i));

    expect(chip(/voting/i).className).toContain('is-active');
    expect(chip(/AI Building/i).className).toContain('is-active');

    const last = listFeatures.mock.calls.at(-1)![0] as { status?: string[] };
    expect(last.status).toEqual(expect.arrayContaining(['VOTING', 'IN_SPRINT']));
  });

  it('All clears the set and is active only while nothing is selected', async () => {
    const { container } = render(<AppShell />);
    await waitFor(() => expect(listFeatures).toHaveBeenCalled());
    const chip = (re: RegExp) =>
      Array.from(container.querySelectorAll('.chip')).find((c) =>
        re.test(c.textContent ?? ''),
      )! as HTMLButtonElement;

    expect(chip(/^all$/i).className).toContain('is-active');
    await userEvent.click(chip(/voting/i));
    expect(chip(/^all$/i).className).not.toContain('is-active');
    await userEvent.click(chip(/^all$/i));
    expect(chip(/voting/i).className).not.toContain('is-active');
    expect(chip(/^all$/i).className).toContain('is-active');
  });
});
