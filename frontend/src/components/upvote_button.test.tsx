/**
 * Contract tests for UpvoteButton — the forum's signature control.
 *
 * No network, no api_client: the button takes a number and a callback, which is
 * what lets both the card and the unlock tree reuse it.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, act, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { UpvoteButton } from './upvote_button';

afterEach(() => vi.useRealTimers());

function renderBtn(over: Partial<React.ComponentProps<typeof UpvoteButton>> = {}) {
  const onVote = vi.fn();
  const utils = render(<UpvoteButton count={3} voted={false} onVote={onVote} {...over} />);
  return { onVote, ...utils };
}

// ===========================================================================
// R1 / R2 / R3 — shape and state
// ===========================================================================

describe('R1 — anatomy', () => {
  it('renders arrow, count and word in the frozen order', () => {
    const { container } = renderBtn();
    const spans = Array.from(container.querySelectorAll('button > span'));
    expect(spans.map((s) => s.className)).toEqual(['upvote-arrow', 'upvote-count', 'upvote-word']);
  });

  it('is a real button', () => {
    renderBtn();
    expect(screen.getByRole('button').tagName).toBe('BUTTON');
  });
});

describe('R2 — the hype wording', () => {
  it('reads "Hype it" before voting', () => {
    renderBtn();
    expect(screen.getByText('Hype it')).toBeInTheDocument();
  });

  it('reads "Hyped!" after voting', () => {
    renderBtn({ voted: true });
    expect(screen.getByText('Hyped!')).toBeInTheDocument();
  });
});

describe('R3 — variants', () => {
  it('adds is-voted when voted', () => {
    renderBtn({ voted: true });
    expect(screen.getByRole('button').className).toContain('is-voted');
  });

  it('adds is-small for the unlock tree variant', () => {
    renderBtn({ small: true });
    expect(screen.getByRole('button').className).toContain('is-small');
  });

  it('carries neither modifier by default', () => {
    renderBtn();
    const cls = screen.getByRole('button').className;
    expect(cls).not.toContain('is-voted');
    expect(cls).not.toContain('is-small');
  });
});

// ===========================================================================
// R4 / R8 — a press that should not happen, does not
// ===========================================================================

describe('R4 — refused presses', () => {
  it('calls onVote on a first press', async () => {
    const { onVote } = renderBtn();
    await userEvent.click(screen.getByRole('button'));
    expect(onVote).toHaveBeenCalledTimes(1);
  });

  it('does not call onVote when already voted', async () => {
    const { onVote } = renderBtn({ voted: true });
    await userEvent.click(screen.getByRole('button'));
    expect(onVote).not.toHaveBeenCalled();
  });

  it('does not call onVote while a vote is in flight', async () => {
    const { onVote } = renderBtn({ disabled: true });
    await userEvent.click(screen.getByRole('button'));
    expect(onVote).not.toHaveBeenCalled();
  });
});

describe('R8 — in-flight state is real, not painted on', () => {
  it('reflects disabled on the DOM node', () => {
    renderBtn({ disabled: true });
    expect(screen.getByRole('button')).toBeDisabled();
  });
});

// ===========================================================================
// R5 / R6 — the burst
// ===========================================================================

describe('R5 — eight particles, each with its own vector', () => {
  it('throws exactly eight particles on an accepted press', async () => {
    const { container } = renderBtn();
    await userEvent.click(screen.getByRole('button'));
    const burst = container.querySelector('.burst');
    expect(burst).not.toBeNull();
    expect(burst!.children).toHaveLength(8);
  });

  it('gives each particle a distinct angle and a distance', async () => {
    const { container } = renderBtn();
    await userEvent.click(screen.getByRole('button'));
    const styles = Array.from(container.querySelectorAll('.burst > *')).map((el) =>
      el.getAttribute('style') ?? '',
    );
    // i * 45deg for i in 0..7 — every particle points somewhere different.
    for (const deg of [0, 45, 90, 135, 180, 225, 270, 315]) {
      expect(styles.some((s) => s.includes(`${deg}deg`))).toBe(true);
    }
    expect(styles.every((s) => /--d:\s*\d+px/.test(s))).toBe(true);
  });

  it('throws no burst on a refused press', async () => {
    const { container } = renderBtn({ voted: true });
    await userEvent.click(screen.getByRole('button'));
    expect(container.querySelector('.burst')).toBeNull();
  });

  it('sweeps the burst back out of the DOM rather than leaking it', () => {
    // fireEvent, not userEvent: userEvent's own async scheduling deadlocks
    // against fake timers, and the thing under test here is purely the timer.
    vi.useFakeTimers();
    const { container } = render(<UpvoteButton count={1} voted={false} onVote={vi.fn()} />);
    fireEvent.click(screen.getByRole('button'));
    expect(container.querySelector('.burst')).not.toBeNull();
    act(() => {
      vi.advanceTimersByTime(1500);
    });
    expect(container.querySelector('.burst')).toBeNull();
  });
});

describe('R6 — the burst is decoration, not information', () => {
  it('hides the burst from assistive technology', async () => {
    const { container } = renderBtn();
    await userEvent.click(screen.getByRole('button'));
    expect(container.querySelector('.burst')).toHaveAttribute('aria-hidden', 'true');
  });
});

// ===========================================================================
// R7 — accessible naming
// ===========================================================================

describe('R7 — the control names itself and its count', () => {
  it('names the action and the count', () => {
    renderBtn({ count: 7 });
    expect(screen.getByRole('button', { name: /upvote — 7 upvotes/i })).toBeInTheDocument();
  });

  it('switches the verb once voted', () => {
    renderBtn({ count: 7, voted: true });
    expect(screen.getByRole('button', { name: /hyped — 7 upvotes/i })).toBeInTheDocument();
  });

  it('names the feature when a label is supplied — the difference between twenty identical buttons and a usable board', () => {
    renderBtn({ count: 2, label: 'Dark mode everywhere' });
    expect(screen.getByRole('button', { name: /dark mode everywhere/i })).toBeInTheDocument();
  });

  it('exposes pressed state', () => {
    renderBtn({ voted: true });
    expect(screen.getByRole('button')).toHaveAttribute('aria-pressed', 'true');
  });
});
