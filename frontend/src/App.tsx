import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from '@/components/layout/AppShell';
import { Dashboard } from '@/routes/Dashboard';

// Dashboard is the index route and tiny — keep it eager so it paints
// without a Suspense flash. Everything else is split into its own chunk
// so first paint doesn't carry the whole app.
const Inbox = lazy(() => import('@/routes/Inbox').then((m) => ({ default: m.Inbox })));
const Threads = lazy(() =>
  import('@/routes/Threads').then((m) => ({ default: m.Threads })),
);
const ThreadDetail = lazy(() =>
  import('@/routes/ThreadDetail').then((m) => ({ default: m.ThreadDetail })),
);
const Campaigns = lazy(() =>
  import('@/routes/Campaigns').then((m) => ({ default: m.Campaigns })),
);
const CampaignDetail = lazy(() =>
  import('@/routes/CampaignDetail').then((m) => ({ default: m.CampaignDetail })),
);
const Leads = lazy(() => import('@/routes/Leads').then((m) => ({ default: m.Leads })));
const LeadDetail = lazy(() =>
  import('@/routes/LeadDetail').then((m) => ({ default: m.LeadDetail })),
);
const LeadsImport = lazy(() =>
  import('@/routes/LeadsImport').then((m) => ({ default: m.LeadsImport })),
);
const Logs = lazy(() => import('@/routes/Logs').then((m) => ({ default: m.Logs })));
const Scans = lazy(() => import('@/routes/Scans').then((m) => ({ default: m.Scans })));
const ScanDetail = lazy(() =>
  import('@/routes/ScanDetail').then((m) => ({ default: m.ScanDetail })),
);
const Settings = lazy(() =>
  import('@/routes/Settings').then((m) => ({ default: m.Settings })),
);
const NotFound = lazy(() =>
  import('@/routes/NotFound').then((m) => ({ default: m.NotFound })),
);

function RouteFallback() {
  return (
    <div className="min-h-[40vh] flex items-center justify-center text-ink-muted text-sm font-mono">
      <span className="inline-flex items-center gap-2">
        <span className="h-1.5 w-1.5 rounded-full bg-rust dot-pulse" />
        loading…
      </span>
    </div>
  );
}

/** App shell + routing. */
export default function App() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
        <Route path="/setup" element={<Navigate to="/settings" replace />} />
        <Route element={<AppShell />}>
          <Route index element={<Dashboard />} />
          <Route path="/inbox" element={<Inbox />} />
          <Route path="/threads" element={<Threads />} />
          <Route path="/threads/:id" element={<ThreadDetail />} />
          <Route path="/campaigns" element={<Campaigns />} />
          <Route path="/campaigns/:id" element={<CampaignDetail />} />
          <Route path="/leads" element={<Leads />} />
          <Route path="/leads/import" element={<LeadsImport />} />
          <Route path="/leads/:id" element={<LeadDetail />} />
          <Route path="/scans" element={<Scans />} />
          <Route path="/scans/:leadId" element={<ScanDetail />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
