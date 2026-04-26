import { lazy, Suspense } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { AppShell } from '@/components/layout/AppShell';
import { Dashboard } from '@/routes/Dashboard';
import { api } from '@/lib/api';

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
const Settings = lazy(() =>
  import('@/routes/Settings').then((m) => ({ default: m.Settings })),
);
const Setup = lazy(() => import('@/routes/Setup').then((m) => ({ default: m.Setup })));
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

/**
 * App shell + routing.
 *
 * `SetupGate` polls `GET /api/setup/required` once on mount. If the
 * backend has no workspace row yet, we force the user to `/setup` before
 * showing anything else. Once the wizard writes the workspace row, the
 * query is invalidated and the gate lets the normal app through.
 */
export default function App() {
  return (
    <SetupGate>
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          <Route path="/setup" element={<Setup />} />
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
            <Route path="/logs" element={<Logs />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </Suspense>
    </SetupGate>
  );
}

/**
 * Gate the app on workspace setup. We derive the redirect target during
 * render rather than in an effect — react-router's `<Navigate>` handles
 * the navigation safely without a double render and avoids a redundant
 * `useEffect` + `useNavigate` pair.
 */
function SetupGate({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const onSetupRoute = location.pathname.startsWith('/setup');

  const { data, isLoading, isError } = useQuery({
    queryKey: ['setup', 'required'],
    queryFn: () => api.getSetupStatus(),
    staleTime: 30_000,
    retry: 1,
  });

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-paper text-ink-muted text-sm font-mono">
        <span className="inline-flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-full bg-rust dot-pulse" />
          booting…
        </span>
      </div>
    );
  }

  if (isError) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-paper text-ink p-8">
        <div className="paper-card px-6 py-5 max-w-md">
          <div className="text-sm font-medium">Can't reach the AutoSDR server.</div>
          <div className="mt-2 text-xs text-ink-muted">
            Check that the backend is running and refresh the page.
          </div>
        </div>
      </div>
    );
  }

  if (data?.setup_required && !onSetupRoute) {
    return <Navigate to="/setup" replace />;
  }
  if (data && !data.setup_required && onSetupRoute) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}
