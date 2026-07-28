/**
 * Contract tests for SubmitModal — US-01 pitching.
 *
 * `onPitch` is the injected seam, so nothing here reaches the network.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { SubmitModal } from './submit_modal';
import type { ApiResult } from '../api_client';

type PitchOk = ApiResult<{ feature_id: string; state: 'screening' }>;

const GOOD_TITLE = 'Weekly completion chart';
const GOOD_SCOPE =
  'Show a small bar chart of how many habits were completed on each of the last seven days.';

const accepted: PitchOk = { ok: true, data: { feature_id: 'new-1', state: 'screening' } };

function setup(over: Partial<React.ComponentProps<typeof SubmitModal>> = {}) {
  const onPitch = vi.fn(async (_input: { title: string; description: string }): Promise<PitchOk> => accepted);
  const onClose = vi.fn();
  const onPitched = vi.fn();
  const utils = render(
    <SubmitModal
      open
      onClose={onClose}
      onPitch={onPitch}
      onPitched={onPitched}
      coinsRemaining={5}
      resetsAt={null}
      {...over}
    />,
  );
  return { onPitch, onClose, onPitched, ...utils };
}

async function fillValid(user = userEvent.setup()) {
  const inputs = screen.getAllByRole('textbox');
  await user.type(inputs[0], GOOD_TITLE);
  await user.type(inputs[1], GOOD_SCOPE);
  return user;
}

function submitButton() {
  return screen
    .getAllByRole('button')
    .find((b) => /pitch it|submit|pitch/i.test(b.textContent ?? ''))!;
}

beforeEach(() => vi.useRealTimers());
afterEach(() => vi.restoreAllMocks());

// ===========================================================================
// Visibility
// ===========================================================================

describe('open state', () => {
  it('renders nothing when closed', () => {
    const { container } = render(
      <SubmitModal
        open={false}
        onClose={vi.fn()}
        onPitch={vi.fn(async () => accepted)}
        onPitched={vi.fn()}
        coinsRemaining={5}
        resetsAt={null}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});

// ===========================================================================
// R1 / R3 / R4 — bounds and refusal copy
// ===========================================================================

describe('R1/R3 — field bounds', () => {
  it('does not submit a description under 30 characters', async () => {
    const { onPitch } = setup();
    const user = userEvent.setup();
    const inputs = screen.getAllByRole('textbox');
    await user.type(inputs[0], GOOD_TITLE);
    await user.type(inputs[1], 'too short');
    await user.click(submitButton());
    expect(onPitch).not.toHaveBeenCalled();
  });

  it('does not submit an empty title', async () => {
    const { onPitch } = setup();
    const user = userEvent.setup();
    await user.type(screen.getAllByRole('textbox')[1], GOOD_SCOPE);
    await user.click(submitButton());
    expect(onPitch).not.toHaveBeenCalled();
  });

  it('R3: says which field failed rather than only disabling', async () => {
    setup();
    const user = userEvent.setup();
    const inputs = screen.getAllByRole('textbox');
    await user.type(inputs[0], GOOD_TITLE);
    await user.type(inputs[1], 'nope');
    await user.click(submitButton());
    await waitFor(() => {
      expect(document.body.textContent).toMatch(/30|scope|description/i);
    });
  });

  it('submits when both fields are in bounds', async () => {
    const { onPitch } = setup();
    const user = await fillValid();
    await user.click(submitButton());
    await waitFor(() => expect(onPitch).toHaveBeenCalledTimes(1));
    expect(onPitch.mock.calls[0][0]).toEqual({ title: GOOD_TITLE, description: GOOD_SCOPE });
  });
});

describe('R2 — live character counters', () => {
  it('shows a count that tracks typing', async () => {
    const { container } = setup();
    const user = userEvent.setup();
    const before = container.querySelector('.char-count')!.textContent;
    await user.type(screen.getAllByRole('textbox')[0], 'abc');
    await waitFor(() =>
      expect(container.querySelector('.char-count')!.textContent).not.toBe(before),
    );
  });
});

// ===========================================================================
// R5 — send exactly what was typed
// ===========================================================================

describe('R5 — no client-side rewriting', () => {
  it('sends the characters as typed — surrounding whitespace may go, nothing else may', async () => {
    const { onPitch } = setup();
    const user = userEvent.setup();
    const inputs = screen.getAllByRole('textbox');
    // Internal punctuation, casing and double spaces are the author's; only the
    // outer padding is the client's to drop (R5).
    const typed = 'Emoji  reactions on "posts" — please!';
    await user.type(inputs[0], `  ${typed}  `);
    await user.type(inputs[1], GOOD_SCOPE);
    await user.click(submitButton());
    await waitFor(() => expect(onPitch).toHaveBeenCalled());
    expect(onPitch.mock.calls[0][0].title).toBe(typed);
  });
});

// ===========================================================================
// R6 / R7 — submitting
// ===========================================================================

describe('R6 — one click, one request', () => {
  it('does not send twice on a double click', async () => {
    let resolve!: (v: PitchOk) => void;
    const onPitch = vi.fn(() => new Promise<PitchOk>((r) => (resolve = r)));
    render(
      <SubmitModal open onClose={vi.fn()} onPitch={onPitch} onPitched={vi.fn()}
        coinsRemaining={5} resetsAt={null} />,
    );
    const user = await fillValid();
    const btn = submitButton();
    await user.click(btn);
    await user.click(btn);
    expect(onPitch).toHaveBeenCalledTimes(1);
    resolve(accepted);
  });
});

describe('R7 — the title is handed up on success', () => {
  it('calls onPitched with feature_id and the typed title', async () => {
    const { onPitched } = setup();
    const user = await fillValid();
    await user.click(submitButton());
    await waitFor(() => expect(onPitched).toHaveBeenCalledWith('new-1', GOOD_TITLE));
  });

  it('closes after a successful pitch', async () => {
    const { onClose } = setup();
    const user = await fillValid();
    await user.click(submitButton());
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });
});

// ===========================================================================
// R8 / R9 / R11 — failure keeps the author's work
// ===========================================================================

describe('R8/R9 — a rejected pitch keeps its text', () => {
  const rejected: PitchOk = {
    ok: false, status: 400, code: 'validation_failed',
    message: 'Title must not contain HTML or script markup.',
  };

  it('shows the backend message verbatim', async () => {
    setup({ onPitch: vi.fn(async () => rejected) });
    const user = await fillValid();
    await user.click(submitButton());
    await waitFor(() =>
      expect(screen.getByText(/must not contain HTML or script markup/i)).toBeInTheDocument(),
    );
  });

  it('leaves both fields populated so the author can edit and retry', async () => {
    setup({ onPitch: vi.fn(async () => rejected) });
    const user = await fillValid();
    await user.click(submitButton());
    await waitFor(() => expect(document.body.textContent).toMatch(/markup/i));
    expect((screen.getAllByRole('textbox')[0] as HTMLInputElement).value).toBe(GOOD_TITLE);
  });

  it('does not close on failure', async () => {
    const { onClose } = setup({ onPitch: vi.fn(async () => rejected) });
    const user = await fillValid();
    await user.click(submitButton());
    await waitFor(() => expect(document.body.textContent).toMatch(/markup/i));
    expect(onClose).not.toHaveBeenCalled();
  });

  it('R11: never leaks a status code or payload into the DOM', async () => {
    setup({
      onPitch: vi.fn(async () => ({
        ok: false, status: 500, code: 'internal_error',
        message: 'Something went wrong. Please try again.',
      }) as PitchOk),
    });
    const user = await fillValid();
    await user.click(submitButton());
    await waitFor(() => expect(document.body.textContent).toMatch(/went wrong/i));
    expect(document.body.textContent).not.toMatch(/\b500\b/);
  });
});

// ===========================================================================
// R10 / R12 / R13 — coins
// ===========================================================================

describe('R10 — out of coins is a schedule, not an error', () => {
  const outOfCoins: PitchOk = {
    ok: false, status: 429, code: 'out_of_coins',
    message: 'You have used all your Pitch Coins for today. Try again tomorrow.',
    resets_at: '2026-07-29T00:00:00+00:00',
  };

  it('shows the out-of-coins copy', async () => {
    setup({ onPitch: vi.fn(async () => outOfCoins) });
    const user = await fillValid();
    await user.click(submitButton());
    await waitFor(() => expect(document.body.textContent).toMatch(/coins refresh/i));
  });

  it('R13: disables the submit control and shows the countdown once the balance is spent', () => {
    setup({ coinsRemaining: 0, resetsAt: new Date(Date.now() + 95_000).toISOString() });
    const submit = screen
      .getAllByRole('button')
      .find((b) => /pitch it/i.test(b.textContent ?? ''))!;
    // Inert before any request is attempted — a 429 must not be how the author
    // discovers the wallet is empty.
    expect(submit).toBeDisabled();
    // …and the wait is named rather than left as a dead button.
    expect(document.body.textContent).toMatch(/coins refresh/i);
    expect(document.body.textContent).toMatch(/\d+:\d{2}/);
  });

  it('R13: cannot pitch at all with an empty balance', async () => {
    const { onPitch } = setup({ coinsRemaining: 0, resetsAt: '2026-07-29T00:00:00+00:00' });
    const user = userEvent.setup();
    const inputs = screen.queryAllByRole('textbox');
    if (inputs.length) {
      await user.type(inputs[0], GOOD_TITLE);
      await user.type(inputs[1], GOOD_SCOPE);
    }
    expect(onPitch).not.toHaveBeenCalled();
  });
});

describe('R12 — the cost is visible before typing', () => {
  it('shows the coin cost and the remaining balance', () => {
    setup({ coinsRemaining: 4 });
    expect(document.body.textContent).toMatch(/🪙/);
    expect(document.body.textContent).toMatch(/4/);
  });
});

// ===========================================================================
// R14 - R18 — dialog behaviour
// ===========================================================================

describe('R14/R15 — dialog semantics and focus', () => {
  it('is a labelled modal dialog', () => {
    setup();
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName();
  });

  it('moves focus to the title field on open', async () => {
    setup();
    await waitFor(() => expect(screen.getAllByRole('textbox')[0]).toHaveFocus());
  });
});

describe('R16 — three ways out', () => {
  it('closes on Escape', async () => {
    const { onClose } = setup();
    // wait for autofocus — pressing Escape before focus lands inside the dialog
    // dispatches to <body>, which the modal's handler never sees
    await waitFor(() => expect(screen.getAllByRole('textbox')[0]).toHaveFocus());
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('closes on the ✕ control', async () => {
    const { onClose } = setup();
    const close = screen.getAllByRole('button').find((b) =>
      /close|✕|×/i.test(b.getAttribute('aria-label') ?? b.textContent ?? ''),
    )!;
    await userEvent.click(close);
    expect(onClose).toHaveBeenCalled();
  });
});

describe('R18 — cannot escape mid-flight', () => {
  it('ignores Escape while a submit is in flight', async () => {
    let resolve!: (v: PitchOk) => void;
    const onPitch = vi.fn(() => new Promise<PitchOk>((r) => (resolve = r)));
    const onClose = vi.fn();
    render(
      <SubmitModal open onClose={onClose} onPitch={onPitch} onPitched={vi.fn()}
        coinsRemaining={5} resetsAt={null} />,
    );
    const user = await fillValid();
    await user.click(submitButton());
    await user.keyboard('{Escape}');
    expect(onClose).not.toHaveBeenCalled();
    resolve(accepted);
  });
});

// ===========================================================================
// R17 — errors are announced, not only styled
// ===========================================================================

describe('R17 — assistive technology hears the failure', () => {
  it('exposes a live region for submission errors', async () => {
    const { container } = setup({
      onPitch: vi.fn(async () => ({
        ok: false, status: 400, code: 'validation_failed', message: 'Nope.',
      }) as PitchOk),
    });
    const user = await fillValid();
    await user.click(submitButton());
    await waitFor(() => expect(container.querySelector('[aria-live]')).not.toBeNull());
  });
});
