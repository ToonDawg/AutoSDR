import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Link } from "react-router-dom";
import { useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { ThreadStatusBadge } from "@/components/domain/ThreadStatusBadge";
import { FilterTabs, type FilterOption } from "@/components/ui/FilterTabs";
import { PageHeader } from "@/components/ui/PageHeader";
import { SearchInput } from "@/components/ui/SearchInput";
import { formatPhone, relTime } from "@/lib/format";
import type { Thread, ThreadStatusT } from "@/lib/types";

const FILTERS: ReadonlyArray<FilterOption<ThreadStatusT | "all">> = [
  { id: "all", label: "All" },
  { id: "paused_for_hitl", label: "Needs you" },
  { id: "active", label: "Active" },
  { id: "won", label: "Won" },
  { id: "lost", label: "Lost" },
];

const ROW_GRID =
  "grid grid-cols-[28%_24%_22%_12%_14%] items-start";
const ROW_HEIGHT = 56;

/**
 * Full threads index with status filter + client-side search. The
 * backend can filter by status server-side, but pulling the full set
 * once lets us compute tab counts without N queries.
 */
export function Threads() {
  const [filter, setFilter] = useState<ThreadStatusT | "all">("all");
  const [q, setQ] = useState("");
  const { data: threads } = useQuery({
    queryKey: ["threads"],
    queryFn: () => api.listThreads({ limit: 500 }),
  });

  const filtered = useMemo(() => {
    if (!threads) return [];
    let out: Thread[] = threads;
    if (filter !== "all") out = out.filter((t) => t.status === filter);
    if (q.trim()) {
      const s = q.trim().toLowerCase();
      out = out.filter(
        (t) =>
          (t.lead_name ?? "").toLowerCase().includes(s) ||
          t.campaign_name.toLowerCase().includes(s) ||
          (t.lead_address ?? "").toLowerCase().includes(s),
      );
    }
    return out.sort((a, b) =>
      (b.last_message_at ?? "").localeCompare(a.last_message_at ?? ""),
    );
  }, [threads, filter, q]);

  const counts = useMemo(() => {
    const m = new Map<string, number>();
    m.set("all", threads?.length ?? 0);
    threads?.forEach((t) => m.set(t.status, (m.get(t.status) ?? 0) + 1));
    return m;
  }, [threads]);

  // Virtualize: even at limit:500 we only render the visible rows.
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const rowVirtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 8,
  });

  return (
    <div className="page gap-5">
      <PageHeader
        title="Threads"
        description="Every conversation AutoSDR has opened."
        right={
          <SearchInput
            value={q}
            onChange={setQ}
            placeholder="Search threads…"
            className="w-72"
          />
        }
      />

      <FilterTabs options={FILTERS} active={filter} onChange={setFilter} counts={counts} />

      <div className="paper-card">
        <div
          className={`${ROW_GRID} label px-3 py-2.5 border-b border-rule bg-paper-deep`}
        >
          <div>Lead</div>
          <div>Campaign</div>
          <div>Angle</div>
          <div>Status</div>
          <div>Last activity</div>
        </div>
        {filtered.length === 0 ? (
          <div className="py-14 text-center text-ink-muted text-sm">
            No threads match the current filter.
          </div>
        ) : (
          <div
            ref={scrollRef}
            className="overflow-auto"
            style={{ maxHeight: "calc(100vh - 22rem)" }}
          >
            <div
              className="relative"
              style={{ height: rowVirtualizer.getTotalSize() }}
            >
              {rowVirtualizer.getVirtualItems().map((vRow) => {
                const t = filtered[vRow.index];
                return (
                  <div
                    key={t.id}
                    data-index={vRow.index}
                    className={`${ROW_GRID} absolute inset-x-0 border-b border-rule px-3 py-3 hover:bg-paper-deep`}
                    style={{
                      transform: `translateY(${vRow.start}px)`,
                      height: ROW_HEIGHT,
                    }}
                  >
                    <div className="min-w-0 pr-3">
                      <Link to={`/threads/${t.id}`} className="group block">
                        <div className="text-sm text-ink group-hover:text-rust font-medium truncate">
                          {t.lead_name ?? "Unknown lead"}
                        </div>
                        <div className="text-[11px] text-ink-faint font-mono mt-0.5">
                          {formatPhone(t.lead_phone)}
                        </div>
                      </Link>
                    </div>
                    <div className="min-w-0 pr-3">
                      <Link
                        to={`/campaigns/${t.campaign_id}`}
                        className="text-sm text-ink-muted hover:text-ink truncate block"
                      >
                        {t.campaign_name}
                      </Link>
                    </div>
                    <div className="text-xs text-ink-muted truncate pr-3">
                      {t.angle ? (
                        <span className="text-ink">{t.angle}</span>
                      ) : (
                        <span className="italic text-ink-faint">No angle</span>
                      )}
                    </div>
                    <div className="pr-3">
                      <ThreadStatusBadge status={t.status} />
                    </div>
                    <div className="font-mono text-[11px] text-ink-muted">
                      {relTime(t.last_message_at)}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
