import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { CheckCircle2, FileText, Upload } from 'lucide-react';
import { api } from '@/lib/api';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { BackLink } from '@/components/ui/BackLink';
import { PageHeader } from '@/components/ui/PageHeader';
import { CONTACT_TYPE_LABEL, formatPhone, formatSkipReason } from '@/lib/format';
import type { ImportPreview } from '@/lib/types';
import { cn } from '@/lib/utils';

/**
 * Two-step lead importer. Drop a CSV/JSON file, we preview what will
 * be imported versus skipped (and why), then the operator commits.
 * Commit reuses the same file — the server normalises and dedupes.
 */
export function LeadsImport() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [dragging, setDragging] = useState(false);

  const previewMut = useMutation({
    mutationFn: (f: File) => api.previewImport(f),
    onSuccess: setPreview,
  });

  const commitMut = useMutation({
    mutationFn: (f: File) => api.commitImport(f),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['leads'] });
      navigate('/leads', { replace: true });
    },
  });

  const handleFile = (f: File | null) => {
    setFile(f);
    setPreview(null);
    if (f) previewMut.mutate(f);
  };

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
                onClick={() => commitMut.mutate(file)}
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
