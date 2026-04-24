import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import type { Workspace } from '@/lib/types';

interface Options<TState extends object> {
  /** Key that identifies the underlying server state; when it changes,
   *  the form resets to a fresh snapshot so we don't keep stale edits
   *  after a save succeeds elsewhere (e.g. another tab). Usually the
   *  workspace's `updated_at`. */
  resetKey: string;
  /** Derive the initial form state from the current server value. Called
   *  on mount and again whenever `resetKey` changes. */
  derive: () => TState;
  /** Called when the user hits Save. Must return the updated workspace;
   *  the hook cascades it into the ``['workspace']`` query so the page
   *  rerenders with the new values. */
  save: (state: TState) => Promise<Workspace>;
}

/**
 * Settings-style form hook.
 *
 * Each settings card has the same shape: read the workspace, spread
 * it across a dozen fields, track dirty state, PATCH on save. This
 * hook bundles that plumbing so the cards can be pure render.
 *
 * It avoids the "setState inside useEffect" pattern the original code
 * used (flagged by react-hooks/set-state-in-effect) by resetting state
 * inline when `resetKey` changes — React's sanctioned escape hatch
 * for "reset all state when prop changes" without remounting.
 */
export function usePatchForm<TState extends object>({
  resetKey,
  derive,
  save,
}: Options<TState>) {
  const qc = useQueryClient();
  const [initial, setInitial] = useState(derive);
  const [state, setState] = useState(initial);
  const [seenKey, setSeenKey] = useState(resetKey);

  if (seenKey !== resetKey) {
    const fresh = derive();
    setSeenKey(resetKey);
    setInitial(fresh);
    setState(fresh);
  }

  const mutation = useMutation({
    mutationFn: () => save(state),
    onSuccess: (workspace) => {
      qc.setQueryData(['workspace'], workspace);
    },
  });

  const dirty = !shallowEqual(state, initial);

  return {
    state,
    set<K extends keyof TState>(key: K, value: TState[K]) {
      setState((prev) => ({ ...prev, [key]: value }));
    },
    patch(partial: Partial<TState>) {
      setState((prev) => ({ ...prev, ...partial }));
    },
    dirty,
    save: () => mutation.mutate(),
    saving: mutation.isPending,
  };
}

function shallowEqual<T extends object>(a: T, b: T): boolean {
  if (a === b) return true;
  const aKeys = Object.keys(a) as Array<keyof T>;
  const bKeys = Object.keys(b) as Array<keyof T>;
  if (aKeys.length !== bKeys.length) return false;
  for (const key of aKeys) {
    if (!Object.is(a[key], b[key])) return false;
  }
  return true;
}
