/**
 * Contract tests for Broadcast — the chyron (US-11).
 *
 * The ticker is driven by an interval, so these use fake timers throughout.
 * No Realtime: the live feed arrives later through the `messages` prop.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';

import { Broadcast, DEMO_BROADCAST } from './broadcast';
import type { BroadcastMessage } from './broadcast';

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

const tick = (ms: number) => act(() => { vi.advanceTimersByTime(ms); });

const SCRIPT: BroadcastMessage[] = [
  { icon: '🛡️', agent: 'Guardagent', text: 'screening two pitches' },
  { icon: '🚀', agent: 'Ship Agent', text: 'shipped it', success: true },
];

// ===========================================================================
// R1 / R2 / R7 — the strip
// ===========================================================================

describe('R1 — the frozen strip', () => {
  it('renders LIVE, the blinking dot, and the broadcast label', () => {
    const { container } = render(<Broadcast messages={SCRIPT} />);
    expect(screen.getByText('LIVE')).toBeInTheDocument();
    expect(container.querySelector('.live-dot')).not.toBeNull();
    expect(screen.getByText('AI CREATOR BROADCAST')).toBeInTheDocument();
  });
});

describe('R2 — a message names its agent', () => {
  it('renders the agent in strong, followed by the text', () => {
    const { container } = render(<Broadcast messages={SCRIPT} />);
    const msg = container.querySelector('.broadcast-msg')!;
    expect(msg.querySelector('strong')!.textContent).toBe('Guardagent');
    expect(msg.textContent).toContain('screening two pitches');
  });
});

describe('R7 — announced without interrupting', () => {
  it('is a polite live region', () => {
    const { container } = render(<Broadcast messages={SCRIPT} />);
    const strip = container.querySelector('.broadcast')!;
    expect(strip).toHaveAttribute('role', 'status');
    expect(strip).toHaveAttribute('aria-live', 'polite');
  });
});

// ===========================================================================
// R3 / R8 — cycling
// ===========================================================================

describe('R3 — the ticker advances', () => {
  it('moves to the next message after five seconds', () => {
    render(<Broadcast messages={SCRIPT} />);
    expect(screen.getByText(/screening two pitches/)).toBeInTheDocument();
    tick(5000);
    expect(screen.getByText(/shipped it/)).toBeInTheDocument();
  });

  it('wraps at the end of the script', () => {
    render(<Broadcast messages={SCRIPT} />);
    tick(5000);
    tick(5000);
    expect(screen.getByText(/screening two pitches/)).toBeInTheDocument();
  });

  it('clears its interval on unmount rather than firing into a dead tree', () => {
    const { unmount } = render(<Broadcast messages={SCRIPT} />);
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('R8 — one line, once', () => {
  it('shows exactly one message at a time', () => {
    const { container } = render(<Broadcast messages={DEMO_BROADCAST} />);
    expect(container.querySelectorAll('.broadcast-msg')).toHaveLength(1);
  });

  it('renders exactly one strip', () => {
    const { container } = render(<Broadcast messages={SCRIPT} />);
    expect(container.querySelectorAll('.broadcast')).toHaveLength(1);
  });
});

// ===========================================================================
// R4 / R5 — the success phase
// ===========================================================================

describe('R4 — the animation replays per phase', () => {
  it('replaces the message node instead of mutating it in place', () => {
    const { container } = render(<Broadcast messages={SCRIPT} />);
    const first = container.querySelector('.broadcast-msg');
    tick(5000);
    const second = container.querySelector('.broadcast-msg');
    expect(second).not.toBe(first);
  });
});

describe('R5 — a landed build announces itself', () => {
  it('marks the success message', () => {
    const { container } = render(<Broadcast messages={SCRIPT} />);
    expect(container.querySelector('.broadcast-msg')!.className).not.toContain('is-success');
    tick(5000);
    expect(container.querySelector('.broadcast-msg')!.className).toContain('is-success');
  });

  it('tells the shell, so the preview panel can pulse', () => {
    const onSuccessPhase = vi.fn();
    render(<Broadcast messages={SCRIPT} onSuccessPhase={onSuccessPhase} />);
    expect(onSuccessPhase).not.toHaveBeenCalled();
    tick(5000);
    expect(onSuccessPhase).toHaveBeenCalled();
  });
});

// ===========================================================================
// R6 / R9 — the default script
// ===========================================================================

describe('R6 — the strip is never empty', () => {
  it('falls back to the demo script when no messages are passed', () => {
    const { container } = render(<Broadcast />);
    expect(container.querySelector('.broadcast-msg')!.textContent!.length).toBeGreaterThan(0);
  });
});

describe('R9 — the script narrates the real pipeline', () => {
  it('names every agent in the architecture, in pipeline order', () => {
    const agents = DEMO_BROADCAST.map((m) => m.agent);
    expect(agents).toEqual([
      'Guardagent',
      'Community',
      'PM Agent',
      'Architect Agent',
      'Janitor Agent',
      'Ship Agent',
    ]);
  });

  it('ends on a success phase — the build landing is the payoff', () => {
    expect(DEMO_BROADCAST[DEMO_BROADCAST.length - 1].success).toBe(true);
    expect(DEMO_BROADCAST.filter((m) => m.success)).toHaveLength(1);
  });
});
