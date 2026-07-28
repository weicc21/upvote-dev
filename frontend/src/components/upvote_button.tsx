import { useState, useCallback } from "react";

type UpvoteButtonProps = {
  count: number;
  voted: boolean;
  onVote: () => void;
  small?: boolean;
  disabled?: boolean;
  label?: string;
};

type Burst = {
  id: number;
  particles: Array<{ angle: string; distance: string }>;
};

let burstId = 0;

/**
 * UpvoteButton component with animated particle burst effects.
 * 
 * @param props - Component props
 * @param props.count - Current upvote count
 * @param props.voted - Whether the user has already voted
 * @param props.onVote - Callback function triggered when vote is cast
 * @param props.small - Optional flag for smaller button variant
 * @param props.disabled - Optional flag to disable the button
 * @param props.label - Optional label for enhanced accessibility
 * @returns JSX.Element - Rendered upvote button
 */
function UpvoteButton(props: UpvoteButtonProps): JSX.Element {
  const { count, voted, onVote, small, disabled, label } = props;
  const [bursts, setBursts] = useState<Burst[]>([]);

  const handleClick = useCallback(() => {
    // Prevent voting if already voted or button is disabled
    if (voted || disabled) return;

    // Generate unique burst ID and create particle configuration
    const id = ++burstId;
    const particles = Array.from({ length: 8 }, (_, i) => ({
      angle: `${i * 45}deg`,
      distance: `${26 + (i % 3) * 10}px`,
    }));

    // Add new burst to state for animation
    setBursts((prev) => [...prev, { id, particles }]);

    // Remove burst after animation completes (600ms matches stylesheet)
    setTimeout(() => {
      setBursts((prev) => prev.filter((b) => b.id !== id));
    }, 600);

    onVote();
  }, [voted, disabled, onVote]);

  // Build dynamic class names based on component state
  const classNames = [
    "upvote",
    voted ? "is-voted" : "",
    small ? "is-small" : "",
  ]
    .filter(Boolean)
    .join(" ");

  // Create accessible name for screen readers
  const accessibleName = label
    ? voted
      ? `Hyped — ${count} upvotes — ${label}`
      : `Upvote — ${count} upvotes — ${label}`
    : voted
      ? `Hyped — ${count} upvotes`
      : `Upvote — ${count} upvotes`;

  return (
    <button
      className={classNames}
      onClick={handleClick}
      aria-pressed={voted}
      aria-label={accessibleName}
      disabled={disabled}
    >
      <span className="upvote-arrow">▲</span>
      <span className="upvote-count">{count}</span>
      <span className="upvote-word">{voted ? "Hyped!" : "Hype it"}</span>

      {bursts.map((burst) => (
        <span key={burst.id} className="burst" aria-hidden="true">
          {burst.particles.map((p, i) => (
            <span
              key={i}
              style={
                {
                  "--a": p.angle,
                  "--d": p.distance,
                } as React.CSSProperties
              }
            />
          ))}
        </span>
      ))}
    </button>
  );
}

export { UpvoteButton };
export type { UpvoteButtonProps };