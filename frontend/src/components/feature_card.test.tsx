/**
 * Contract tests for the four card shapes — US-05 board, US-04 voting,
 * US-08 holding, US-10 shipped, US-16 vault reboot.
 *
 * No network, no api_client: each card takes its data and its callbacks as
 * props, which is what makes it testable in isolation (design guide rule 7).
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { FeatureCard, HoldingCard, ShippedCard, VaultCard } from './feature_card';
import type { Feature, Status } from '../api_client';

const NO_VOTES: ReadonlySet<string> = new Set<string>();

function feature(over: Partial<Feature> = {}): Feature {
  return {
    id: 'f-1',
    title: 'Reorder habits by dragging',
    description: 'Let a user drag habit cards up and down to reorder the list.',
    status: 'VOTING',
    upvotes: 3,
    author_handle: 'mika.dev',
    parent_id: null,
    split_depth: 0,
    unlock_threshold: null,
    extends_id: null,
    extends_title: null,
    postpone_count: 0,
    ai_explanation: null,
    merge_count: null,
    shipped_version: null,
    shipped_at: null,
    viewer_has_voted: false,
    children: [],
    created_at: new Date(Date.now() - 3 * 3600_000).toISOString(),
    updated_at: null,
    ...over,
  };
}

function renderCard(
  over: Partial<Feature> = {},
  props: Partial<React.ComponentProps<typeof FeatureCard>> = {},
) {
  const onUpvote = vi.fn();
  const utils = render(
    <FeatureCard feature={feature(over)} onUpvote={onUpvote} votedIds={NO_VOTES} {...props} />,
  );
  return { onUpvote, ...utils };
}

// ===========================================================================
// R1 / R2 / R3 — anatomy and content
// ===========================================================================

describe('R1 — frozen anatomy', () => {
  it('nests card → card-main → card-copy in the shape the stylesheet lays out', () => {
    const { container } = renderCard();
    expect(container.querySelector('article.card .card-main .card-copy .card-title')).not.toBeNull();
  });

  it('carries the status as a class hook', () => {
    const { container } = renderCard({ status: 'COMPILED' });
    expect(container.querySelector('.status-COMPILED')).not.toBeNull();
  });
});

describe('R2 / R3 — the idea and its byline', () => {
  it('renders title and description', () => {
    renderCard();
    expect(screen.getByText('Reorder habits by dragging')).toBeInTheDocument();
    expect(screen.getByText(/drag habit cards/i)).toBeInTheDocument();
  });

  it('renders the author and a relative time, not a raw timestamp', () => {
    const { container } = renderCard();
    const byline = container.querySelector('.card-byline')!;
    expect(byline.textContent).toContain('mika.dev');
    expect(byline.textContent).toMatch(/\d+(m|h|d) ago/);
    expect(byline.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}/);
  });
});

// ===========================================================================
// R4 / R5 / R6 — stage pills name the agent, never the enum
// ===========================================================================

describe('R4 — stage pills', () => {
  const cases: Array<[Status, RegExp]> = [
    ['CONSOLIDATING', /AI Merging Duplicates/i],
    ['IN_SPRINT', /AI Building/i],
    ['COMPILED', /Live in Sandbox/i],
    ['SPLIT', /AI Evolving/i],
  ];

  it.each(cases)('%s names the agent at work', (status, copy) => {
    renderCard({ status });
    expect(screen.getByText(copy)).toBeInTheDocument();
  });

  it('never shows a raw enum label to a reader', () => {
    const { container } = renderCard({ status: 'CONSOLIDATING', merge_count: 4 });
    expect(container.textContent).not.toContain('CONSOLIDATING');
  });
});

describe('R5 — a votable card needs no pill', () => {
  it('renders no pill at all on VOTING — the live button is the status', () => {
    const { container } = renderCard({ status: 'VOTING' });
    expect(container.querySelector('.pill')).toBeNull();
  });
});

describe('R6 — an unknown status degrades quietly', () => {
  it('renders without a pill and without leaking the enum', () => {
    const { container } = renderCard({ status: 'FUTURE_STATUS' as Status });
    expect(container.querySelector('.pill')).toBeNull();
    expect(container.textContent).not.toContain('FUTURE_STATUS');
    expect(screen.getByText('Reorder habits by dragging')).toBeInTheDocument();
  });
});

// ===========================================================================
// R7 / R8 / R9 — voting is open only where voting is open
// ===========================================================================

describe('R7 — only VOTING rows carry a live control', () => {
  it('renders the hype button on VOTING', async () => {
    const { onUpvote } = renderCard({ status: 'VOTING' });
    await userEvent.click(screen.getByRole('button', { name: /upvote/i }));
    expect(onUpvote).toHaveBeenCalledWith('f-1');
  });

  it.each(['CONSOLIDATING', 'IN_SPRINT', 'COMPILED', 'SPLIT'] as Status[])(
    '%s shows a frozen count instead of a button',
    (status) => {
      const { container } = renderCard({ status, upvotes: 12 });
      expect(container.querySelector('.vote-frozen')).not.toBeNull();
      expect(container.querySelector('.upvote')).toBeNull();
      expect(container.querySelector('.vote-frozen')!.textContent).toContain('12');
    },
  );
});

describe('R8 — a returning visitor is not invited to re-vote', () => {
  it('shows voted state from votedIds', () => {
    renderCard({}, { votedIds: new Set(['f-1']) });
    expect(screen.getByRole('button', { name: /hyped/i })).toBeInTheDocument();
  });

  it('shows voted state from viewer_has_voted', () => {
    renderCard({ viewer_has_voted: true });
    expect(screen.getByRole('button', { name: /hyped/i })).toBeInTheDocument();
  });
});

describe('R9 — a double click cannot send two votes', () => {
  it('disables the control while this feature is in flight', async () => {
    const { onUpvote } = renderCard({}, { pendingVoteId: 'f-1' });
    await userEvent.click(screen.getByRole('button', { name: /upvote/i }));
    expect(onUpvote).not.toHaveBeenCalled();
  });

  it('leaves other features alone', async () => {
    const { onUpvote } = renderCard({}, { pendingVoteId: 'someone-else' });
    await userEvent.click(screen.getByRole('button', { name: /upvote/i }));
    expect(onUpvote).toHaveBeenCalledWith('f-1');
  });
});

// ===========================================================================
// R10 — dedup left visible evidence
// ===========================================================================

describe('R10 — dedup is visible', () => {
  it('shows how many requests merged', () => {
    renderCard({ status: 'CONSOLIDATING', merge_count: 42 });
    expect(screen.getByText(/42 matching requests found/i)).toBeInTheDocument();
  });

  it('shows a builds-on chip when extends_title is set', () => {
    renderCard({ extends_title: 'Login with email' });
    expect(screen.getByText(/login with email/i)).toBeInTheDocument();
  });

  it('shows no merge note at zero or null', () => {
    const { container } = renderCard({ status: 'CONSOLIDATING', merge_count: 0 });
    expect(container.querySelector('.merge-note')).toBeNull();
  });
});

// ===========================================================================
// R11–R16 — the unlock tree
// ===========================================================================

const splitParent = (childOver: Partial<Feature>[] = []) =>
  feature({
    id: 'p-1',
    title: 'Full statistics dashboard',
    status: 'SPLIT',
    children:
      childOver.length > 0
        ? childOver.map((o, i) => feature({ id: `c-${i}`, parent_id: 'p-1', ...o }))
        : [
            feature({ id: 'c-1', title: 'Completion-rate chart', parent_id: 'p-1', upvotes: 61, unlock_threshold: 50 }),
            feature({ id: 'c-2', title: 'Best-day insight', parent_id: 'p-1', upvotes: 12, unlock_threshold: 50 }),
          ],
  });

function renderSplit(f: Feature = splitParent()) {
  const onUpvote = vi.fn();
  const utils = render(<FeatureCard feature={f} onUpvote={onUpvote} votedIds={NO_VOTES} />);
  return { onUpvote, ...utils };
}

describe('R11 — the tree is collapsed until asked for', () => {
  it('hides the children behind a toggle', () => {
    renderSplit();
    expect(screen.queryByText('Completion-rate chart')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /show unlock tree/i })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
  });

  it('reveals them on press and flips the label', async () => {
    renderSplit();
    await userEvent.click(screen.getByRole('button', { name: /show unlock tree/i }));
    expect(screen.getByText('Completion-rate chart')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /hide unlock tree/i })).toBeInTheDocument();
  });
});

describe('R12 / R13 — progress toward the unlock', () => {
  it('heads the tree with the parent and the unlocked tally', async () => {
    renderSplit();
    await userEvent.click(screen.getByRole('button', { name: /show unlock tree/i }));
    expect(screen.getByText(/Evolved from/i)).toBeInTheDocument();
    expect(screen.getByText('1/2 unlocked')).toBeInTheDocument();
  });

  it('marks a child past its threshold as unlocked and withdraws its button', async () => {
    const { container } = renderSplit();
    await userEvent.click(screen.getByRole('button', { name: /show unlock tree/i }));
    const done = container.querySelector('.unlock-node.is-unlocked')!;
    expect(within(done as HTMLElement).getByText('Unlocked!')).toBeInTheDocument();
    expect(done.querySelector('.upvote')).toBeNull();
  });

  it('shows the remaining votes on a locked child, with a live button', async () => {
    const { container, onUpvote } = renderSplit();
    await userEvent.click(screen.getByRole('button', { name: /show unlock tree/i }));
    const locked = Array.from(container.querySelectorAll('.unlock-node')).find(
      (n) => !n.className.includes('is-unlocked'),
    )! as HTMLElement;
    expect(within(locked).getByText('12 / 50 votes to unlock')).toBeInTheDocument();
    await userEvent.click(within(locked).getByRole('button', { name: /upvote/i }));
    expect(onUpvote).toHaveBeenCalledWith('c-2');
  });
});

describe('R14 — a null threshold invents no goal', () => {
  it('renders no bar, keeps the child votable, and never implies it is unlocked', async () => {
    const { container, onUpvote } = renderSplit(
      splitParent([{ id: 'c-0', title: 'No threshold yet', upvotes: 4, unlock_threshold: null }]),
    );
    await userEvent.click(screen.getByRole('button', { name: /show unlock tree/i }));
    expect(container.querySelector('.unlock-bar')).toBeNull();
    expect(container.querySelector('.is-unlocked')).toBeNull();
    expect(container.textContent).not.toMatch(/\/\s*0\s*votes/);
    await userEvent.click(screen.getByRole('button', { name: /upvote/i }));
    expect(onUpvote).toHaveBeenCalledWith('c-0');
  });
});

describe('R15 — the bar is the stylesheet\'s bar', () => {
  it('is a div wrapping a width-carrying span, not a native <progress>', async () => {
    const { container } = renderSplit();
    await userEvent.click(screen.getByRole('button', { name: /show unlock tree/i }));
    expect(container.querySelector('progress')).toBeNull();
    const bar = container.querySelector('.unlock-bar')!;
    expect(bar.tagName).toBe('DIV');
    const fill = bar.querySelector('span')!;
    expect(fill.getAttribute('style') ?? '').toMatch(/width:\s*\d+(\.\d+)?%/);
  });

  it('R15a: exposes its value and a name', async () => {
    const { container } = renderSplit();
    await userEvent.click(screen.getByRole('button', { name: /show unlock tree/i }));
    const bar = container.querySelector('[role="progressbar"]')!;
    expect(bar).toHaveAttribute('aria-valuenow');
    expect(bar).toHaveAttribute('aria-valuemax', '50');
    expect(bar.getAttribute('aria-label')).toMatch(/completion-rate chart/i);
  });
});

describe('R16 — no children, no tree', () => {
  it('renders nothing extra when children is empty', () => {
    const { container } = renderCard({ status: 'SPLIT', children: [] });
    expect(container.querySelector('.unlock-tree')).toBeNull();
  });
});

// ===========================================================================
// R17 / R18 — HoldingCard
// ===========================================================================

describe('R17 / R18 — a holding card carries its reason', () => {
  const held = feature({
    status: 'POSTPONED_CONFLICT',
    title: 'Sync my habits across devices',
    postpone_count: 1,
    upvotes: 64,
    ai_explanation: 'Cross-device sync needs accounts and a server, but this app is client-only.',
  });

  it('states the paradox and the cycle', () => {
    render(<HoldingCard feature={held} />);
    expect(screen.getByText(/Structural Paradox Detected/i)).toBeInTheDocument();
    expect(screen.getByText(/holding cycle 1 of 2/i)).toBeInTheDocument();
  });

  it('explains the pause in the agent\'s own words', () => {
    render(<HoldingCard feature={held} />);
    expect(screen.getByText(/🤖 Why the pause/i)).toBeInTheDocument();
    expect(screen.getByText(/needs accounts and a server/i)).toBeInTheDocument();
  });

  it('freezes the vote count rather than inviting a press', () => {
    const { container } = render(<HoldingCard feature={held} />);
    expect(container.querySelector('.vote-frozen')!.textContent).toContain('64');
    expect(container.querySelector('.upvote')).toBeNull();
  });

  it('never leaks the enum', () => {
    const { container } = render(<HoldingCard feature={held} />);
    expect(container.textContent).not.toContain('POSTPONED_CONFLICT');
  });
});

// ===========================================================================
// R19 — ShippedCard
// ===========================================================================

describe('R19 — a shipped card is a trophy', () => {
  const shipped = feature({
    status: 'COMPILED',
    title: 'Refreshed onboarding tour',
    shipped_version: 'v0.4.1',
    shipped_at: new Date(Date.now() - 26 * 3600_000).toISOString(),
    author_handle: 'hexadecimal',
    upvotes: 216,
  });

  it('names the version it landed in', () => {
    render(<ShippedCard feature={shipped} />);
    expect(screen.getByText(/🏆 Shipped in v0\.4\.1/)).toBeInTheDocument();
  });

  it('credits the pitcher — the reward they actually came for', () => {
    const { container } = render(<ShippedCard feature={shipped} />);
    const credit = container.querySelector('.shipped-credit')!;
    expect(credit.textContent).toMatch(/pitched by/i);
    expect(credit.textContent).toMatch(/live in the sandbox/i);
    expect(credit.querySelector('strong')!.textContent).toBe('hexadecimal');
  });
});

// ===========================================================================
// R20 — VaultCard (US-16)
// ===========================================================================

describe('R20 — the vault has a way out', () => {
  const archived = feature({ status: 'ARCHIVED', title: '3D animated habit mascot', upvotes: 11 });

  it('offers a reboot control', () => {
    render(<VaultCard feature={archived} onReboot={vi.fn()} />);
    expect(screen.getByRole('button', { name: /reboot request/i })).toBeInTheDocument();
  });

  it('reports the id to the shell', async () => {
    const onReboot = vi.fn();
    render(<VaultCard feature={archived} onReboot={onReboot} />);
    await userEvent.click(screen.getByRole('button', { name: /reboot request/i }));
    expect(onReboot).toHaveBeenCalledWith('f-1');
  });

  it('explains why it was archived instead of showing the original scope', () => {
    render(<VaultCard feature={archived} onReboot={vi.fn()} />);
    expect(screen.getByText(/low community velocity/i)).toBeInTheDocument();
  });
});

// ===========================================================================
// R21 / R22 — identity and accessibility
// ===========================================================================

describe('R21 — no ids are rendered', () => {
  it('never prints a uuid', () => {
    const { container } = renderCard({ id: '3f8c1a22-9b4d-4e7a-8c11-77aa2b3c4d5e' });
    expect(container.textContent).not.toContain('3f8c1a22');
  });

  it('omits the byline when author_handle is null rather than printing a placeholder', () => {
    const { container } = renderCard({ author_handle: null });
    expect(container.textContent).not.toMatch(/anonymous|unknown|null/i);
  });
});

describe('R22 — every vote control names its feature', () => {
  it('names the parent on its own button', () => {
    renderCard({ title: 'Custom emoji icon per habit' });
    expect(
      screen.getByRole('button', { name: /custom emoji icon per habit/i }),
    ).toBeInTheDocument();
  });

  it('names each child in the unlock tree', async () => {
    renderSplit();
    await userEvent.click(screen.getByRole('button', { name: /show unlock tree/i }));
    expect(screen.getByRole('button', { name: /best-day insight/i })).toBeInTheDocument();
  });
});

describe('R23 — keyboard operable', () => {
  it('reaches the upvote control by tab and fires it with the keyboard', async () => {
    const { onUpvote } = renderCard();
    await userEvent.tab();
    await userEvent.keyboard('{Enter}');
    expect(onUpvote).toHaveBeenCalledWith('f-1');
  });

  it('uses no click-handling divs', () => {
    const { container } = renderCard({ status: 'SPLIT', children: [feature({ id: 'c-9' })] });
    expect(container.querySelectorAll('div[onclick]')).toHaveLength(0);
  });
});


// ===========================================================================
// R3a — the timestamp does not depend on having a handle
// ===========================================================================

describe('R3a — an account-less pitch still shows when it was pitched', () => {
  it('shows the relative time with no handle, and no placeholder', () => {
    const { container } = renderCard({ author_handle: null });
    const byline = container.querySelector('.card-byline');
    expect(byline).not.toBeNull();
    expect(byline!.textContent).toMatch(/\d+(m|h|d) ago/);
    expect(byline!.textContent).not.toMatch(/anonymous|unknown|null|·/i);
  });

  it('shows handle · time when a handle exists', () => {
    const { container } = renderCard({ author_handle: 'keen.cedar61' });
    const byline = container.querySelector('.card-byline')!;
    expect(byline.textContent).toContain('keen.cedar61');
    expect(byline.textContent).toContain('·');
    expect(byline.textContent).toMatch(/\d+(m|h|d) ago/);
  });
});
