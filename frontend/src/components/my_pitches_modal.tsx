// frontend/src/components/my_pitches_modal.tsx

import { useEffect, useRef, useCallback } from "react";
import type { PendingPitch, Feature } from "../api_client";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export type MyPitchesModalProps = {
  open: boolean;
  onClose: () => void;
  pending: PendingPitch[];
  features: Feature[];
  loading?: boolean;
  error?: string | null;
  onDismiss: (feature_id: string) => void;
  onRetry?: () => void;
};

// ---------------------------------------------------------------------------
// Rejection reason → actionable copy (R4)
// ---------------------------------------------------------------------------

function rejectionCopy(
  reason: PendingPitch["reason"],
  shippedVersion: string | null,
): string {
  switch (reason) {
    case "security":
      return "Your pitch didn\u2019t clear screening \u2014 rephrase and try again.";
    case "off_topic":
      return "Great idea, but it\u2019s outside what this app is about \u2014 check the Sandbox to see what we\u2019re building.";
    case "unclear":
      return "Your title and description seemed to describe different things \u2014 try again with them lined up.";
    case "already_shipped":
      return `Looks like this already shipped in ${shippedVersion ?? "a previous version"} \u2014 check it out in the Sandbox!`;
    default:
      // R4 says never fall back to generic — but we must handle null/unknown
      // defensively. This path should be unreachable for rejected entries.
      return "Your pitch didn\u2019t clear screening \u2014 rephrase and try again.";
  }
}

// ---------------------------------------------------------------------------
// Stage → pill class (R8)
// ---------------------------------------------------------------------------

function pillClass(status: Feature["status"]): string {
  switch (status) {
    case "CONSOLIDATING":
      return "pill pill-consolidating";
    case "IN_SPRINT":
      return "pill pill-building";
    case "COMPILED":
      return "pill pill-live";
    case "SPLIT":
      return "pill pill-evolving";
    case "ARCHIVED":
      return "pill pill-vault";
    default:
      // VOTING has no pill; POSTPONED_CONFLICT doesn't either in this context
      return "pill";
  }
}

function pillLabel(status: Feature["status"]): string {
  switch (status) {
    case "VOTING":
      return "\uD83D\uDDF3\uFE0F Voting";
    case "CONSOLIDATING":
      return "\uD83E\uDE84 AI Merging Duplicates";
    case "IN_SPRINT":
      return "\uD83D\uDEE0\uFE0F AI Building";
    case "COMPILED":
      return "\uD83C\uDF89 Live in Sandbox";
    case "SPLIT":
      return "\uD83D\uDE80 AI Evolving";
    case "POSTPONED_CONFLICT":
      return "\u23F8 Holding";
    case "ARCHIVED":
      return "\uD83D\uDDC4\uFE0F In the Vault";
    default:
      return status;
  }
}

// ---------------------------------------------------------------------------
// Helpers: sort newest-first
// ---------------------------------------------------------------------------

function sortedPending(list: PendingPitch[]): PendingPitch[] {
  return [...list].sort(
    (a, b) => new Date(b.submitted_at).getTime() - new Date(a.submitted_at).getTime(),
  );
}

function sortedFeatures(list: Feature[]): Feature[] {
  return [...list].sort(
    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MyPitchesModal(props: MyPitchesModalProps): JSX.Element | null {
  const { open, onClose, pending, features, loading, error, onDismiss, onRetry } = props;

  const dialogRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  // R12: capture the opener element before the dialog opens
  useEffect(() => {
    if (open) {
      openerRef.current = document.activeElement as HTMLElement | null;
    }
  }, [open]);

  // R12: move focus into the dialog on open
  useEffect(() => {
    if (open && closeButtonRef.current) {
      closeButtonRef.current.focus();
    }
  }, [open]);

  // R12: return focus to opener on close
  const handleClose = useCallback(() => {
    onClose();
    // Defer so the dialog unmounts first
    requestAnimationFrame(() => {
      if (openerRef.current && typeof openerRef.current.focus === "function") {
        openerRef.current.focus();
      }
    });
  }, [onClose]);

  // R11: close on Escape
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        handleClose();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, handleClose]);

  if (!open) return null;

  // R11: close on backdrop click
  const onBackdropClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) {
      handleClose();
    }
  };

  const isEmpty = pending.length === 0 && features.length === 0;
  const headingId = "my-pitches-heading";

  return (
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions
    <div className="modal-backdrop" onClick={onBackdropClick}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        ref={dialogRef}
      >
        {/* Header */}
        <div className="modal-head">
          <h2 id={headingId}>My pitches</h2>
          <button
            className="modal-close"
            onClick={handleClose}
            aria-label="Close"
            ref={closeButtonRef}
          >
            ✕
          </button>
        </div>

        {/* R1: hint */}
        <p className="modal-hint">
          🔒 Only you can see these. A pitch joins the public feed once Guardagent clears it.
        </p>

        {/* R10: loading state */}
        {loading && (
          <div className="pitches-list">
            <div className="pending-card">
              <span className="pending-copy">Loading your pitches…</span>
            </div>
          </div>
        )}

        {/* R10: error state */}
        {!loading && error && (
          <div className="pitches-list">
            <div className="feed-status is-error">
              <span>{error}</span>
              {onRetry && (
                <button className="btn-ghost" onClick={onRetry}>
                  Retry
                </button>
              )}
            </div>
          </div>
        )}

        {/* R9: empty state */}
        {!loading && !error && isEmpty && (
          <div className="pitches-empty">
            Nothing in screening right now. Pitch a feature and track it here while Guardagent
            checks it over.
          </div>
        )}

        {/* Content: pending above features (R2) */}
        {!loading && !error && !isEmpty && (
          <div className="pitches-list">
            {/* --- Pending pitches --- */}
            {sortedPending(pending).map((p) => {
              if (p.state === "screening") {
                // R3: screening entry
                return (
                  <div className="pending-card" key={p.feature_id}>
                    <div className="pending-copy">
                      <strong>{p.title}</strong>
                    </div>
                    <div className="pending-status">
                      <span className="shield" />
                      🛡️ Guardagent is screening your pitch...
                    </div>
                  </div>
                );
              }

              if (p.state === "merged") {
                // R6: merged entry — a win, not a rejection
                return (
                  <div className="pending-card" key={p.feature_id}>
                    <div className="pending-copy">
                      <strong>{p.title}</strong>
                    </div>
                    <div className="pending-status">
                      🔗 Great minds! Your idea joined forces with &ldquo;
                      {p.merged_into_title ?? "an existing feature"}&rdquo;
                    </div>
                    {/* R7: dismiss control */}
                    <button
                      className="btn-dismiss"
                      onClick={() => onDismiss(p.feature_id)}
                    >
                      Dismiss
                    </button>
                  </div>
                );
              }

              if (p.state === "rejected") {
                // R4: rejection with specific copy; R5: no raw verdict
                return (
                  <div className="pending-card is-rejected" key={p.feature_id}>
                    <div className="pending-copy">
                      <strong>{p.title}</strong>
                    </div>
                    <div className="pending-status is-rejected">
                      {rejectionCopy(p.reason, p.shipped_version)}
                    </div>
                    {/* R7: dismiss control */}
                    <button
                      className="btn-dismiss"
                      onClick={() => onDismiss(p.feature_id)}
                    >
                      Dismiss
                    </button>
                  </div>
                );
              }

              // R14: unknown state — treat as dismissed / don't render
              return null;
            })}

            {/* --- Author's live features (R8) --- */}
            {sortedFeatures(features).map((f) => (
              <div className="pending-card" key={f.id}>
                <div className="pending-copy">
                  <strong>{f.title}</strong>
                </div>
                <div className="pending-status">
                  <span className={pillClass(f.status)}>{pillLabel(f.status)}</span>
                  <span style={{ marginLeft: "0.5em" }}>▲ {f.upvotes}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}