import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { CheckCircle2, FileText, Upload } from 'lucide-react';
import { api } from '@/lib/api';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { BackLink } from '@/components/ui/BackLink';
import { PageHeader } from '@/components/ui/PageHeader';
import { CONTACT_TYPE_LABEL, formatPhone, formatSkipReason } from '@/lib/format';
import type {
  CoreFieldName,
  ImportPreview,
  ImportPreviewColumn,
  MappingConfig,
} from '@/lib/types';
import { cn } from '@/lib/utils';

/**
 * Two-step lead importer. Drop a CSV/JSON file, we preview what will
 * be imported versus skipped, and we render a column-mapping table so
 * the operator can approve or override every detected column before
 * commit. Commit re-uploads the same file plus the operator's
 * `mapping_config` — the server normalises and dedupes.
 *
 * Mapping table semantics:
 * - Per-column choice: a core field, "raw_data only", or "drop entirely".
 * - "Drop entirely" filters keys out of *this* import only — existing
 *   ``raw_data`` for prior rows is left alone (commit-only — see ticket
 *   0004 council resolution `drop-semantic`).
 * - Choices preselect to the server's suggestion. Operator overrides
 *   never get silently re-suggested.
 */
export function LeadsImport() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [dragging, setDragging] = useState(false);
  const [choices, setChoices] = useState<Record<string, MappingChoice>>({});

  const previewMut = useMutation({
    mutationFn: (f: File) => api.previewImport(f),
    onSuccess: (p) => {
      setPreview(p);
      setChoices(initialChoicesFromPreview(p));
    },
  });

  const commitMut = useMutation({
    mutationFn: ({ f, cfg }: { f: File; cfg: MappingConfig | null }) =>
      api.commitImport(f, cfg),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['leads'] });
      navigate('/leads', { replace: true });
    },
  });

  const handleFile = (f: File | null) => {
    setFile(f);
    setPreview(null);
    setChoices({});
    if (f) previewMut.mutate(f);
  };

  const setChoice = (col: string, choice: MappingChoice) =>
    setChoices((prev) => ({ ...prev, [col]: choice }));

  const dropAllUnsuggested = () => {
    if (!preview) return;
    setChoices((prev) => {
      const next = { ...prev };
      for (const col of preview.columns) {
        if (col.suggested_target === null || col.suggestion_confidence === 'none') {
          next[col.name] = 'drop';
        }
      }
      return next;
    });
  };

  const mappingConfig = useMemo(
    () => (preview ? buildMappingConfig(preview.columns, choices) : null),
    [preview, choices],
  );

  return (
    <div className="page-narrow gap-6">
      <BackLink to="/leads">All leads</BackLink>

      <PageHeader
        title={file ? 'Check the import before committing' : 'Import leads'}
        description="CSV, JSON, NDJSON. Phones normalise to E.164, mobiles get queued, everything else is kept for reference."
      />

      {!file && (
        <>
          <label
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              const f = e.dataTransfer.files[0];
              if (f) handleFile(f);
            }}
            className={cn(
              'flex flex-col items-center justify-center border-2 border-dashed transition-colors cursor-pointer py-16 px-8 text-center',
              dragging
                ? 'border-rust bg-rust-soft/30'
                : 'border-rule-strong hover:border-ink bg-paper-deep/50',
            )}
          >
            <Upload className="h-10 w-10 mb-4 text-ink-muted" strokeWidth={1} />
            <div className="text-base font-medium mb-1">Drop a file or click to browse</div>
            <div className="text-xs text-ink-muted max-w-md">
              Accepts <span className="font-mono">.csv</span>,{' '}
              <span className="font-mono">.json</span>, or{' '}
              <span className="font-mono">.ndjson</span>. Core columns (name, category, address,
              website, phone) auto-detected.
            </div>
            <input
              type="file"
              accept=".csv,.json,.ndjson"
              className="hidden"
              onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
            />
          </label>

          <div className="grid grid-cols-3 gap-4 text-sm">
            <Rule
              title="E.164 phones"
              body="All phones parsed to international format. Unparseable numbers still import but get skipped."
            />
            <Rule
              title="Mobile only"
              body="Landlines and toll-free numbers import for your records but the scheduler never texts them."
            />
            <Rule
              title="Re-import is safe"
              body="Re-uploading merges raw data and fills blanks without creating duplicates."
            />
          </div>
        </>
      )}

      {file && previewMut.isPending && (
        <div className="py-16 text-center text-ink-muted text-sm">Parsing {file.name}…</div>
      )}

      {file && previewMut.isError && (
        <div className="paper-card px-5 py-4 border-oxblood text-oxblood text-sm">
          Preview failed: {String(previewMut.error)}
          <button
            className="ml-2 underline"
            onClick={() => handleFile(null)}
          >
            Try another file
          </button>
        </div>
      )}

      {file && preview && (
        <div className="flex flex-col gap-6">
          <div className="flex items-center justify-between border border-rule bg-paper-deep p-4">
            <div className="flex items-center gap-3">
              <FileText className="h-5 w-5 text-ink-muted" strokeWidth={1.5} />
              <div>
                <div className="text-sm font-medium text-ink">{file.name}</div>
                <div className="text-[11px] font-mono text-ink-muted uppercase tracking-[0.14em]">
                  {preview.file_type} · {preview.total_rows} rows
                </div>
              </div>
            </div>
            <button
              onClick={() => handleFile(null)}
              className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted hover:text-ink"
            >
              Change file
            </button>
          </div>

          <SocialWebsiteCallout hosts={preview.social_website_hosts} />

          <div className="grid grid-cols-2 gap-4">
            <div className="border border-forest bg-forest-soft p-4">
              <div className="label text-forest mb-1">Will import</div>
              <div className="text-3xl font-medium text-forest tabular-nums">
                {preview.would_import}
              </div>
              <div className="mt-1 text-xs text-forest/80">
                Mobile numbers — scheduler-ready
              </div>
            </div>
            <div className="border border-rule-strong p-4">
              <div className="label mb-1">Will skip</div>
              <div className="text-3xl font-medium text-ink-muted tabular-nums">
                {preview.would_skip.reduce((a, b) => a + b.count, 0)}
              </div>
              <div className="mt-2 flex flex-col gap-1 text-xs">
                {preview.would_skip.map((s) => (
                  <div key={s.reason} className="flex items-center justify-between gap-3">
                    <span className="text-ink-muted truncate">
                      {formatSkipReason(s.reason)}
                    </span>
                    <span className="font-mono text-ink tabular-nums shrink-0">
                      {s.count}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {preview.columns.length > 0 && (
            <ColumnMappingTable
              columns={preview.columns}
              choices={choices}
              onChange={setChoice}
              onDropAllUnsuggested={dropAllUnsuggested}
            />
          )}

          <div>
            <div className="flex items-center justify-between pb-2 border-b border-rule mb-3">
              <h2 className="text-sm font-medium">Sample</h2>
              <span className="text-xs font-mono text-ink-muted">
                {preview.sample.length} of {preview.total_rows}
              </span>
            </div>
            <div className="paper-card">
              <table className="t-table">
                <thead>
                  <tr>
                    <th style={{ width: '30%' }}>Name</th>
                    <th style={{ width: '22%' }}>Raw phone</th>
                    <th style={{ width: '22%' }}>Normalised</th>
                    <th style={{ width: '12%' }}>Type</th>
                    <th style={{ width: '14%' }}>Verdict</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.sample.map((s, i) => (
                    <tr key={i} className={cn(s.skip_reason && 'stripes')}>
                      <td className="text-sm text-ink">{s.name}</td>
                      <td className="font-mono text-xs text-ink-muted">{s.phone}</td>
                      <td className="font-mono text-xs text-ink">
                        {formatPhone(s.normalised_phone)}
                      </td>
                      <td className="font-mono text-[10px] tracking-[0.14em] uppercase text-ink-muted">
                        {CONTACT_TYPE_LABEL[s.contact_type]}
                      </td>
                      <td>
                        {s.skip_reason ? (
                          <Badge tone="oxblood">skip</Badge>
                        ) : (
                          <Badge tone="forest">import</Badge>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="flex items-center justify-between pt-4 border-t border-rule">
            <div className="text-sm text-ink-muted">
              Ready to commit <span className="text-ink">{preview.would_import}</span> leads.
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="ghost"
                onClick={() => handleFile(null)}
                disabled={commitMut.isPending}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                iconLeft={<CheckCircle2 className="h-4 w-4" strokeWidth={1.5} />}
                onClick={() => commitMut.mutate({ f: file, cfg: mappingConfig })}
                disabled={commitMut.isPending}
              >
                {commitMut.isPending ? 'Committing…' : 'Commit import'}
              </Button>
            </div>
          </div>

          {commitMut.isError && (
            <div className="text-xs text-oxblood">
              Commit failed: {String(commitMut.error)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Rule({ title, body }: { title: string; body: string }) {
  return (
    <div className="border-t border-rule-strong pt-2 pr-3">
      <div className="text-sm font-medium text-ink mb-1">{title}</div>
      <p className="text-xs text-ink-muted leading-relaxed">{body}</p>
    </div>
  );
}

// ---------- mapping table ----------

/**
 * What the operator picked for one source column. The wire format
 * splits these into ``mapping`` / ``include_in_raw_only`` /
 * ``drop_from_raw``; this UI-side enum keeps the radio model simple.
 */
/**
 * Pre-commit callout: how many of the upload's rows have a social
 * profile in the ``website`` column. Renders nothing when no social
 * URLs were detected. Operators see this *before* committing so
 * they can decide whether to clean the data or just let those rows
 * land in the priority tier. Ticket 0014.
 */

const SOCIAL_PLATFORM_LABEL: Record<string, string> = {
  facebook: 'Facebook',
  instagram: 'Instagram',
  linkedin: 'LinkedIn',
  twitter: 'Twitter',
  x: 'X',
  tiktok: 'TikTok',
  youtube: 'YouTube',
};

function SocialWebsiteCallout({
  hosts,
}: {
  hosts: Record<string, number> | undefined;
}) {
  if (!hosts) return null;
  const entries = Object.entries(hosts).filter(([, count]) => count > 0);
  if (entries.length === 0) return null;
  // Stable platform order so the callout doesn't reflow if the
  // server renders the dict in a different key order between
  // requests.
  entries.sort((a, b) => a[0].localeCompare(b[0]));
  return (
    <div className="border border-mustard/40 bg-mustard-soft/50 px-4 py-3 text-xs text-ink">
      <div className="label text-mustard mb-1">Priority on import</div>
      <div className="flex flex-col gap-1">
        {entries.map(([platform, count]) => (
          <div key={platform} className="flex items-center justify-between gap-3">
            <span className="text-ink">
              {count} lead{count === 1 ? '' : 's'} have{' '}
              {SOCIAL_PLATFORM_LABEL[platform] ?? platform} as their website
            </span>
            <span className="font-mono text-ink-muted tabular-nums shrink-0">
              {count}
            </span>
          </div>
        ))}
      </div>
      <div className="mt-2 text-[11px] text-ink-muted">
        These leads will be flagged as priority — sent before normal-tier leads
        in any campaign you assign them to.
      </div>
    </div>
  );
}

type MappingChoice = CoreFieldName | 'raw_only' | 'drop';

const CORE_FIELD_OPTIONS: { value: CoreFieldName; label: string }[] = [
  { value: 'name', label: 'name' },
  { value: 'category', label: 'category' },
  { value: 'address', label: 'address' },
  { value: 'website', label: 'website' },
  { value: 'phone', label: 'phone' },
];

const CONFIDENCE_TONE: Record<
  ImportPreviewColumn['suggestion_confidence'],
  'forest' | 'mustard' | 'neutral'
> = {
  high: 'forest',
  medium: 'mustard',
  low: 'neutral',
  none: 'neutral',
};

function initialChoicesFromPreview(p: ImportPreview): Record<string, MappingChoice> {
  const next: Record<string, MappingChoice> = {};
  for (const col of p.columns) {
    next[col.name] = suggestionToChoice(col);
  }
  return next;
}

function suggestionToChoice(col: ImportPreviewColumn): MappingChoice {
  if (col.suggested_target === null) return 'raw_only';
  if (col.suggested_target === 'raw_only') return 'raw_only';
  return col.suggested_target;
}

/**
 * Convert the operator's per-column picks into the wire format.
 * Returns ``null`` when the operator hasn't deviated from a default
 * pure pass-through (no mappings, no drops, no raw-only forces) so we
 * keep the wire payload empty for backward-compatible clients.
 */
function buildMappingConfig(
  columns: ImportPreviewColumn[],
  choices: Record<string, MappingChoice>,
): MappingConfig | null {
  const mapping: Partial<Record<CoreFieldName, string>> = {};
  const drop: string[] = [];
  const rawOnly: string[] = [];

  for (const col of columns) {
    const choice = choices[col.name];
    if (choice === 'drop') {
      drop.push(col.name);
    } else if (choice === 'raw_only') {
      rawOnly.push(col.name);
    } else if (choice) {
      mapping[choice] = col.name;
    }
  }

  if (
    Object.keys(mapping).length === 0 &&
    drop.length === 0 &&
    rawOnly.length === 0
  ) {
    return null;
  }
  return { mapping, drop_from_raw: drop, include_in_raw_only: rawOnly };
}

interface ColumnMappingTableProps {
  columns: ImportPreviewColumn[];
  choices: Record<string, MappingChoice>;
  onChange: (col: string, choice: MappingChoice) => void;
  onDropAllUnsuggested: () => void;
}

function ColumnMappingTable({
  columns,
  choices,
  onChange,
  onDropAllUnsuggested,
}: ColumnMappingTableProps) {
  return (
    <div>
      <div className="flex items-center justify-between pb-2 border-b border-rule mb-3">
        <h2 className="text-sm font-medium">Column mapping</h2>
        <button
          type="button"
          onClick={onDropAllUnsuggested}
          className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted hover:text-ink"
        >
          Drop all unsuggested
        </button>
      </div>
      <p className="text-xs text-ink-muted mb-3 leading-relaxed">
        One row per column we found in your file. Map each to a core field, keep it
        in <span className="font-mono">raw_data</span> only, or drop it entirely.{' '}
        <span className="text-ink">
          Dropping applies to this import only — existing records keep what they have.
        </span>
      </p>
      <div className="paper-card">
        <table className="t-table">
          <thead>
            <tr>
              <th style={{ width: '22%' }}>Column</th>
              <th style={{ width: '38%' }}>Sample values</th>
              <th style={{ width: '14%' }}>Suggestion</th>
              <th style={{ width: '26%' }}>Map to</th>
            </tr>
          </thead>
          <tbody>
            {columns.map((col) => (
              <tr key={col.name}>
                <td className="font-mono text-xs text-ink">{col.name}</td>
                <td>
                  <div
                    className="text-xs text-ink-muted truncate max-w-md"
                    title={formatSampleTitle(col.sample_values)}
                  >
                    {formatSampleInline(col.sample_values)}
                  </div>
                </td>
                <td>
                  {col.suggestion_confidence === 'none' ? (
                    <Badge tone="neutral">no guess</Badge>
                  ) : (
                    <span title={col.suggestion_reason}>
                      <Badge tone={CONFIDENCE_TONE[col.suggestion_confidence]}>
                        {col.suggestion_confidence}
                      </Badge>
                    </span>
                  )}
                </td>
                <td>
                  <select
                    className="w-full bg-paper border border-rule-strong px-2 py-1 text-xs font-mono text-ink focus:outline-none focus:border-ink"
                    value={choices[col.name] ?? 'raw_only'}
                    onChange={(e) =>
                      onChange(col.name, e.target.value as MappingChoice)
                    }
                  >
                    {CORE_FIELD_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        → {opt.label}
                      </option>
                    ))}
                    <option value="raw_only">Keep in raw_data only</option>
                    <option value="drop">Drop entirely</option>
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function formatSampleInline(values: unknown[]): string {
  if (!values.length) return '—';
  return values
    .slice(0, 3)
    .map((v) => formatSampleValue(v))
    .join(' · ');
}

function formatSampleTitle(values: unknown[]): string {
  return values.map((v) => formatSampleValue(v)).join('\n');
}

function formatSampleValue(v: unknown): string {
  if (v === null || v === undefined) return '∅';
  if (typeof v === 'string') return v.length > 60 ? `${v.slice(0, 60)}…` : v;
  try {
    const s = JSON.stringify(v);
    return s.length > 60 ? `${s.slice(0, 60)}…` : s;
  } catch {
    return String(v);
  }
}
