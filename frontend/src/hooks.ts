import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { useApp } from "./ctx";

/** Fetch with loading/error state; refetches when deps change or the app-wide tick is bumped. Ignores stale responses. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const { tick } = useApp();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const [local, setLocal] = useState(0);
  useEffect(() => {
    const id = ++seq.current;
    setLoading(true);
    fn().then((d) => { if (id === seq.current) { setData(d); setError(null); } })
      .catch((e) => { if (id === seq.current) setError(e instanceof ApiError || e instanceof Error ? e.message : "Failed to load"); })
      .finally(() => { if (id === seq.current) setLoading(false); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, local]);
  const reload = useCallback(() => setLocal((x) => x + 1), []);
  return { data, error, loading, reload, setData };
}

export function useAction() {
  const { toast, refresh } = useApp();
  const [busy, setBusy] = useState(false);
  const run = useCallback(async <T,>(fn: () => Promise<T>, ok?: string | ((r: T) => string)): Promise<T | undefined> => {
    setBusy(true);
    try {
      const r = await fn();
      if (ok) toast(typeof ok === "function" ? ok(r) : ok);
      refresh();
      return r;
    } catch (e) {
      toast(e instanceof Error ? e.message : "Action failed", true);
    } finally { setBusy(false); }
  }, [toast, refresh]);
  return { run, busy };
}

export function useDebounced<T>(v: T, ms = 300): T {
  const [d, setD] = useState(v);
  useEffect(() => { const t = setTimeout(() => setD(v), ms); return () => clearTimeout(t); }, [v, ms]);
  return d;
}
