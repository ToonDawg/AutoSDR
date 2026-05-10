import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, RotateCcw, RefreshCw, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { FilterTabs, type FilterOption } from "@/components/ui/FilterTabs";
import {
  HITL_LABEL,
  HITL_REASON_CHIP_LABEL,
  INTENT_LABEL,
  evalScoreTone,
  relTime,
} from "@/lib/format";
import { type Thread } from "@/lib/types";
import { useHitlCount, useHitlThreads } from "@/lib/useHitlThreads";

/**
 * "Needs your eye" — triage view for threads that the operator is on
 * the hook to handle.
 *
 * Two tabs:
 *   - Active: paused-for-HITL threads the operator hasn't acknowledged.
 *   - Recently dismissed: threads they swept off the queue, kept for 30
 *     days as an audit trail. Anything older is hidden client-side; the
 *     row stays in the DB.
 *
 * A *new* HITL event (lead replies, eval fails again, take-over,
 * regenerate) automatically clears ``hitl_dismissed_at`` server-side, so
 * dismiss behaves like an ack — not an indefinite mute. See
 * ``autosdr/pipeline/_shared.py::pause_thread_for_hitl``.
 */

type Tab = "active" | "dismissed";

const TABS: ReadonlyArray<FilterOption<Tab>> = [
  { id: "active", label: "Active" },
  { id: "dismissed", label: "Recently dismissed" },
];

const PAGE_SIZE = 50;
const HISTORY_RETENTION_DAYS = 30;
const HISTORY_CUTOFF_MS = HISTORY_RETENTION_DAYS * 24 * 3600 * 1000;

/**
 * Reason filter chip type. ``"all"`` means "no filter"; a specific
 * reason maps 1-1 to a ``hitl_reason`` value the server understands;
 * ``"other"`` is a client-side bucket that catches everything not in
 * the explicitly-listed reasons (mostly classifier flags) and asks the
 * server for ``hitl_reason=unknown`` while also showing whatever the
 * user picked in the dropdown — kept simple in v0 by just filtering
 * client-side. See ticket 0018.
 */
type ReasonFilter =
  | "all"
  | "awaiting_human_reply"
  | "connector_send_failed"
  | "eval_failed_after_max_attempts"
  | "other";

/**
 * Reasons promoted to top-level chips in the filter row. Anything else
 * (bot_check, low_confidence, …) folds into the "Other" chip whose
 * count is computed from ``count.by_reason`` minus the promoted ones.
 */
const PROMOTED_REASONS: ReadonlyArray<
  Exclude<ReasonFilter, "all" | "other">
> = [
  "connector_send_failed",
  "eval_failed_after_max_attempts",
  "awaiting_human_reply",
];

export function Inbox() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("active");
  const [reason, setReason] = useState<ReasonFilter>("all");
  const [activePages, setActivePages] = useState(1);
  const [historyPages, setHistoryPages] = useState(1);
  const [selected, setSelected] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const [requeueCount, setRequeueCount] = useState<number | null>(null);

  const pages = tab === "active" ? activePages : historyPages;
  const setPages = tab === "active" ? setActivePages : setHistoryPages;

  // Server-side ``hitl_reason`` only fires when a specific promoted
  // chip is active. ``"other"`` is filtered client-side because it's
  // an open-ended union that the server doesn't know about as a
  // single token, so we ask the server for everything-not-promoted is
  // simply the unfiltered set minus the promoted reasons.
  const serverReasonFilter = useMemo(() => {
    if (tab !== "active") return undefined;
    if (reason === "all" || reason === "other") return undefined;
    return reason;
  }, [tab, reason]);

  const { data: threads, isLoading } = useHitlThreads({
    dismissed: tab === "dismissed",
    limit: PAGE_SIZE * pages,
    reason: serverReasonFilter,
  });
  const { data: count } = useHitlCount();

  // History tab hides anything older than 30 days client-side. Active
  // tab + "Other" chip applies a client-side filter that catches every
  // ``hitl_reason`` not in ``PROMOTED_REASONS`` (matches the server
  // ``by_reason`` "Other" bucket).
  const visibleThreads = useMemo(() => {
    if (!threads) return [];
    if (tab === "dismissed") {
      const cutoff = new Date().getTime() - HISTORY_CUTOFF_MS;
      return threads.filter((t) => {
        const at = t.hitl_dismissed_at ? Date.parse(t.hitl_dismissed_at) : 0;
        return at >= cutoff;
      });
    }
    if (reason === "other") {
      return threads.filter(
        (t) =>
          !t.hitl_reason ||
          !PROMOTED_REASONS.includes(t.hitl_reason as never),
      );
    }
    return threads;
  }, [threads, tab, reason]);

  // Server returned a full page → assume more exist. The client-side
  // 30-day filter on the history tab is intentionally ignored here so a
  // page full of 31-day-old rows still lets the user fetch the next
  // batch (which may include in-window rows).
  const canLoadMore =
    threads !== undefined && threads.length >= PAGE_SIZE * pages;

  const invalidateHitl = () => {
    qc.invalidateQueries({ queryKey: ["threads", "hitl"] });
  };

  const dismiss = useMutation({
    mutationFn: (id: string) => api.dismissThread(id),
    onSuccess: invalidateHitl,
  });
  const restore = useMutation({
    mutationFn: (id: string) => api.restoreThread(id),
    onSuccess: invalidateHitl,
  });
  const dismissBulk = useMutation({
    mutationFn: (ids: string[]) =>
      Promise.all(ids.map((id) => api.dismissThread(id))),
    onSuccess: () => {
      setSelected(new Set());
      invalidateHitl();
    },
  });

  const requeueBulk = useMutation({
    mutationFn: (ids: readonly string[]) =>
      api.requeueThreads({ thread_ids: Array.from(ids) }),
    onSuccess: (resp) => {
      setSelected(new Set());
      setRequeueCount(resp.requeued);
      invalidateHitl();
    },
  });

  const onTabChange = (next: Tab) => {
    setTab(next);
    setReason("all");
    setSelected(new Set());
    setRequeueCount(null);
  };

  const onReasonChange = (next: ReasonFilter) => {
    setReason(next);
    setSelected(new Set());
    setRequeueCount(null);
  };

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectAllVisible = () => {
    setSelected(new Set(visibleThreads.map((t) => t.id)));
  };

  const tabCounts: Record<Tab, number> = {
    active: count?.active ?? 0,
    dismissed: count?.dismissed ?? 0,
  };

  const headerTitle =
    tab === "active"
      ? tabCounts.active > 0
        ? `${tabCounts.active} thread${tabCounts.active === 1 ? "" : "s"} waiting`
        : "All clear"
      : `${visibleThreads.length} dismissed in the last ${HISTORY_RETENTION_DAYS} days`;

  const headerSubtitle =
    tab === "active"
      ? "First-message-only mode: AutoSDR never answers a reply without you picking a draft."
      : "Threads you set aside. A new HITL event automatically pulls them back to Active.";

  const allVisibleSelected =
    visibleThreads.length > 0 && selected.size === visibleThreads.length;
  const actionPending =
    dismiss.isPending ||
    restore.isPending ||
    dismissBulk.isPending ||
    requeueBulk.isPending;

  // Reason-chip counts. ``count.by_reason`` is the server snapshot for
  // *all* active paused threads; promoted reasons sum into "Other"'s
  // residual.
  const byReason = count?.by_reason ?? {};
  const reasonCounts: Record<ReasonFilter, number> = useMemo(() => {
    const promoted = PROMOTED_REASONS.reduce(
      (sum, r) => sum + (byReason[r] ?? 0),
      0,
    );
    const total = count?.active ?? 0;
    return {
      all: total,
      awaiting_human_reply: byReason.awaiting_human_reply ?? 0,
      connector_send_failed: byReason.connector_send_failed ?? 0,
      eval_failed_after_max_attempts:
        byReason.eval_failed_after_max_attempts ?? 0,
      other: Math.max(0, total - promoted),
    };
  }, [byReason, count?.active]);

  // Connector-failed sweep candidates — when the user is on the
  // connector chip we can offer a "Retry all N" button that pulls the
  // visible-page worth of IDs (capped at the server batch size by the
  // mutation's chunking).
  const connectorFailedIds = useMemo(() => {
    if (reason !== "connector_send_failed") return [];
    return visibleThreads
      .filter((t) => t.hitl_reason === "connector_send_failed")
      .map((t) => t.id);
  }, [reason, visibleThreads]);

  const selectedConnectorFailedCount = useMemo(() => {
    if (selected.size === 0) return 0;
    return visibleThreads.filter(
      (t) =>
        selected.has(t.id) && t.hitl_reason === "connector_send_failed",
    ).length;
  }, [selected, visibleThreads]);

  return (
    <div className="page-narrow gap-6">
      <header className="border-b border-rule pb-4">
        <h1 className="text-2xl font-medium">{headerTitle}</h1>
        <p className="text-sm text-ink-muted mt-1 max-w-prose">
          {headerSubtitle}
        </p>
      </header>

      <FilterTabs<Tab>
        options={TABS}
        active={tab}
        onChange={onTabChange}
        counts={tabCounts}
      />

      {tab === "active" && (
        <ReasonChipRow
          active={reason}
          counts={reasonCounts}
          onChange={onReasonChange}
        />
      )}

      {requeueCount !== null && (
        <div className="paper-card border-l-4 border-l-[color:var(--color-forest)] bg-forest-soft px-4 py-3 flex items-center justify-between">
          <span className="text-sm">
            {requeueCount} thread{requeueCount === 1 ? "" : "s"} re-queued — the scheduler will send them within the daily limit.
          </span>
          <button
            type="button"
            onClick={() => setRequeueCount(null)}
            className="text-xs text-ink-muted hover:text-ink cursor-pointer ml-4"
          >
            Dismiss
          </button>
        </div>
      )}

      {tab === "active" && visibleThreads.length > 0 && (
        <div className="paper-card flex items-center justify-between px-4 py-2">
          <div className="flex items-center gap-3 text-xs text-ink-muted">
            <button
              type="button"
              onClick={() =>
                allVisibleSelected ? setSelected(new Set()) : selectAllVisible()
              }
              className="text-xs text-ink-muted hover:text-ink cursor-pointer"
            >
              {allVisibleSelected ? "Clear selection" : "Select all"}
            </button>
            {selected.size > 0 && (
              <span className="font-mono">{selected.size} selected</span>
            )}
            {requeueBulk.isPending && (
              <span className="font-mono text-ink-faint">re-queuing…</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            {reason === "connector_send_failed" &&
              selectedConnectorFailedCount > 0 && (
                <Button
                  variant="primary"
                  size="sm"
                  iconLeft={<RefreshCw className="h-3.5 w-3.5" strokeWidth={1.5} />}
                  disabled={actionPending}
                  onClick={() => {
                    const ids = visibleThreads
                      .filter(
                        (t) =>
                          selected.has(t.id) &&
                          t.hitl_reason === "connector_send_failed",
                      )
                      .map((t) => t.id);
                    requeueBulk.mutate(ids);
                  }}
                >
                  Re-queue {selectedConnectorFailedCount}
                </Button>
              )}
            {reason === "connector_send_failed" &&
              selected.size === 0 &&
              connectorFailedIds.length > 0 && (
                <Button
                  variant="ghost"
                  size="sm"
                  iconLeft={<RefreshCw className="h-3.5 w-3.5" strokeWidth={1.5} />}
                  disabled={actionPending}
                  onClick={() => requeueBulk.mutate(connectorFailedIds)}
                >
                  Re-queue all {connectorFailedIds.length}
                </Button>
              )}
            <Button
              variant={reason === "connector_send_failed" ? "ghost" : "primary"}
              size="sm"
              iconLeft={<Trash2 className="h-3.5 w-3.5" strokeWidth={1.5} />}
              disabled={selected.size === 0 || actionPending}
              onClick={() => dismissBulk.mutate(Array.from(selected))}
            >
              Dismiss {selected.size || ""}
            </Button>
          </div>
        </div>
      )}

      {isLoading && <div className="text-sm text-ink-muted">Loading…</div>}

      {visibleThreads.length > 0 && (
        <ul className="paper-card divide-y divide-rule">
          {visibleThreads.map((t) => (
            <li key={t.id}>
              <HitlRow
                thread={t}
                variant={tab}
                selected={selected.has(t.id)}
                onToggleSelect={() => toggleSelect(t.id)}
                onDismiss={() => dismiss.mutate(t.id)}
                onRestore={() => restore.mutate(t.id)}
                actionPending={actionPending}
              />
            </li>
          ))}
        </ul>
      )}

      {!isLoading && visibleThreads.length === 0 && (
        <div className="paper-card px-6 py-12 text-center">
          <div className="text-sm font-medium mb-1">
            {tab === "active"
              ? "Nothing to look at."
              : `Nothing dismissed in the last ${HISTORY_RETENTION_DAYS} days.`}
          </div>
          <p className="text-sm text-ink-muted">
            {tab === "active" ? (
              <>
                New lead replies will show up here the moment they arrive.{" "}
                <Link to="/threads" className="underline">
                  Browse all threads
                </Link>
                .
              </>
            ) : (
              `Older entries roll off the audit trail after ${HISTORY_RETENTION_DAYS} days.`
            )}
          </p>
        </div>
      )}

      {canLoadMore && (
        <div className="flex justify-center">
          <Button variant="ghost" size="sm" onClick={() => setPages(pages + 1)}>
            Load more
          </Button>
        </div>
      )}
    </div>
  );
}

interface HitlRowProps {
  thread: Thread;
  variant: Tab;
  selected: boolean;
  onToggleSelect: () => void;
  onDismiss: () => void;
  onRestore: () => void;
  actionPending: boolean;
}

function HitlRow({
  thread,
  variant,
  selected,
  onToggleSelect,
  onDismiss,
  onRestore,
  actionPending,
}: HitlRowProps) {
  const ctx = thread.hitl_context as
    | (Thread["hitl_context"] & {
        last_drafts?: string[];
        last_scores?: { overall?: number; feedback?: string | null }[];
      })
    | null;

  const topSuggestion = ctx?.suggestions?.[0];

  // Fallback preview for eval_failed / connector_failed threads: they
  // don't carry `suggestions`, but `last_drafts` + `last_scores` are
  // stashed so the operator can see the best attempt at a glance.
  const lastDrafts = ctx?.last_drafts ?? [];
  const lastScores = ctx?.last_scores ?? [];
  const lastIdx = lastDrafts.length - 1;
  const fallbackDraft = lastIdx >= 0 ? lastDrafts[lastIdx] : null;
  const fallbackScore =
    lastIdx >= 0 && lastScores[lastIdx]?.overall != null
      ? Math.round((lastScores[lastIdx]!.overall as number) * 100)
      : null;

  const previewDraft = topSuggestion?.draft ?? fallbackDraft;
  const previewScore =
    topSuggestion && topSuggestion.overall != null
      ? Math.round(topSuggestion.overall * 100)
      : !topSuggestion
        ? fallbackScore
        : null;
  const previewLabel = topSuggestion ? "top draft" : "last attempt";
  const scoreTone =
    previewScore != null ? evalScoreTone(previewScore) : "neutral";
  const dimmed = variant === "dismissed";
  const reasonLabel = thread.hitl_reason
    ? (HITL_LABEL[thread.hitl_reason] ?? "Needs you")
    : "Needs you";
  const timestampLabel =
    variant === "dismissed" && thread.hitl_dismissed_at
      ? `dismissed ${relTime(thread.hitl_dismissed_at)}`
      : relTime(thread.last_message_at);

  return (
    <div
      className={`flex flex-col sm:flex-row items-stretch group transition-colors hover:bg-paper-deep ${
        dimmed ? "opacity-75" : ""
      }`}
    >
      {variant === "active" && (
        <label className="pl-5 pr-1 pt-3 sm:py-4 flex items-start cursor-pointer select-none">
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggleSelect}
            className="mt-0.5 h-4 w-4 accent-ink cursor-pointer"
            aria-label={`Select ${thread.lead_name ?? "thread"}`}
          />
        </label>
      )}

      <Link
        to={`/threads/${thread.id}`}
        className={`flex-1 min-w-0 py-4 ${
          variant === "active" ? "pl-2 sm:pl-2 pr-4 sm:pr-3" : "px-4 sm:px-5"
        }`}
      >
        <div className="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-1.5 sm:gap-4 mb-2">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 min-w-0">
            <span className="text-sm font-medium truncate">
              {thread.lead_name ?? "Unknown lead"}
            </span>
            <span className="text-xs text-ink-muted truncate">
              {thread.campaign_name}
            </span>
          </div>
          <div className="flex items-center gap-2 shrink-0 flex-wrap">
            <Badge tone={dimmed ? "neutral" : "rust"} uppercase={false}>
              {reasonLabel}
            </Badge>
            <span className="text-xs text-ink-faint font-mono">
              {timestampLabel}
            </span>
          </div>
        </div>

        {ctx?.incoming_message && (
          <p className="text-sm text-ink-muted mb-2 line-clamp-2">
            <span className="text-ink-faint">they said:</span>{" "}
            {ctx.incoming_message}
          </p>
        )}

        {previewDraft && (
          <div className="flex flex-col sm:flex-row items-start gap-2 sm:gap-3 pt-2 mt-1 border-t border-rule">
            <Badge tone={scoreTone} uppercase={false} className="shrink-0">
              {previewLabel}
              {previewScore != null ? ` · ${previewScore}` : ""}
            </Badge>
            <p className="text-sm text-ink line-clamp-2 flex-1">
              {previewDraft}
            </p>
            <ArrowRight
              className="hidden sm:block h-4 w-4 text-ink-muted shrink-0 mt-0.5 group-hover:translate-x-0.5 transition-transform"
              strokeWidth={1.5}
            />
          </div>
        )}

        {!previewDraft && ctx?.intent && (
          <div className="text-xs text-ink-muted mt-1">
            classified as{" "}
            <span className="text-rust">
              {INTENT_LABEL[ctx.intent] ?? ctx.intent}
            </span>
            {ctx.confidence != null && (
              <span className="text-ink-faint">
                {" "}
                · {Math.round(ctx.confidence * 100)}% confidence
              </span>
            )}
          </div>
        )}
      </Link>

      <div className="flex items-stretch sm:items-start sm:py-4 sm:pr-4 sm:pl-2 border-t sm:border-t-0 border-rule">
        {variant === "active" ? (
          <button
            type="button"
            onClick={onDismiss}
            disabled={actionPending}
            className="flex-1 sm:flex-none text-xs text-ink-muted hover:text-rust px-3 py-3 sm:py-1 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center justify-center gap-1 min-h-[44px]"
            title="Dismiss this thread"
          >
            <Trash2 className="h-3 w-3" strokeWidth={1.5} />
            Dismiss
          </button>
        ) : (
          <button
            type="button"
            onClick={onRestore}
            disabled={actionPending}
            className="flex-1 sm:flex-none text-xs text-ink-muted hover:text-ink px-3 py-3 sm:py-1 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center justify-center gap-1 min-h-[44px]"
            title="Restore this thread"
          >
            <RotateCcw className="h-3 w-3" strokeWidth={1.5} />
            Restore
          </button>
        )}
      </div>
    </div>
  );
}

interface ReasonChipRowProps {
  active: ReasonFilter;
  counts: Record<ReasonFilter, number>;
  onChange: (next: ReasonFilter) => void;
}

/**
 * Lightweight chip row that filters the active Inbox by ``hitl_reason``
 * (ticket 0018). Counts come straight off ``GET /api/threads/hitl/count``
 * — the "Other" bucket is computed in the parent as
 * ``active - sum(promoted)`` so it stays consistent with whatever the
 * server reports without requiring the client to enumerate every
 * possible reason token.
 *
 * The "Awaiting reply" chip is included even though the bulk-retry
 * button only shows for ``connector_send_failed`` because operators
 * want a way to focus on one bucket at a time regardless.
 */
function ReasonChipRow({ active, counts, onChange }: ReasonChipRowProps) {
  const chips: ReadonlyArray<{ id: ReasonFilter; label: string }> = [
    { id: "all", label: "All" },
    {
      id: "connector_send_failed",
      label: HITL_REASON_CHIP_LABEL.connector_send_failed!,
    },
    {
      id: "eval_failed_after_max_attempts",
      label: HITL_REASON_CHIP_LABEL.eval_failed_after_max_attempts!,
    },
    {
      id: "awaiting_human_reply",
      label: HITL_REASON_CHIP_LABEL.awaiting_human_reply!,
    },
    { id: "other", label: "Other" },
  ];
  return (
    <div
      role="tablist"
      aria-label="Filter by reason"
      className="flex flex-wrap items-center gap-1.5"
    >
      {chips.map((chip) => {
        const n = counts[chip.id] ?? 0;
        const isActive = chip.id === active;
        return (
          <button
            key={chip.id}
            type="button"
            role="tab"
            aria-selected={isActive}
            onClick={() => onChange(chip.id)}
            className={`text-xs px-2.5 py-1 rounded-full border cursor-pointer transition-colors ${
              isActive
                ? "border-ink bg-ink text-paper"
                : "border-rule text-ink-muted hover:text-ink hover:border-ink-muted"
            }`}
          >
            {chip.label}
            <span
              className={`font-mono ml-1.5 ${
                isActive ? "text-paper-deep" : "text-ink-faint"
              }`}
            >
              {n}
            </span>
          </button>
        );
      })}
    </div>
  );
}

