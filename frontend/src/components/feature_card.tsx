// frontend/src/components/feature_card.tsx

import { useState } from "react";
import type { Feature, Status } from "../api_client";
import { UpvoteButton } from "./upvote_button";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

type FeatureCardProps = {
  feature: Feature;
  votedIds: ReadonlySet<string>;
  onUpvote: (id: string) => void;
  pendingVoteId?: string | null;
};

// ---------------------------------------------------------------------------
// Relative-time helper (R3)
// ---------------------------------------------------------------------------

function relativeTime(iso: string): string {
  const now = Date.now();
  const then = new Date(iso).getTime();
  const diffMs = now - then;
  if (diffMs < 0) return "just now";

  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(diffMs / 3_600_000);
  if (hours < 24) return `${hours}h ago`;

  const days = Math.floor(diffMs / 86_400_000);
  return `${days}d ago`;
}

// ---------------------------------------------------------------------------
// Stage pill (R4, R5, R6)
// ---------------------------------------------------------------------------

function StagePill({ status }: { status: Status }): JSX.Element | null {
  switch (status) {
    case "VOTING":
      // R5: no pill for VOTING
      return null;
    case "CONSOLIDATING":
      return (
        <span className="pill pill-consolidating">
          <span className="wand" aria-hidden="true" />
          🪄 AI Merging Duplicates
        </span>
      );
    case "IN_SPRINT":
      return (
        <span className="pill pill-building">
          <span className="loading-ring" aria-hidden="true" />
          🛠️ AI Building
        </span>
      );
    case "COMPILED":
      return <span className="pill pill-live">🎉 Live in Sandbox</span>;
    case "SPLIT":
      return <span className="pill pill-evolving">🚀 AI Evolving</span>;
    default:
      // R6: unrecognised status → no pill, no raw enum
      return null;
  }
}

// ---------------------------------------------------------------------------
// Byline (R3, R21)
// ---------------------------------------------------------------------------

function Byline({
  authorHandle,
  createdAt,
}: {
  authorHandle: string | null;
  createdAt: string;
}): JSX.Element | null {
  // R21: omit byline entirely when handle is null
  if (!authorHandle) return null;
  return (
    <span className="card-byline">
      {authorHandle} · {relativeTime(createdAt)}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Frozen vote display (R7)
// ---------------------------------------------------------------------------

function VoteFrozen({ count }: { count: number }): JSX.Element {
  return (
    <div className="vote-frozen" aria-label={`${count} upvotes`}>
      <span>▲</span>
      <span>{count}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Unlock tree (R11–R16)
// ---------------------------------------------------------------------------

function UnlockTree({
  feature,
  votedIds,
  onUpvote,
  pendingVoteId,
}: {
  feature: Feature;
  votedIds: ReadonlySet<string>;
  onUpvote: (id: string) => void;
  pendingVoteId?: string | null;
}): JSX.Element | null {
  const [expanded, setExpanded] = useState(false);
  const children = feature.children;

  // R16: only render when children is non-empty
  if (children.length === 0) return null;

  const unlockedCount = children.filter((c) => {
    if (c.unlock_threshold === null) return false;
    return c.upvotes >= c.unlock_threshold;
  }).length;

  const total = children.length;

  return (
    <div className="unlock-tree">
      {/* R11: expand/collapse toggle */}
      <button
        className="expand-btn"
        aria-expanded={expanded}
        onClick={() => setExpanded((prev) => !prev)}
      >
        {expanded ? "▾ Hide unlock tree" : "▸ Show unlock tree"}
      </button>

      {expanded && (
        <>
          <div className="unlock-head">
            <span className="unlock-parent">
              Evolved from &ldquo;{feature.title}&rdquo;
            </span>
            <span className="unlock-progress">
              {unlockedCount}/{total} unlocked
            </span>
          </div>
          <ul>
            {children.map((child) => {
              const hasThreshold = child.unlock_threshold !== null;
              const isUnlocked =
                hasThreshold && child.upvotes >= child.unlock_threshold!;
              const childVoted =
                votedIds.has(child.id) || child.viewer_has_voted;
              const pct =
                hasThreshold && child.unlock_threshold! > 0
                  ? Math.min(
                      100,
                      Math.round(
                        (child.upvotes / child.unlock_threshold!) * 100,
                      ),
                    )
                  : 0;

              return (
                <li
                  key={child.id}
                  className={`unlock-node${isUnlocked ? " is-unlocked" : ""}`}
                >
                  {/* R12: lock/unlock icon */}
                  <span className="unlock-icon" aria-hidden="true">
                    {isUnlocked ? "🔓" : "🔒"}
                  </span>
                  <div className="unlock-body">
                    <span className="unlock-title">{child.title}</span>

                    {/* R14, R15: progress bar only when threshold is non-null */}
                    {hasThreshold && (
                      <div
                        className="unlock-bar"
                        role="progressbar"
                        aria-valuenow={child.upvotes}
                        aria-valuemax={child.unlock_threshold!}
                        aria-label={`${child.title}: ${child.upvotes} of ${child.unlock_threshold!} votes`}
                      >
                        <span style={{ width: `${pct}%` }} />
                      </div>
                    )}

                    {/* R13: count and status */}
                    <span className="unlock-count">
                      {isUnlocked
                        ? "Unlocked!"
                        : hasThreshold
                          ? `${child.upvotes} / ${child.unlock_threshold!} votes to unlock`
                          : `${child.upvotes} votes`}
                    </span>

                    {/* R13, R14: vote button unless unlocked */}
                    {isUnlocked ? null : (
                      <UpvoteButton
                        count={child.upvotes}
                        voted={childVoted}
                        onVote={() => onUpvote(child.id)}
                        small
                        disabled={pendingVoteId === child.id}
                        label={child.title}
                      />
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// FeatureCard (R1–R12, R22, R23)
// ---------------------------------------------------------------------------

function FeatureCard(props: FeatureCardProps): JSX.Element {
  const { feature, votedIds, onUpvote, pendingVoteId } = props;
  const isVoting = feature.status === "VOTING";
  const isSplit = feature.status === "SPLIT";
  const voted = votedIds.has(feature.id) || feature.viewer_has_voted;

  return (
    <article className={`card status-${feature.status}`}>
      <div className="card-main">
        <div className="card-copy">
          {/* R3: card-meta → pill, dedup note, byline */}
          <div className="card-meta">
            <StagePill status={feature.status} />

            {/* R10: merge note for CONSOLIDATING */}
            {feature.status === "CONSOLIDATING" &&
              feature.merge_count != null &&
              feature.merge_count > 0 && (
                <span className="merge-note">
                  {feature.merge_count} matching requests found
                </span>
              )}

            {/* R10: builds-on chip */}
            {feature.extends_title && (
              <span className="merge-note">
                Builds on: {feature.extends_title}
              </span>
            )}

            <Byline
              authorHandle={feature.author_handle}
              createdAt={feature.created_at}
            />
          </div>

          <h3 className="card-title">{feature.title}</h3>
          {/* R2: description → card-scope */}
          <p className="card-scope">{feature.description}</p>
        </div>

        {/* R7: interactive button only on VOTING; frozen count otherwise */}
        {isVoting ? (
          <UpvoteButton
            count={feature.upvotes}
            voted={voted}
            onVote={() => onUpvote(feature.id)}
            disabled={pendingVoteId === feature.id}
            label={feature.title}
          />
        ) : (
          <VoteFrozen count={feature.upvotes} />
        )}
      </div>

      {/* R11: unlock tree for SPLIT parents */}
      {isSplit && (
        <UnlockTree
          feature={feature}
          votedIds={votedIds}
          onUpvote={onUpvote}
          pendingVoteId={pendingVoteId}
        />
      )}
    </article>
  );
}

// ---------------------------------------------------------------------------
// HoldingCard (R17, R18)
// ---------------------------------------------------------------------------

function HoldingCard({ feature }: { feature: Feature }): JSX.Element {
  return (
    <article className="card card-holding">
      <div className="holding-header">
        <span className="holding-badge" aria-hidden="true">
          ⏸
        </span>
        <h3 className="card-title">{feature.title}</h3>
        <span className="holding-sub">
          Structural Paradox Detected · holding cycle {feature.postpone_count}{" "}
          of 2
        </span>
        <VoteFrozen count={feature.upvotes} />
      </div>
      <div className="ai-explains">
        <span className="ai-explains-label">🤖 Why the pause</span>
        {/* R18: explanation is the only thing the community sees */}
        <p>{feature.ai_explanation}</p>
      </div>
    </article>
  );
}

// ---------------------------------------------------------------------------
// ShippedCard (R19)
// ---------------------------------------------------------------------------

function ShippedCard({ feature }: { feature: Feature }): JSX.Element {
  const shippedTime = feature.shipped_at
    ? relativeTime(feature.shipped_at)
    : "";

  return (
    <article className="card card-shipped">
      <div className="card-main">
        <div className="card-copy">
          <div className="card-meta">
            <span className="pill pill-shipped">
              🏆 Shipped in {feature.shipped_version}
            </span>
            {/* R19: byline uses shipped_at relative time */}
            {feature.author_handle && (
              <span className="card-byline">
                {feature.author_handle} · {shippedTime}
              </span>
            )}
          </div>
          <h3 className="card-title">{feature.title}</h3>
          <p className="card-scope">{feature.description}</p>
        </div>
        <VoteFrozen count={feature.upvotes} />
      </div>
      <p className="shipped-credit">
        Pitched by{" "}
        <strong>{feature.author_handle ?? "the community"}</strong> · live in
        the sandbox
      </p>
    </article>
  );
}

// ---------------------------------------------------------------------------
// VaultCard (R20)
// ---------------------------------------------------------------------------

function VaultCard({
  feature,
  onReboot,
}: {
  feature: Feature;
  onReboot: (id: string) => void;
}): JSX.Element {
  return (
    <article className="card card-vault">
      <div className="card-main">
        <div className="card-copy">
          <div className="card-meta">
            <span className="pill pill-vault">🗄️ In the Vault</span>
            <Byline
              authorHandle={feature.author_handle}
              createdAt={feature.created_at}
            />
          </div>
          <h3 className="card-title">{feature.title}</h3>
          {/* R20: standard archived explanation replaces scope */}
          <p className="card-scope">
            Archived due to low community velocity. If you still want this
            built, you can reboot the request to restart its 30-day VOTING
            window.
          </p>
        </div>
        <VoteFrozen count={feature.upvotes} />
      </div>
      <button className="reboot-btn" onClick={() => onReboot(feature.id)}>
        ⚡ Reboot request
      </button>
    </article>
  );
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

export { FeatureCard, HoldingCard, ShippedCard, VaultCard };
export type { FeatureCardProps };