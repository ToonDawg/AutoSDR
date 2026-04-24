import { useCallback, useEffect, useState } from 'react';

type Theme = 'dark' | 'light';

/**
 * Read `html.dark` / `html.light` as the source of truth — the inline
 * bootstrap in `index.html` has already set the right class before first
 * paint, so we don't have to re-derive from localStorage/matchMedia here.
 */
function readTheme(): Theme {
  if (typeof document === 'undefined') return 'light';
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light';
}

/**
 * Theme state for any component that needs to read or flip the paper/ink
 * palette. Sync and cheap: the class on `<html>` is the canonical flag
 * (set by the head script before React boots), and we mirror it to
 * localStorage so reloads stick.
 */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(readTheme);

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle('dark', theme === 'dark');
    root.classList.toggle('light', theme === 'light');
    try {
      localStorage.setItem('theme', theme);
    } catch {
      /* private mode — ignore */
    }
  }, [theme]);

  const toggle = useCallback(() => setTheme((t) => (t === 'dark' ? 'light' : 'dark')), []);

  return { theme, isDark: theme === 'dark', setTheme, toggle } as const;
}
