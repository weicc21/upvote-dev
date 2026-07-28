/**
 * Contract tests for SandboxPanel — the embedded preview (US-10).
 *
 * The URL arrives already validated from api_client; a `null` here means the
 * allowlist already said no, so these tests never exercise validation.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, fireEvent } from '@testing-library/react';

import { SandboxPanel } from './sandbox_panel';

const URL_OK = 'https://streaks-demo.onrender.com';

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

const tick = (ms: number) => act(() => { vi.advanceTimersByTime(ms); });

// ===========================================================================
// R1 / R2 — the panel
// ===========================================================================

describe('R1 — the frozen aside', () => {
  it('renders the heading and its sub-line', () => {
    render(<SandboxPanel url={URL_OK} />);
    expect(screen.getByRole('heading', { name: /sandbox/i })).toBeInTheDocument();
    expect(screen.getByText(/the app you.re all building/i)).toBeInTheDocument();
  });
});

describe('R2 — the embed', () => {
  it('frames an iframe pointed at the supplied URL', () => {
    const { container } = render(<SandboxPanel url={URL_OK} />);
    const frame = container.querySelector('.sandbox-frame-wrap iframe') as HTMLIFrameElement;
    expect(frame).not.toBeNull();
    expect(frame.getAttribute('src')).toContain(URL_OK);
  });

  it('names the frame for assistive technology', () => {
    render(<SandboxPanel url={URL_OK} />);
    expect(screen.getByTitle(/preview/i)).toBeInTheDocument();
  });
});

// ===========================================================================
// R3 — the embed is not trusted
// ===========================================================================

describe('R3 — a restrictive sandbox attribute', () => {
  it('permits scripts and same-origin only', () => {
    const { container } = render(<SandboxPanel url={URL_OK} />);
    const attr = container.querySelector('iframe')!.getAttribute('sandbox') ?? '';
    expect(attr).toContain('allow-scripts');
    expect(attr).toContain('allow-same-origin');
  });

  it('never grants navigation, popups, or modals to AI-generated code', () => {
    const { container } = render(<SandboxPanel url={URL_OK} />);
    const attr = container.querySelector('iframe')!.getAttribute('sandbox') ?? '';
    for (const forbidden of ['allow-top-navigation', 'allow-popups', 'allow-modals']) {
      expect(attr).not.toContain(forbidden);
    }
  });
});

// ===========================================================================
// R4 / R6 — the cold-start skeleton
// ===========================================================================

describe('R4 — a sleeping host degrades legibly', () => {
  it('shows the warming-up skeleton before the frame loads', () => {
    render(<SandboxPanel url={URL_OK} />);
    expect(screen.getByText(/warming up the sandbox/i)).toBeInTheDocument();
  });

  it('clears the skeleton when the frame reports load', () => {
    const { container } = render(<SandboxPanel url={URL_OK} />);
    fireEvent.load(container.querySelector('iframe')!);
    expect(container.querySelector('.sandbox-skeleton')).toBeNull();
  });

  it('gives up waiting after the timeout rather than spinning forever', () => {
    const { container } = render(<SandboxPanel url={URL_OK} />);
    expect(container.querySelector('.sandbox-skeleton')).not.toBeNull();
    tick(8000);
    expect(container.querySelector('.sandbox-skeleton')).toBeNull();
  });
});

// ===========================================================================
// R5 / R6 / R7 / R8 — refreshing
// ===========================================================================

describe('R5 — a refresh really reloads', () => {
  it('cache-busts the src so a stale build is never shown', () => {
    const { container } = render(<SandboxPanel url={URL_OK} />);
    const before = container.querySelector('iframe')!.getAttribute('src');
    fireEvent.click(screen.getByRole('button', { name: /refresh preview/i }));
    const after = container.querySelector('iframe')!.getAttribute('src');
    expect(after).not.toBe(before);
    expect(after).toMatch(/[?&]v=/);
  });
});

describe('R6 — the second load is as legible as the first', () => {
  it('restores the skeleton on refresh', () => {
    const { container } = render(<SandboxPanel url={URL_OK} />);
    fireEvent.load(container.querySelector('iframe')!);
    expect(container.querySelector('.sandbox-skeleton')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /refresh preview/i }));
    expect(container.querySelector('.sandbox-skeleton')).not.toBeNull();
  });
});

describe('R7 — a landed build advertises itself', () => {
  it('reads as a plain refresh normally', () => {
    render(<SandboxPanel url={URL_OK} />);
    const btn = screen.getByRole('button', { name: /refresh preview/i });
    expect(btn.textContent).toContain('↻');
    expect(btn.className).not.toContain('is-pulsing');
  });

  it('switches copy and pulses when a build is ready', () => {
    render(<SandboxPanel url={URL_OK} pulse />);
    const btn = screen.getByRole('button', { name: /new build ready/i });
    expect(btn.className).toContain('is-pulsing');
  });
});

describe('R8 — the shell hears about the refresh', () => {
  it('calls onRefreshed so the pulse can be cleared', () => {
    const onRefreshed = vi.fn();
    render(<SandboxPanel url={URL_OK} pulse onRefreshed={onRefreshed} />);
    fireEvent.click(screen.getByRole('button', { name: /refresh preview/i }));
    expect(onRefreshed).toHaveBeenCalled();
  });
});

// ===========================================================================
// R9 / R10 — no URL, and the escape hatch
// ===========================================================================

describe('R9 — a null URL is explained, not blank', () => {
  it('renders an explanation instead of an empty frame', () => {
    const { container } = render(<SandboxPanel url={null} />);
    expect(container.querySelector('iframe')).toBeNull();
    const panel = container.querySelector('.sandbox-unavailable');
    expect(panel).not.toBeNull();
    expect(panel!.textContent!.trim().length).toBeGreaterThan(20);
  });
});

describe('R10 — the full-size escape hatch', () => {
  it('links out to the unmodified URL, safely', () => {
    render(<SandboxPanel url={URL_OK} />);
    const link = screen.getByRole('link', { name: /new tab/i });
    expect(link).toHaveAttribute('href', URL_OK);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link.getAttribute('rel')).toContain('noopener');
  });

  it('offers no link when there is nothing to open', () => {
    render(<SandboxPanel url={null} />);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});

// ===========================================================================
// Sizing — regression guard (design guide 4c)
// ===========================================================================

describe('the stylesheet owns the frame height', () => {
  it('sets no inline height or width on the iframe', () => {
    const { container } = render(<SandboxPanel url={URL_OK} />);
    const style = container.querySelector('iframe')!.getAttribute('style') ?? '';
    // An inline height:100% resolves against an auto-height parent and collapses
    // the embed, silently overriding `.sandbox-frame-wrap iframe`.
    expect(style).not.toMatch(/height/i);
    expect(style).not.toMatch(/width/i);
  });

  it('leaves the frame in the wrapper the stylesheet targets', () => {
    const { container } = render(<SandboxPanel url={URL_OK} />);
    expect(container.querySelector('.sandbox-frame-wrap > iframe')).not.toBeNull();
  });
});
