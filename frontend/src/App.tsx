import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { AppShell } from '@/components/layout/AppShell';
import { Dashboard } from '@/routes/Dashboard';
import { Inbox } from '@/routes/Inbox';
import { Threads } from '@/routes/Threads';
import { ThreadDetail } from '@/routes/ThreadDetail';
import { Campaigns } from '@/routes/Campaigns';
import { CampaignDetail } from '@/routes/CampaignDetail';
import { Leads } from '@/routes/Leads';
import { LeadsImport } from '@/routes/LeadsImport';
import { Logs } from '@/routes/Logs';
import { Settings } from '@/routes/Settings';
import { Setup } from '@/routes/Setup';
import { NotFound } from '@/routes/NotFound';
import { api } from '@/lib/api';

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
          <Route path="/logs" element={<Logs />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </SetupGate>
  );
}

function SetupGate({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const onSetupRoute = location.pathname.startsWith('/setup');

  const { data, isLoading, isError } = useQuery({
    queryKey: ['setup', 'required'],
    queryFn: () => api.getSetupStatus(),
    staleTime: 30_000,
    retry: 1,
  });

  useEffect(() => {
    if (data?.setup_required && !onSetupRoute) {
      navigate('/setup', { replace: true });
    }
    if (data && !data.setup_required && onSetupRoute) {
      navigate('/', { replace: true });
    }
  }, [data, onSetupRoute, navigate]);

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

  return <>{children}</>;
}
