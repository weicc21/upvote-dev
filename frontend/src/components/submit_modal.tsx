// frontend/src/components/submit_modal.tsx

import { useState, useEffect, useRef, useCallback } from "react";
import type { ApiResult } from "../api_client";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export type SubmitModalProps = {
  open: boolean;
  onClose: () => void;
  onPitch: (input: {
    title: string;
    description: string;
  }) => Promise<ApiResult<{ feature_id: string; state: "screening" }>>;
  onPitched: (feature_id: string, title: string) => void;
  coinsRemaining: number;
  resetsAt: string | null;
};

// ---------------------------------------------------------------------------
// Constants (mirrored from openapi.yaml — frozen schema)
// ---------------------------------------------------------------------------

const TITLE_MIN = 1;
const TITLE_MAX = 60;
const TITLE_LOW = 10;

const SCOPE_MIN = 30;
const SCOPE_MAX = 300;
const SCOPE_LOW = 30;

// ---------------------------------------------------------------------------
// Countdown helper
// ---------------------------------------------------------------------------

function formatCountdown(resetsAt: string | null): string | null {
  if (!resetsAt) return null;
  const diff = new Date(resetsAt).getTime() - Date.now();
  if (diff <= 0) return null;
  const totalSeconds = Math.ceil(diff / 1000);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// Focus trap (R16, R21 — hand-written, no library)
// ---------------------------------------------------------------------------

function getFocusableElements(container: HTMLElement): HTMLElement[] {
  const selectors =
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
  return Array.from(container.querySelectorAll<HTMLElement>(selectors));
}

function trapFocus(e: KeyboardEvent, container: HTMLElement) {
  if (e.key !== "Tab") return;
  const focusable = getFocusableElements(container);
  if (focusable.length === 0) {
    e.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey) {
    if (document.activeElement === first) {
      e.preventDefault();
      last.focus();
    }
  } else {
    if (document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SubmitModal(props: SubmitModalProps): JSX.Element | null {
  const { open, onClose, onPitch, onPitched, coinsRemaining, resetsAt } = props;

  // Form state
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");

  // Validation errors (shown on submit attempt)
  const [titleError, setTitleError] = useState<string | null>(null);
  const [scopeError, setScopeError] = useState<string | null>(null);

  // Submission state
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // 429 state
  const [rateLimited, setRateLimited] = useState(false);
  const [rateLimitResetsAt, setRateLimitResetsAt] = useState<string | null>(null);
  const [countdown, setCountdown] = useState<string | null>(null);

  // Refs
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleInputRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const liveRegionRef = useRef<HTMLDivElement>(null);

  // ---------------------------------------------------------------------------
  // Countdown tick (R10, R13)
  // ---------------------------------------------------------------------------

  const effectiveResetsAt = rateLimitResetsAt ?? resetsAt;
  const outOfCoins = coinsRemaining <= 0 || rateLimited;

  useEffect(() => {
    if (!open) return;
    if (!outOfCoins || !effectiveResetsAt) {
      setCountdown(null);
      return;
    }

    function tick() {
      const cd = formatCountdown(effectiveResetsAt);
      setCountdown(cd);
      if (!cd) {
        // Timer expired — coins may have refreshed
        setRateLimited(false);
        setRateLimitResetsAt(null);
      }
    }

    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [open, outOfCoins, effectiveResetsAt]);

  // ---------------------------------------------------------------------------
  // Focus management (R16)
  // ---------------------------------------------------------------------------

  useEffect(() => {
    if (open) {
      // Capture the element that opened the dialog
      triggerRef.current = document.activeElement as HTMLElement | null;

      // Move focus to title input on next frame (after render)
      requestAnimationFrame(() => {
        titleInputRef.current?.focus();
      });
    }
  }, [open]);

  // Return focus on close (R16)
  const closeDialog = useCallback(() => {
    onClose();
    // Return focus after the dialog unmounts
    requestAnimationFrame(() => {
      triggerRef.current?.focus();
    });
  }, [onClose]);

  // Focus trap + Escape (R16, R17, R19)
  useEffect(() => {
    if (!open) return;

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        // R19: do not close while submitting
        if (submitting) {
          e.preventDefault();
          return;
        }
        closeDialog();
        return;
      }
      if (dialogRef.current) {
        trapFocus(e, dialogRef.current);
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, submitting, closeDialog]);

  // ---------------------------------------------------------------------------
  // Validation (R1, R3, R4)
  // ---------------------------------------------------------------------------

  function validate(): boolean {
    const trimmedTitle = title.trim();
    const trimmedScope = description.trim();
    let valid = true;

    if (trimmedTitle.length < TITLE_MIN) {
      setTitleError(`Feature title must be at least ${TITLE_MIN} character.`);
      valid = false;
    } else {
      setTitleError(null);
    }

    if (trimmedScope.length < SCOPE_MIN) {
      setScopeError(
        `Functional scope must be at least ${SCOPE_MIN} characters — describe one precise, implementation-ready behaviour.`,
      );
      valid = false;
    } else {
      setScopeError(null);
    }

    return valid;
  }

  // ---------------------------------------------------------------------------
  // Submit (R5, R6, R7, R8, R9, R10, R11)
  // ---------------------------------------------------------------------------

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();

    // R6: prevent double submit
    if (submitting) return;

    // Clear previous form-level error
    setFormError(null);

    // Client-side validation
    if (!validate()) return;

    // R5: trim surrounding whitespace only
    const trimmedTitle = title.trim();
    const trimmedDescription = description.trim();

    setSubmitting(true);

    try {
      const result = await onPitch({
        title: trimmedTitle,
        description: trimmedDescription,
      });

      if (result.ok) {
        // R7: clear form, call onPitched, close
        const featureId = result.data.feature_id;
        setTitle("");
        setDescription("");
        setTitleError(null);
        setScopeError(null);
        setFormError(null);
        setRateLimited(false);
        setRateLimitResetsAt(null);
        onPitched(featureId, trimmedTitle);
        closeDialog();
      } else {
        // R8: do NOT clear form on failure
        if (result.status === 429) {
          // R10: out-of-coins state
          setRateLimited(true);
          const ra = "resets_at" in result ? result.resets_at ?? null : null;
          setRateLimitResetsAt(ra);
          // Don't show as a red error — it's a scheduled limit
        } else if (result.status === 400) {
          // R9: verbatim backend message
          setFormError(result.message);
          announceError(result.message);
        } else {
          // R11: friendly line, no raw status/payload
          const friendly = "Something went wrong — please try again.";
          setFormError(friendly);
          announceError(friendly);
        }
      }
    } catch {
      // R11: never expose raw errors
      const friendly = "Something went wrong — please try again.";
      setFormError(friendly);
      announceError(friendly);
    } finally {
      setSubmitting(false);
    }
  }

  // ---------------------------------------------------------------------------
  // Live region announcements (R18)
  // ---------------------------------------------------------------------------

  function announceError(msg: string) {
    if (liveRegionRef.current) {
      // Clear then set to force re-announcement
      liveRegionRef.current.textContent = "";
      requestAnimationFrame(() => {
        if (liveRegionRef.current) {
          liveRegionRef.current.textContent = msg;
        }
      });
    }
  }

  // ---------------------------------------------------------------------------
  // Backdrop click (R17, R19)
  // ---------------------------------------------------------------------------

  function handleBackdropClick(e: React.MouseEvent) {
    // R19: do not close while submitting
    if (submitting) return;
    // Only close if the click target is the backdrop itself, not the dialog
    if (e.target === e.currentTarget) {
      closeDialog();
    }
  }

  // ---------------------------------------------------------------------------
  // Character counts (R2)
  // ---------------------------------------------------------------------------

  const titleRemaining = TITLE_MAX - title.length;
  const scopeRemaining = SCOPE_MAX - description.length;
  const titleCountLow = titleRemaining <= TITLE_LOW;
  const scopeCountLow = scopeRemaining <= SCOPE_LOW;

  // ---------------------------------------------------------------------------
  // Derived state
  // ---------------------------------------------------------------------------

  const submitDisabled = submitting || outOfCoins;

  const headingId = "submit-modal-heading";
  const titleErrorId = "title-error";
  const scopeErrorId = "scope-error";
  const formErrorId = "form-error";

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  if (!open) return null;

  return (
    <div
      className="modal-backdrop"
      onClick={handleBackdropClick}
      /* R17: backdrop click closes unless submitting */
    >
      <div
        ref={dialogRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
      >
        {/* Header */}
        <div className="modal-head">
          <h2 id={headingId}>Pitch a feature</h2>
          <button
            className="modal-close"
            onClick={submitting ? undefined : closeDialog}
            disabled={submitting}
            aria-label="Close"
            type="button"
          >
            ✕
          </button>
        </div>

        {/* Hint (R22) */}
        <p className="modal-hint">
          Precise, implementation-first behaviors get built fastest. The PM Agent merges duplicates,
          so pitch even if something similar exists.
        </p>

        <form onSubmit={handleSubmit} noValidate>
          {/* Title field (R1, R2, R3) */}
          <div className="field">
            <label className="field-label" htmlFor="pitch-title">
              Feature title
              <span className={`char-count${titleCountLow ? " is-low" : ""}`}>
                {titleRemaining}
              </span>
            </label>
            <input
              id="pitch-title"
              ref={titleInputRef}
              type="text"
              maxLength={TITLE_MAX}
              value={title}
              onChange={(e) => {
                setTitle(e.target.value);
                if (titleError) setTitleError(null);
              }}
              placeholder="e.g. Emoji reactions on posts"
              aria-invalid={titleError ? "true" : undefined}
              aria-describedby={titleError ? titleErrorId : undefined}
              disabled={submitting}
              autoComplete="off"
            />
            {titleError && (
              <p id={titleErrorId} className="form-error" role="alert">
                {titleError}
              </p>
            )}
          </div>

          {/* Scope field (R1, R2, R3, R4) */}
          <div className="field">
            <label className="field-label" htmlFor="pitch-scope">
              Functional scope
              <span className={`char-count${scopeCountLow ? " is-low" : ""}`}>
                {scopeRemaining}
              </span>
            </label>
            <textarea
              id="pitch-scope"
              rows={4}
              maxLength={SCOPE_MAX}
              value={description}
              onChange={(e) => {
                setDescription(e.target.value);
                if (scopeError) setScopeError(null);
              }}
              placeholder="Describe the exact behavior: what the user sees, clicks, and gets. Minimum 30 characters."
              aria-invalid={scopeError ? "true" : undefined}
              aria-describedby={scopeError ? scopeErrorId : undefined}
              disabled={submitting}
            />
            {scopeError && (
              <p id={scopeErrorId} className="form-error" role="alert">
                {scopeError}
              </p>
            )}
          </div>

          {/* Form-level error (R9, R11) */}
          {formError && (
            <p id={formErrorId} className="form-error" role="alert">
              {formError}
            </p>
          )}

          {/* 429 / out-of-coins state (R10, R13) */}
          {outOfCoins && (
            <p className="form-error" role="status">
              {countdown
                ? `🪙 Coins refresh in ${countdown}`
                : "🪙 Coins refresh soon — check back in a moment."}
            </p>
          )}

          {/* Actions (R12, R22) */}
          <div className="modal-actions">
            <span className="coin-cost">
              {outOfCoins
                ? countdown
                  ? `🪙 Coins refresh in ${countdown}`
                  : "🪙 Coins refresh soon"
                : `Costs 1 🪙 · ${coinsRemaining} left today`}
            </span>
            <button
              type="button"
              className="btn-ghost"
              onClick={submitting ? undefined : closeDialog}
              disabled={submitting}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn-primary"
              disabled={submitDisabled}
            >
              {submitting ? "Pitching…" : "🔥 Pitch it (1 🪙)"}
            </button>
          </div>
        </form>

        {/* Live region for screen reader announcements (R18) */}
        <div
          ref={liveRegionRef}
          className="visually-hidden"
          aria-live="assertive"
          aria-atomic="true"
        />
      </div>
    </div>
  );
}