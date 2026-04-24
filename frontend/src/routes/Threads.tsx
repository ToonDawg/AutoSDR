import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useMemo, useState } from "react";
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
        <table className="t-table">
          <colgroup>
            <col style={{ width: "28%" }} />
            <col style={{ width: "24%" }} />
            <col style={{ width: "22%" }} />
            <col style={{ width: "12%" }} />
            <col style={{ width: "14%" }} />
          </colgroup>
          <thead>
            <tr>
              <th>Lead</th>
              <th>Campaign</th>
              <th>Angle</th>
              <th>Status</th>
              <th>Last activity</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((t) => (
              <tr key={t.id}>
                <td>
                  <Link to={`/threads/${t.id}`} className="group block">
                    <div className="text-sm text-ink group-hover:text-rust font-medium">
                      {t.lead_name ?? "Unknown lead"}
                    </div>
                    <div className="text-[11px] text-ink-faint font-mono mt-0.5">
                      {formatPhone(t.lead_phone)}
                    </div>
                  </Link>
                </td>
                <td>
                  <Link
                    to={`/campaigns/${t.campaign_id}`}
                    className="text-sm text-ink-muted hover:text-ink"
                  >
                    {t.campaign_name}
                  </Link>
                </td>
                <td className="text-xs text-ink-muted">
                  {t.angle ? (
                    <span className="text-ink">{t.angle}</span>
                  ) : (
                    <span className="italic text-ink-faint">No angle</span>
                  )}
                </td>
                <td>
                  <ThreadStatusBadge status={t.status} />
                </td>
                <td className="font-mono text-[11px] text-ink-muted">
                  {relTime(t.last_message_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {filtered.length === 0 && (
          <div className="py-14 text-center text-ink-muted text-sm">
            No threads match the current filter.
          </div>
        )}
      </div>
    </div>
  );
}
