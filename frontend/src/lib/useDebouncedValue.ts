import { useEffect, useState } from 'react';

/**
 * Debounce a rapidly-changing value. Returns a stable copy that
 * trails the source by `delayMs` of quiet time — useful for
 * driving a TanStack Query key off a search-input draft so each
 * keystroke doesn't fire a fresh round-trip.
 *
 * The single internal `useEffect` is the canonical "subscribe to
 * a fast source" pattern (timers + cleanup on dependency change);
 * keeping it behind this hook is how the rest of the app stays
 * `useEffect`-free at the call site.
 */
export function useDebouncedValue<T>(value: T, delayMs = 200): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(id);
  }, [value, delayMs]);

  return debounced;
}
