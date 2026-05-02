import { useCallback, useEffect, useState } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';
import { MobileDrawer } from './MobileDrawer';

/**
 * Top-level chrome for every authenticated route.
 *
 * Layout shape:
 * - `≥md`: persistent left sidebar + sticky topbar + main content.
 * - `<md`: sidebar is hidden; the same nav lives inside `MobileDrawer`,
 *   toggled by the hamburger button in `TopBar`. The topbar (with
 *   killswitch) stays sticky so the operator can pause AutoSDR from
 *   anywhere — and the killswitch banner is always visible.
 */
export function AppShell() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const closeDrawer = useCallback(() => setDrawerOpen(false), []);
  const openDrawer = useCallback(() => setDrawerOpen(true), []);

  const location = useLocation();
  useEffect(() => {
    if (drawerOpen) setDrawerOpen(false);
  }, [location.pathname]);

  return (
    <div className="flex min-h-screen bg-paper text-ink">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0">
        <TopBar onOpenMenu={openDrawer} />
        <main className="flex-1 min-w-0">
          <Outlet />
        </main>
      </div>
      <MobileDrawer open={drawerOpen} onClose={closeDrawer} />
    </div>
  );
}
