import { useState, useEffect, useRef } from "react";

export type BroadcastMessage = {
  icon: string;
  agent: string;
  text: string;
  success?: boolean;
};

export const DEMO_BROADCAST: BroadcastMessage[] = [
  {
    icon: "🛡️",
    agent: "Guardagent",
    text: "Screening incoming pitch for policy compliance…",
  },
  {
    icon: "🗳️",
    agent: "Community",
    text: "Voting is live — the community is ranking features by upvotes.",
  },
  {
    icon: "🪄",
    agent: "PM Agent",
    text: "Synthesising duplicates and merging overlapping requests…",
  },
  {
    icon: "🏗️",
    agent: "Architect Agent",
    text: "Testing structural constraints against the current build…",
  },
  {
    icon: "🧹",
    agent: "Janitor Agent",
    text: "Sweeping stale requests into the Vault…",
  },
  {
    icon: "🚀",
    agent: "Ship Agent",
    text: "Build deployed to the sandbox — ready for preview!",
    success: true,
  },
];

export type BroadcastProps = {
  messages?: BroadcastMessage[];
  onSuccessPhase?: () => void;
};

/**
 * Broadcast component that cycles through AI agent status messages.
 * Automatically rotates messages every 5 seconds and triggers a callback
 * when a success message is displayed.
 *
 * @param messages - Array of broadcast messages to display (defaults to DEMO_BROADCAST)
 * @param onSuccessPhase - Optional callback triggered when a success message is shown
 */
export function Broadcast({
  messages = DEMO_BROADCAST,
  onSuccessPhase,
}: BroadcastProps): JSX.Element {
  const [index, setIndex] = useState(0);
  
  // Store the latest onSuccessPhase callback in a ref to avoid effect dependencies
  const onSuccessRef = useRef(onSuccessPhase);
  
  // Update ref when callback changes
  useEffect(() => {
    onSuccessRef.current = onSuccessPhase;
  }, [onSuccessPhase]);

  // Reset index when messages array changes identity or length
  useEffect(() => {
    setIndex(0);
  }, [messages]);

  // Set up interval to cycle through messages every 5 seconds
  useEffect(() => {
    if (messages.length === 0) return;

    const id = setInterval(() => {
      setIndex((prev) => {
        const next = (prev + 1) % messages.length;
        return next;
      });
    }, 5000);

    return () => clearInterval(id);
  }, [messages]);

  // Get current message to display
  const current = messages.length > 0 ? messages[index % messages.length] : null;

  // Fire onSuccessPhase callback whenever the current message is a success message
  useEffect(() => {
    if (current?.success && onSuccessRef.current) {
      onSuccessRef.current();
    }
  }, [index, current?.success]);

  // Guard against empty messages array
  if (!current) {
    return (
      <div className="broadcast" role="status" aria-live="polite">
        <span className="broadcast-live">
          <span className="live-dot" />
          LIVE
        </span>
        <span className="broadcast-label">AI CREATOR BROADCAST</span>
        <span className="broadcast-msg" />
      </div>
    );
  }

  const isSuccess = !!current.success;

  return (
    <div className="broadcast" role="status" aria-live="polite">
      <span className="broadcast-live">
        <span className="live-dot" />
        LIVE
      </span>
      <span className="broadcast-label">AI CREATOR BROADCAST</span>
      <span
        key={index}
        className={`broadcast-msg${isSuccess ? " is-success" : ""}`}
      >
        {current.icon} <strong>{current.agent}</strong> {current.text}
      </span>
    </div>
  );
}