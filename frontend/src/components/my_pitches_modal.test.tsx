/**
 * Contract tests for MyPitchesModal — US-06, the author's private view.
 *
 * The shell owns the read; this dialog is handed both lists, so nothing here
 * touches the network.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { MyPitchesModal } from './my_pitches_modal';
import type { Feature, PendingPitch, Status } from '../api_client';

function pending(over: Partial<PendingPitch> = {}): PendingPitch {
  return {
    feature_id: 'p-1',
    title: 'Confetti on a 7-day streak',
    state: 'screening',
    reason: null,
    shipped_version: null,
    merged_into_feature_id: null,
    merged_into_title: null,
    submitted_at: new Date().toISOString(),
    ...over,
  };
}

function feature(over: Partial<Feature> = {}): Feature {
  return {
    id: 'f-1', title: 'Export my history as CSV', description: 'A button in settings.',
    status: 'VOTING' as Status, upvotes: 41, author_handle: 'jinnn', parent_id: null,
    split_depth: 0, unlock_threshold: null, extends_id: null, extends_title: null,
    postpone_count: 0, ai_explanation: null, merge_count: null, shipped_version: null,
    shipped_at: null, viewer_has_voted: false, children: [],
    created_at: new Date().toISOString(), updated_at: null,
    ...over,
  };
}

function setup(over: Partial<React.ComponentProps<typeof MyPitchesModal>> = {}) {
  const onClose = vi.fn();
  const onDismiss = vi.fn();
  const utils = render(
    <MyPitchesModal
      open
      onClose={onClose}
      pending={[]}
      features={[]}
      onDismiss={onDismiss}
      {...over}
    />,
  );
  return { onClose, onDismiss, ...utils };
}

// ===========================================================================
// R1 — the dialog says it is private
// ===========================================================================

describe('R1 — private by construction', () => {
  it('renders nothing when closed', () => {
    const { container } = setup({ open: false });
    expect(container).toBeEmptyDOMElement();
  });

  it('names itself and states that only the author can see it', () => {
    setup();
    expect(screen.getByRole('heading', { name: /my pitches/i })).toBeInTheDocument();
    expect(screen.getByText(/only you can see these/i)).toBeInTheDocument();
  });
});

// ===========================================================================
// R2 / R3 — pending entries
// ===========================================================================

describe('R3 — a pitch in screening', () => {
  it('shows the shield and says who is looking at it', () => {
    const { container } = setup({ pending: [pending()] });
    expect(screen.getByText(/Guardagent is screening your pitch/i)).toBeInTheDocument();
    expect(container.querySelector('.shield')).not.toBeNull();
  });

  it('offers nothing to dismiss — there is no outcome yet', () => {
    setup({ pending: [pending()] });
    expect(screen.queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument();
  });

  it('shows the title the author typed', () => {
    setup({ pending: [pending()] });
    expect(screen.getByText('Confetti on a 7-day streak')).toBeInTheDocument();
  });
});

describe('R2 — unresolved first', () => {
  it('puts pending entries above already-public features', () => {
    const { container } = setup({
      pending: [pending({ title: 'Still screening' })],
      features: [feature({ title: 'Already public' })],
    });
    const text = container.textContent!;
    expect(text.indexOf('Still screening')).toBeLessThan(text.indexOf('Already public'));
  });
});

// ===========================================================================
// R4 / R5 — rejection copy is actionable, never generic, never raw
// ===========================================================================

describe('R4 — every reason gets its own sentence', () => {
  const cases: Array<[PendingPitch['reason'], RegExp]> = [
    ['security', /rephrase and try again/i],
    ['off_topic', /outside what this app is about/i],
    ['unclear', /describe different things/i],
  ];

  it.each(cases)('%s reads as advice, not a verdict', (reason, copy) => {
    setup({ pending: [pending({ state: 'rejected', reason })] });
    expect(screen.getByText(copy)).toBeInTheDocument();
  });

  it('interpolates the version for an already-shipped pitch', () => {
    setup({
      pending: [pending({ state: 'rejected', reason: 'already_shipped', shipped_version: 'v0.4.1' })],
    });
    expect(screen.getByText(/already shipped in v0\.4\.1/i)).toBeInTheDocument();
  });

  it('marks a rejected row as such', () => {
    const { container } = setup({ pending: [pending({ state: 'rejected', reason: 'security' })] });
    expect(container.querySelector('.pending-card.is-rejected')).not.toBeNull();
  });
});

describe('R5 — the machine\'s reasoning stays private', () => {
  it('shows no verdict, confidence, or model detail', () => {
    const { container } = setup({ pending: [pending({ state: 'rejected', reason: 'security' })] });
    const text = container.textContent!;
    for (const leak of ['verdict', 'confidence', 'model', 'prompt', 'security']) {
      expect(text.toLowerCase()).not.toContain(leak);
    }
  });
});

// ===========================================================================
// R6 — merged is a win
// ===========================================================================

describe('R6 — a merge is celebrated, not apologised for', () => {
  const merged = pending({
    state: 'merged',
    merged_into_feature_id: 'f-9',
    merged_into_title: 'Dark mode everywhere',
  });

  it('names the feature the idea joined', () => {
    setup({ pending: [merged] });
    expect(screen.getByText(/great minds/i)).toBeInTheDocument();
    expect(screen.getByText(/dark mode everywhere/i)).toBeInTheDocument();
  });

  it('is not styled as a rejection', () => {
    const { container } = setup({ pending: [merged] });
    expect(container.querySelector('.pending-card.is-rejected')).toBeNull();
  });
});

// ===========================================================================
// R7 — clearing terminal entries
// ===========================================================================

describe('R7 — terminal entries can be cleared', () => {
  it('reports the id to the shell on dismiss', async () => {
    const { onDismiss } = setup({
      pending: [pending({ state: 'rejected', reason: 'security', feature_id: 'p-42' })],
    });
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(onDismiss).toHaveBeenCalledWith('p-42');
  });

  it('offers dismiss on a merged entry too', () => {
    setup({ pending: [pending({ state: 'merged', merged_into_title: 'X' })] });
    expect(screen.getByRole('button', { name: /dismiss/i })).toBeInTheDocument();
  });
});

// ===========================================================================
// R8 — the author's public features are status only
// ===========================================================================

describe('R8 — a read-only status list', () => {
  it('shows the title, stage and count', () => {
    setup({ features: [feature({ title: 'Export my history as CSV', upvotes: 41 })] });
    expect(screen.getByText('Export my history as CSV')).toBeInTheDocument();
    expect(screen.getByText(/41/)).toBeInTheDocument();
  });

  it('offers no vote control — the board is where you interact with it', () => {
    const { container } = setup({ features: [feature()] });
    expect(container.querySelector('.upvote')).toBeNull();
  });

  it('names the stage in the community\'s language, never the enum', () => {
    const { container } = setup({ features: [feature({ status: 'COMPILED', shipped_version: 'v0.4.1' })] });
    expect(screen.getByText(/live in sandbox/i)).toBeInTheDocument();
    expect(container.textContent).not.toContain('COMPILED');
  });
});

// ===========================================================================
// R9 / R10 — empty, loading and error are three different facts
// ===========================================================================

describe('R9 — an inviting empty state', () => {
  it('invites a first pitch when both lists are empty', () => {
    const { container } = setup();
    expect(container.querySelector('.pitches-empty')).not.toBeNull();
    expect(screen.getByText(/nothing in screening right now/i)).toBeInTheDocument();
  });
});

describe('R10 — an unreachable server is not an empty tray', () => {
  it('shows a loading state distinct from empty', () => {
    setup({ loading: true });
    expect(screen.queryByText(/nothing in screening right now/i)).not.toBeInTheDocument();
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  it('shows the error and a retry rather than "you have no pitches"', async () => {
    const onRetry = vi.fn();
    setup({ error: 'We could not reach the server.', onRetry });
    expect(screen.getByText(/could not reach the server/i)).toBeInTheDocument();
    expect(screen.queryByText(/nothing in screening right now/i)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /retry|try again/i }));
    expect(onRetry).toHaveBeenCalled();
  });
});

// ===========================================================================
// R11 / R12 / R13 — dialog behaviour
// ===========================================================================

describe('R11 — a real dialog with real exits', () => {
  it('is a modal dialog with an accessible name', () => {
    setup();
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(within(dialog).getByRole('heading', { name: /my pitches/i })).toBeInTheDocument();
  });

  it('closes on the ✕ control', async () => {
    const { onClose } = setup();
    await userEvent.click(screen.getByRole('button', { name: /close/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it('closes on Escape', async () => {
    const { onClose } = setup();
    await userEvent.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalled();
  });

  it('closes on a backdrop click but not on a click inside the dialog', async () => {
    const { onClose, container } = setup();
    await userEvent.click(screen.getByRole('dialog'));
    expect(onClose).not.toHaveBeenCalled();
    await userEvent.click(container.querySelector('.modal-backdrop')!);
    expect(onClose).toHaveBeenCalled();
  });
});

describe('R13 — no ids are rendered', () => {
  it('never prints a feature_id', () => {
    const { container } = setup({
      pending: [pending({ feature_id: '3f8c1a22-9b4d-4e7a-8c11-77aa2b3c4d5e' })],
    });
    expect(container.textContent).not.toContain('3f8c1a22');
  });
});
