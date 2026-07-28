// frontend/src/components/sandbox_panel.tsx

import { useState, useCallback, useEffect, useRef } from "react";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export type SandboxPanelProps = {
  url: string | null;
  pulse?: boolean;
  onRefreshed?: () => void;
};

// ---------------------------------------------------------------------------
// SandboxPanel
// ---------------------------------------------------------------------------

export function SandboxPanel({ url, pulse, onRefreshed }: SandboxPanelProps): JSX.Element {
  // R5: cache-busting nonce — bumped on every refresh
  const [nonce, setNonce] = useState<number>(0);

  // R4: skeleton visibility — shown until iframe loads or 8 s timeout
  const [loading, setLoading] = useState<boolean>(!!url);

  // Ref to track the timeout so we can clear it on unmount / refresh
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Clear any pending timeout on unmount
  useEffect(() => {
    return () => {
      if (timeoutRef.current !== null) {
        clearTimeout(timeoutRef.current);
      }
    };
  }, []);

  // R4: start the 8-second fallback timer whenever nonce or url changes
  useEffect(() => {
    if (!url) return;

    // Reset loading state for this cycle
    setLoading(true);

    // Clear any previous timeout
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current);
    }

    // R4: clear skeleton after 8 s regardless
    timeoutRef.current = setTimeout(() => {
      setLoading(false);
    }, 8000);

    return () => {
      if (timeoutRef.current !== null) {
        clearTimeout(timeoutRef.current);
      }
    };
  }, [nonce, url]);

  // R4: iframe load handler — clear skeleton immediately
  const handleLoad = useCallback(() => {
    setLoading(false);
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  }, []);

  // R5, R6, R7, R8: refresh handler
  const handleRefresh = useCallback(() => {
    setNonce((n) => n + 1);
    // R6: skeleton is reset via the useEffect above reacting to nonce change
    // R8: notify shell so it can clear the pulse
    onRefreshed?.();
  }, [onRefreshed]);

  // R5: build the framed URL with cache-busting query param
  const framedUrl = url ? `${url}${url.includes("?") ? "&" : "?"}v=${nonce}` : null;

  // R7: refresh button label and class
  const refreshLabel = pulse
    ? "✨ Refresh Preview — new build ready!"
    : "↻ Refresh Preview";
  const refreshClass = pulse ? "refresh-btn is-pulsing" : "refresh-btn";

  return (
    <aside className="sandbox">
      {/* R1: heading */}
      <div className="sandbox-head">
        <h2>Sandbox</h2>
        <span className="sandbox-sub">the app you're all building</span>
      </div>

      {/* R2, R3, R4, R9: frame or unavailable */}
      {url !== null && framedUrl !== null ? (
        <>
          <div className="sandbox-frame-wrap">
            {/* R4: cold-start skeleton */}
            {loading && (
              <div className="sandbox-skeleton">🔥 Warming up the sandbox…</div>
            )}

            {/* R2: iframe with restrictive sandbox; R3: only scripts + same-origin; R5: key forces remount */}
            {/* Sizing is the stylesheet's job (design guide 4c): an inline
                height:100% resolves against an auto-height parent and collapses
                the frame, overriding `.sandbox-frame-wrap iframe`. */}
            <iframe
              key={nonce}
              src={framedUrl}
              title="Live preview of the community sandbox app"
              sandbox="allow-scripts allow-same-origin"
              onLoad={handleLoad}
            />
          </div>

          {/* R7: refresh control */}
          <button
            type="button"
            className={refreshClass}
            onClick={handleRefresh}
          >
            {refreshLabel}
          </button>

          {/* R10: new-tab link */}
          <a
            className="sandbox-newtab"
            href={url}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open sandbox in new tab ↗
          </a>
        </>
      ) : (
        /* R9: unavailable state */
        <div className="sandbox-unavailable">
          The sandbox preview is currently unavailable. The URL was not configured
          or did not pass the host allowlist.
        </div>
      )}
    </aside>
  );
}