// frontend/src/app_shell.tsx

import "./styles.css";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  listFeatures,
  listBroadcastEvents,
  rebootFeature,
  upvote,
  createFeature,
  getMyPitches,
  subscribe,
  sandboxUrl,
  type Feature,
  type Status,
  type View,
  type Sort,
  type ApiResult,
  type PendingPitch,
  type BroadcastEvent,
} from "./api_client";
import { Broadcast, type BroadcastMessage } from "./components/broadcast";
import { FeatureCard, HoldingCard, ShippedCard, VaultCard } from "./components/feature_card";
import { SandboxPanel } from "./components/sandbox_panel";
import { SubmitModal } from "./components/submit_modal";
import { MyPitchesModal } from "./components/my_pitches_modal";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const INITIAL_COINS = 5;
const COIN_REFILL_MS = 2 * 60 * 1000; // 2 minutes
const TOAST_DURATION_MS = 4500;
const DEBOUNCE_MS = 350;
const BROADCAST_CAP = 50; // R35: max retained broadcast messages

// Which statuses belong to which view — used to filter Realtime events (R15)
const VIEW_STATUSES: Record<View, ReadonlySet<Status>> = {
  pipeline: new Set<Status>([
    "VOTING",
    "CONSOLIDATING",
    "IN_SPRINT",
    "SPLIT",
    "COMPILED",
  ]),
  shipped: new Set<Status>(["COMPILED"]), // shipped_version != null distinguishes, but status is COMPILED
  holding: new Set<Status>(["POSTPONED_CONFLICT"]),
  vault: new Set<Status>(["ARCHIVED"]),
};

// Filter chip definitions for the pipeline tab (R5b)
const FILTER_CHIPS: { label: string; status: Status | null }[] = [
  { label: "All", status: null },
  { label: "🗳️ Voting", status: "VOTING" },
  { label: "🪄 AI Merging Duplicates", status: "CONSOLIDATING" },
  { label: "🛠️ AI Building", status: "IN_SPRINT" },
  { label: "🚀 AI Evolving", status: "SPLIT" },
  { label: "🎉 Live in Sandbox", status: "COMPILED" },
];

// Tab definitions (R4)
const TABS: { view: View; label: string }[] = [
  { view: "pipeline", label: "⚡ Up Next Pipeline" },
  { view: "shipped", label: "🏆 Shipped" },
  { view: "holding", label: "⏸ Holding Pattern" },
  { view: "vault", label: "🗄️ The Vault" },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Does a Realtime row belong to the "shipped" view? */
function isShipped(f: Partial<Feature>): boolean {
  return !!f.shipped_version;
}

/** Does a Realtime row belong to the given view? */
function rowBelongsToView(row: Partial<Feature>, view: View): boolean {
  const st = row.status as Status | undefined;
  if (!st) return false;
  if (view === "shipped") return isShipped(row);
  if (view === "pipeline") return VIEW_STATUSES.pipeline.has(st) && !isShipped(row);
  return VIEW_STATUSES[view].has(st);
}

function formatCountdown(ms: number): string {
  if (ms <= 0) return "0:00";
  const totalSec = Math.ceil(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// R33: Map a BroadcastEvent to the ticker's BroadcastMessage shape
// ---------------------------------------------------------------------------

const AGENT_ICONS: Record<string, string> = {
  Guardagent: "🛡️",
  "PM Agent": "🔮",
  "Architect Agent": "📐",
  "Janitor Agent": "🧹",
  "Ship Agent": "🚀",
};

function mapBroadcastEvent(event: BroadcastEvent): BroadcastMessage {
  return {
    icon: AGENT_ICONS[event.agent_name] ?? "🤖",
    agent: event.agent_name,
    text: event.message,
    success: event.phase === "deployed" ? true : undefined,
  };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function AppShell(): JSX.Element {
  // -- Board state --
  const [features, setFeatures] = useState<Feature[]>([]);
  const [loading, setLoading] = useState(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // -- Navigation --
  const [activeView, setActiveView] = useState<View>("pipeline");
  const [sort, setSort] = useState<Sort>("top");
  // R5b: multi-select filter chips — each toggles independently
  const [filterStatus, setFilterStatus] = useState<Set<Status>>(new Set());
  const [vaultQuery, setVaultQuery] = useState("");
  const debouncedQueryRef = useRef("");
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // -- Voting --
  const [votedIds, setVotedIds] = useState<Set<string>>(new Set());
  const [pendingVoteId, setPendingVoteId] = useState<string | null>(null);

  // -- Coins (R19) --
  const [coins, setCoins] = useState(INITIAL_COINS);
  const [coinRefillAt, setCoinRefillAt] = useState<number | null>(null);
  const [coinCountdown, setCoinCountdown] = useState<string | null>(null);
  const coinTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // -- Dialogs --
  const [submitOpen, setSubmitOpen] = useState(false);
  const [myPitchesOpen, setMyPitchesOpen] = useState(false);
  const [myPitchesPending, setMyPitchesPending] = useState<PendingPitch[]>([]);
  const [myPitchesFeatures, setMyPitchesFeatures] = useState<Feature[]>([]);
  const [myPitchesLoading, setMyPitchesLoading] = useState(false);
  const [myPitchesError, setMyPitchesError] = useState<string | null>(null);
  const [resetsAt, setResetsAt] = useState<string | null>(null);

  // -- Pending entries (R23) --
  const [localPending, setLocalPending] = useState<
    { feature_id: string; title: string; submitted_at: string }[]
  >([]);

  // -- Toast (R27) --
  const [toast, setToast] = useState<string | null>(null);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // -- Sandbox pulse (R26) --
  const [sandboxPulse, setSandboxPulse] = useState(false);

  // -- Holding count for tab badge (R4) --
  const [holdingCount, setHoldingCount] = useState(0);

  // -- Broadcast messages (R32–R35) --
  const [broadcastMessages, setBroadcastMessages] = useState<BroadcastMessage[]>([]);

  // =========================================================================
  // Toast helper (R27)
  // =========================================================================

  const showToast = useCallback((msg: string) => {
    if (toastTimerRef.current !== null) {
      clearTimeout(toastTimerRef.current);
      toastTimerRef.current = null;
    }
    setToast(msg);
    toastTimerRef.current = setTimeout(() => {
      setToast(null);
      toastTimerRef.current = null;
    }, TOAST_DURATION_MS);
  }, []);

  // Clean up toast timer on unmount
  useEffect(() => {
    return () => {
      if (toastTimerRef.current !== null) clearTimeout(toastTimerRef.current);
    };
  }, []);

  // =========================================================================
  // Coin refill timer (R19, R20)
  // =========================================================================

  useEffect(() => {
    if (coinRefillAt === null) {
      setCoinCountdown(null);
      if (coinTimerRef.current !== null) {
        clearInterval(coinTimerRef.current);
        coinTimerRef.current = null;
      }
      return;
    }

    function tick() {
      const remaining = (coinRefillAt as number) - Date.now();
      if (remaining <= 0) {
        setCoins(INITIAL_COINS);
        setCoinRefillAt(null);
        setCoinCountdown(null);
        showToast("🪙 Pitch coins refreshed — 5 new coins!");
        if (coinTimerRef.current !== null) {
          clearInterval(coinTimerRef.current);
          coinTimerRef.current = null;
        }
      } else {
        setCoinCountdown(formatCountdown(remaining));
      }
    }

    tick();
    coinTimerRef.current = setInterval(tick, 1000);

    return () => {
      if (coinTimerRef.current !== null) {
        clearInterval(coinTimerRef.current);
        coinTimerRef.current = null;
      }
    };
  }, [coinRefillAt, showToast]);

  // =========================================================================
  // Fetch board (R8, R6, R28)
  // =========================================================================

  const fetchBoard = useCallback(
    async (
      view: View,
      sortBy: Sort,
      statusFilter: Set<Status>,
      q: string,
    ) => {
      setLoading(true);
      setErrorMsg(null);

      const params: Parameters<typeof listFeatures>[0] = { view, sort: sortBy };
      // R6: send the whole selected set as the status query parameter
      if (view === "pipeline" && statusFilter.size > 0) {
        params.status = Array.from(statusFilter);
      }
      if (view === "vault" && q) {
        params.q = q;
      }

      const result = await listFeatures(params);

      if (result.ok) {
        setFeatures(result.data.features);
        // Seed voted set (R17)
        setVotedIds((prev) => {
          const next = new Set(prev);
          for (const f of result.data.features) {
            if (f.viewer_has_voted) next.add(f.id);
            for (const c of f.children) {
              if (c.viewer_has_voted) next.add(c.id);
            }
          }
          return next;
        });
        setErrorMsg(null);
      } else {
        setFeatures([]);
        setErrorMsg(result.message);
      }

      setLoading(false);
    },
    [],
  );

  // Fetch on mount and when view/sort/filter change
  useEffect(() => {
    fetchBoard(activeView, sort, filterStatus, debouncedQueryRef.current);
  }, [activeView, sort, filterStatus, fetchBoard]);

  // Fetch holding count for the tab badge (R4)
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const res = await listFeatures({ view: "holding" });
      if (!cancelled && res.ok) {
        setHoldingCount(res.data.features.length);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // =========================================================================
  // R32: Load broadcast event tail on mount
  // =========================================================================

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const res = await listBroadcastEvents(BROADCAST_CAP);
      if (!cancelled && res.ok && res.data.length > 0) {
        setBroadcastMessages(res.data.map(mapBroadcastEvent));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // =========================================================================
  // Vault search debounce (R7)
  // =========================================================================

  const handleVaultSearch = useCallback(
    (value: string) => {
      setVaultQuery(value);
      if (debounceTimerRef.current !== null) {
        clearTimeout(debounceTimerRef.current);
      }
      debounceTimerRef.current = setTimeout(() => {
        debouncedQueryRef.current = value;
        fetchBoard("vault", sort, new Set(), value);
      }, DEBOUNCE_MS);
    },
    [sort, fetchBoard],
  );

  // Clean up debounce timer
  useEffect(() => {
    return () => {
      if (debounceTimerRef.current !== null) clearTimeout(debounceTimerRef.current);
    };
  }, []);

  // =========================================================================
  // Realtime subscription (R12–R15, R18, R25, R32)
  // =========================================================================

  // Keep a ref to the current view so the subscription handler can read it
  // without re-subscribing on every tab change.
  const activeViewRef = useRef(activeView);
  activeViewRef.current = activeView;

  const filterStatusRef = useRef<Set<Status>>(filterStatus);
  filterStatusRef.current = filterStatus;

  useEffect(() => {
    const unsubscribe = subscribe({
      onFeatureInsert: (row) => {
        // R14: branch on parent_id
        if (row.parent_id) {
          // Split child — merge into parent's children
          setFeatures((prev) =>
            prev.map((f) => {
              if (f.id === row.parent_id) {
                // Check if child already exists
                const exists = f.children.some((c) => c.id === row.id);
                if (exists) return f;
                return { ...f, children: [...f.children, row] };
              }
              return f;
            }),
          );

          // R18: check if child crossed unlock threshold
          if (
            row.unlock_threshold !== null &&
            row.upvotes >= row.unlock_threshold
          ) {
            showToast(`🔓 Unlocked: "${row.title}"`);
          }
          return;
        }

        // R15: ignore if row doesn't belong to current view
        if (!rowBelongsToView(row, activeViewRef.current)) {
          // But update holding count if it's a holding row
          if (row.status === "POSTPONED_CONFLICT") {
            setHoldingCount((c) => c + 1);
          }
          return;
        }

        // Also check filter chip (R15) — multi-select: ignore if set is non-empty and status not in set
        if (
          activeViewRef.current === "pipeline" &&
          filterStatusRef.current.size > 0 &&
          !filterStatusRef.current.has(row.status as Status)
        ) {
          return;
        }

        // R25: remove matching pending entry and celebrate
        setLocalPending((prev) => {
          const match = prev.find((p) => p.feature_id === row.id);
          if (match) {
            showToast("🎉 Cleared screening — your pitch is live in the feed!");
            return prev.filter((p) => p.feature_id !== row.id);
          }
          return prev;
        });

        // Add to feed
        setFeatures((prev) => {
          if (prev.some((f) => f.id === row.id)) return prev;
          return [row, ...prev];
        });

        // Update holding count
        if (row.status === "POSTPONED_CONFLICT") {
          setHoldingCount((c) => c + 1);
        }
      },

      onFeatureUpdate: (row) => {
        // R13: merge incoming row rather than replacing
        // R15: ignore if not in current view
        const currentView = activeViewRef.current;

        // Update holding count if status changed
        if (row.status === "POSTPONED_CONFLICT") {
          // Could be a new holding entry — we'll refetch count
          setHoldingCount((prev) => {
            // We can't know the exact count from a single update,
            // but we can ensure it's at least 1
            return Math.max(prev, 1);
          });
        }

        if (!rowBelongsToView(row, currentView)) {
          // The row left this view — remove it from the feed
          setFeatures((prev) => prev.filter((f) => f.id !== row.id));

          // If it was holding and left, decrement
          if (currentView === "holding") {
            setHoldingCount((c) => Math.max(0, c - 1));
          }
          return;
        }

        // Also check filter chip — multi-select
        if (
          currentView === "pipeline" &&
          filterStatusRef.current.size > 0 &&
          !filterStatusRef.current.has(row.status as Status)
        ) {
          setFeatures((prev) => prev.filter((f) => f.id !== row.id));
          return;
        }

        setFeatures((prev) => {
          let found = false;
          const next = prev.map((f) => {
            if (f.id === row.id) {
              found = true;
              // R13: merge — keep children from existing state
              return { ...f, ...row, children: f.children };
            }
            return f;
          });
          if (!found) {
            // Row now belongs to this view but wasn't in the list
            return [...next, { ...row, children: [] }];
          }
          return next;
        });

        // R18: check children for unlock
        // (children come via insert, not update, so this is mainly for
        // the parent row update that might carry updated child data)
      },

      // R32: append live broadcast events to the ticker
      onBroadcastEvent: (row) => {
        const msg = mapBroadcastEvent(row);
        setBroadcastMessages((prev) => {
          const next = [...prev, msg];
          // R35: cap at BROADCAST_CAP, drop oldest
          if (next.length > BROADCAST_CAP) {
            return next.slice(next.length - BROADCAST_CAP);
          }
          return next;
        });
      },
    });

    return unsubscribe; // R12: dispose on unmount
  }, [showToast]);

  // =========================================================================
  // Vote handler (R16, R17)
  // =========================================================================

  const handleUpvote = useCallback(
    async (id: string) => {
      if (pendingVoteId) return;
      if (votedIds.has(id)) return;

      setPendingVoteId(id);

      const result = await upvote(id);

      if (result.ok) {
        // Reconcile count from response
        setFeatures((prev) =>
          prev.map((f) => {
            if (f.id === id) {
              return { ...f, upvotes: result.data.upvotes, viewer_has_voted: true };
            }
            // Check children
            if (f.children.some((c) => c.id === id)) {
              return {
                ...f,
                children: f.children.map((c) =>
                  c.id === id
                    ? { ...c, upvotes: result.data.upvotes, viewer_has_voted: true }
                    : c,
                ),
              };
            }
            return f;
          }),
        );
        setVotedIds((prev) => new Set(prev).add(id));

        // R18: check if a child crossed its unlock threshold
        for (const f of features) {
          for (const c of f.children) {
            if (
              c.id === id &&
              c.unlock_threshold !== null &&
              result.data.upvotes >= c.unlock_threshold
            ) {
              showToast(`🔓 Unlocked: "${c.title}"`);
            }
          }
        }
      } else if (result.status === 409) {
        // R16: already voted — mark as voted, not an error
        setVotedIds((prev) => new Set(prev).add(id));
      }

      setPendingVoteId(null);
    },
    [pendingVoteId, votedIds, features, showToast],
  );

  // =========================================================================
  // Pitch handler (R19–R24)
  // =========================================================================

  const handlePitch = useCallback(
    async (input: {
      title: string;
      description: string;
    }): Promise<ApiResult<{ feature_id: string; state: "screening" }>> => {
      const result = await createFeature(input);

      if (result.ok) {
        // R19: decrement coin
        setCoins((prev) => {
          const next = prev - 1;
          if (next <= 0) {
            // Start refill timer
            setCoinRefillAt(Date.now() + COIN_REFILL_MS);
          }
          return Math.max(0, next);
        });
      } else if (result.status === 429) {
        // R21: server says rate limited
        setCoins(0);
        if (result.resets_at) {
          setResetsAt(result.resets_at);
          const resetTime = new Date(result.resets_at).getTime();
          setCoinRefillAt(resetTime);
        } else {
          setCoinRefillAt(Date.now() + COIN_REFILL_MS);
        }
      }

      return result;
    },
    [],
  );

  const handlePitched = useCallback(
    (featureId: string, title: string) => {
      // R23: show screening toast
      showToast(
        "🛡️ Pitched! Guardagent is screening it — track it in My pitches.",
      );

      // R23: add pending entry
      setLocalPending((prev) => [
        ...prev,
        {
          feature_id: featureId,
          title,
          submitted_at: new Date().toISOString(),
        },
      ]);
    },
    [showToast],
  );

  // =========================================================================
  // My Pitches (R22)
  // =========================================================================

  const loadMyPitches = useCallback(async () => {
    setMyPitchesLoading(true);
    setMyPitchesError(null);

    const result = await getMyPitches();

    if (result.ok) {
      setMyPitchesPending(result.data.pending);
      setMyPitchesFeatures(result.data.features);
    } else {
      setMyPitchesError(result.message);
    }

    setMyPitchesLoading(false);
  }, []);

  const handleOpenMyPitches = useCallback(() => {
    setMyPitchesOpen(true);
    loadMyPitches();
  }, [loadMyPitches]);

  const handleDismissPending = useCallback((featureId: string) => {
    setLocalPending((prev) => prev.filter((p) => p.feature_id !== featureId));
    setMyPitchesPending((prev) =>
      prev.filter((p) => p.feature_id !== featureId),
    );
  }, []);

  // Build combined pending list for the modal
  const combinedPending: PendingPitch[] = [
    ...myPitchesPending,
    ...localPending
      .filter(
        (lp) => !myPitchesPending.some((mp) => mp.feature_id === lp.feature_id),
      )
      .map(
        (lp): PendingPitch => ({
          feature_id: lp.feature_id,
          title: lp.title,
          state: "screening",
          reason: null,
          shipped_version: null,
          merged_into_feature_id: null,
          merged_into_title: null,
          submitted_at: lp.submitted_at,
        }),
      ),
  ];

  // Detect rejected pitches from Realtime / my-pitches refresh
  const prevPendingRef = useRef<PendingPitch[]>([]);
  useEffect(() => {
    // Check if any new rejections appeared
    for (const p of myPitchesPending) {
      if (p.state === "rejected") {
        const wasPreviouslyKnown = prevPendingRef.current.some(
          (pp) => pp.feature_id === p.feature_id && pp.state === "rejected",
        );
        if (!wasPreviouslyKnown) {
          showToast(
            "🛡️ A pitch didn't clear screening — check My pitches.",
          );
          break; // one toast at a time
        }
      }
    }
    prevPendingRef.current = myPitchesPending;
  }, [myPitchesPending, showToast]);

  // =========================================================================
  // Vault reboot (R31)
  // =========================================================================

  const handleReboot = useCallback(
    async (id: string) => {
      // R31: the reboot persists (US-16). Only drop the row from the Vault once
      // the server has actually moved it — otherwise a failed call would leave
      // the visitor believing an idea was revived when it was not.
      const result = await rebootFeature(id);
      if (result.ok) {
        setFeatures((prev) => prev.filter((f) => f.id !== id));
        showToast("⚡ Rebooted! A fresh 30-day VOTING window has started.");
        return;
      }
      if (result.status === 422) {
        // Another visitor revived it first — a race, not a failure.
        setFeatures((prev) => prev.filter((f) => f.id !== id));
        showToast("⚡ Already back on the board — someone beat you to it!");
        return;
      }
      showToast(result.message);
    },
    [showToast],
  );

  // =========================================================================
  // Broadcast success → sandbox pulse (R26)
  // =========================================================================

  const handleBroadcastSuccess = useCallback(() => {
    setSandboxPulse(true);
  }, []);

  const handleSandboxRefreshed = useCallback(() => {
    setSandboxPulse(false);
  }, []);

  // =========================================================================
  // Tab change
  // =========================================================================

  const handleTabChange = useCallback(
    (view: View) => {
      if (view === activeView) return;
      setActiveView(view);
      setFilterStatus(new Set());
      setVaultQuery("");
      debouncedQueryRef.current = "";
    },
    [activeView],
  );

  // =========================================================================
  // Filter chip toggle (R5b: multi-select)
  // =========================================================================

  const handleChipToggle = useCallback((status: Status | null) => {
    if (status === null) {
      // "All" clears the set
      setFilterStatus(new Set());
    } else {
      setFilterStatus((prev) => {
        const next = new Set(prev);
        if (next.has(status)) {
          next.delete(status);
        } else {
          next.add(status);
        }
        return next;
      });
    }
  }, []);

  // =========================================================================
  // Retry on error (R10)
  // =========================================================================

  const handleRetry = useCallback(() => {
    fetchBoard(activeView, sort, filterStatus, debouncedQueryRef.current);
  }, [activeView, sort, filterStatus, fetchBoard]);

  // =========================================================================
  // Derived state
  // =========================================================================

  const unresolvedPitchCount =
    localPending.length +
    myPitchesPending.filter((p) => p.state === "screening").length;

  const pitchDisabled = coins <= 0;

  // =========================================================================
  // Render helpers
  // =========================================================================

  function renderFeedContent(): JSX.Element {
    // R9: loading state
    if (loading) {
      return (
        <div className="feed-status" role="status" aria-live="polite">
          <div className="skeleton" />
          <div className="skeleton" />
          <div className="skeleton" />
          <span className="visually-hidden">Loading features…</span>
        </div>
      );
    }

    // R9: error state — MUST NOT render as empty
    if (errorMsg) {
      return (
        <div className="feed-status is-error" role="alert" aria-live="assertive">
          <p>{errorMsg}</p>
          <button className="btn-ghost" onClick={handleRetry}>
            Retry
          </button>
        </div>
      );
    }

    // R9: empty state
    if (features.length === 0) {
      return (
        <div className="feed-status" role="status" aria-live="polite">
          {activeView === "vault" && vaultQuery
            ? "Nothing in the Vault matches that search."
            : activeView === "vault"
              ? "The Vault is empty — every idea is still in play."
              : "No features in this stage right now."}
        </div>
      );
    }

    // Render cards based on view (R8)
    return (
      <>
        {features.map((f) => {
          switch (activeView) {
            case "pipeline":
              return (
                <FeatureCard
                  key={f.id}
                  feature={f}
                  votedIds={votedIds}
                  onUpvote={handleUpvote}
                  pendingVoteId={pendingVoteId}
                />
              );
            case "shipped":
              return <ShippedCard key={f.id} feature={f} />;
            case "holding":
              return <HoldingCard key={f.id} feature={f} />;
            case "vault":
              return (
                <VaultCard key={f.id} feature={f} onReboot={handleReboot} />
              );
            default:
              return null;
          }
        })}
      </>
    );
  }

  // =========================================================================
  // Render
  // =========================================================================

  return (
    <div className="app">
      {/* R1: Broadcast chyron — R34: leave messages unset when empty */}
      <Broadcast
        messages={broadcastMessages.length > 0 ? broadcastMessages : undefined}
        onSuccessPhase={handleBroadcastSuccess}
      />

      {/* R1, R2: Masthead */}
      <header className="masthead">
        <div className="brand">
          <h1>
            upvote<span className="brand-dot">·</span>dev
          </h1>
          <p className="tagline">You wish it. Agents ship it.</p>
        </div>

        {/* R3: Masthead actions — both buttons carry both classes */}
        <div className="masthead-actions">
          {/* Coin chip */}
          <span
            className="coin-chip"
            title="Pitch Coins — each pitch costs 1. Refills daily (2 min in this demo)."
          >
            🪙 {coins}
          </span>

          {/* My pitches button — btn-ghost btn-mypitches (R3) */}
          <button
            className="btn-ghost btn-mypitches"
            onClick={handleOpenMyPitches}
          >
            🔒 My pitches
            {unresolvedPitchCount > 0 && (
              <span className="pitch-count">{unresolvedPitchCount}</span>
            )}
          </button>

          {/* Pitch button — btn-primary btn-pitch (R3, R20) */}
          <button
            className="btn-primary btn-pitch"
            onClick={() => setSubmitOpen(true)}
            disabled={pitchDisabled}
          >
            {pitchDisabled && coinCountdown
              ? `🪙 Coins refresh in ${coinCountdown}`
              : "🔥 Pitch a feature"}
          </button>
        </div>
      </header>

      {/* R1: Layout — feed + sandbox */}
      <div className="layout">
        {/* Feed column */}
        <main
          className="feed"
          role="region"
          aria-label="Feature board"
        >
          {/* R4: Tabs */}
          <nav className="tabs" aria-label="Board views">
            {TABS.map((tab) => (
              <button
                key={tab.view}
                className={`tab${activeView === tab.view ? " is-active" : ""}`}
                aria-current={activeView === tab.view ? "page" : undefined}
                onClick={() => handleTabChange(tab.view)}
              >
                {tab.label}
                {tab.view === "holding" && holdingCount > 0 && (
                  <span className="tab-count">{holdingCount}</span>
                )}
              </button>
            ))}
          </nav>

          {/* R5, R5a: Sort toggle and filter chips as SIBLINGS (pipeline only) */}
          {activeView === "pipeline" && (
            <>
              <div className="feed-controls">
                <span className="feed-controls-label">Sort by</span>
                <div className="sort-toggle">
                  <button
                    className={sort === "top" ? "is-active" : ""}
                    onClick={() => setSort("top")}
                  >
                    🔥 Highest Upvotes
                  </button>
                  <button
                    className={sort === "new" ? "is-active" : ""}
                    onClick={() => setSort("new")}
                  >
                    ✨ Newest
                  </button>
                </div>
              </div>

              {/* R5a, R5b, R6: Filter chips — sibling of feed-controls, multi-select */}
              <div className="filter-chips">
                {FILTER_CHIPS.map((chip) => (
                  <button
                    key={chip.label}
                    className={`chip${
                      chip.status === null
                        ? filterStatus.size === 0
                          ? " is-active"
                          : ""
                        : filterStatus.has(chip.status)
                          ? " is-active"
                          : ""
                    }`}
                    onClick={() => handleChipToggle(chip.status)}
                  >
                    {chip.label}
                  </button>
                ))}
              </div>
            </>
          )}

          {/* R7: Vault search */}
          {activeView === "vault" && (
            <div className="feed-controls">
              <input
                className="vault-search"
                type="search"
                placeholder="Search the Vault…"
                value={vaultQuery}
                onChange={(e) => handleVaultSearch(e.target.value)}
              />
            </div>
          )}

          {/* R11: Section blurbs */}
          {activeView === "shipped" && !loading && !errorMsg && (
            <p className="section-blurb">
              🏆 {features.length} features shipped by this community — trophies,
              not tickets. Everything here stays forever.
            </p>
          )}
          {activeView === "holding" && !loading && !errorMsg && (
            <p className="section-blurb">
              Requests the community loved but the current architecture can&apos;t
              hold yet. Nothing here is rejected — the agents re-check every
              cycle.
            </p>
          )}
          {activeView === "vault" && !loading && !errorMsg && features.length > 0 && (
            <p className="section-blurb">
              Ideas archived due to low community velocity. Reboot any request to
              give it a fresh 30-day voting window.
            </p>
          )}

          {/* Feed content */}
          {renderFeedContent()}
        </main>

        {/* R1: Sandbox panel */}
        <SandboxPanel
          url={sandboxUrl}
          pulse={sandboxPulse}
          onRefreshed={handleSandboxRefreshed}
        />
      </div>

      {/* R22: Submit modal */}
      <SubmitModal
        open={submitOpen}
        onClose={() => setSubmitOpen(false)}
        onPitch={handlePitch}
        onPitched={handlePitched}
        coinsRemaining={coins}
        resetsAt={resetsAt}
      />

      {/* R22: My Pitches modal */}
      <MyPitchesModal
        open={myPitchesOpen}
        onClose={() => setMyPitchesOpen(false)}
        pending={combinedPending}
        features={myPitchesFeatures}
        loading={myPitchesLoading}
        error={myPitchesError}
        onDismiss={handleDismissPending}
        onRetry={loadMyPitches}
      />

      {/* R27: Toast */}
      {toast && (
        <div className="toast" role="status">
          {toast}
        </div>
      )}
    </div>
  );
}
