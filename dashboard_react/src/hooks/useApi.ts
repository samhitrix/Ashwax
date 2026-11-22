import { useState, useEffect, useRef } from "react";

// Last successful response per URL, kept for the page's lifetime. Tabs unmount on
// every switch, so without this each revisit started at null/"Loading…" and waited
// a full round-trip for data fetched seconds earlier. Now a revisit renders the last
// value immediately and the normal fetch refreshes it (stale-while-revalidate).
const _cache = new Map<string, unknown>();

export function cachedApi<T>(url: string): T | null {
  return (_cache.get(url) as T | undefined) ?? null;
}

export function storeApi(url: string, data: unknown): void {
  _cache.set(url, data);
}

export function useApi<T>(
  url: string,
  interval = 5000
): { data: T | null; loading: boolean; error: string | null } {
  const [data, setData] = useState<T | null>(() => cachedApi<T>(url));
  const [loading, setLoading] = useState(() => !_cache.has(url));
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let cancelled = false;
    // url changed on a mounted component: show that url's cached value, not the old one.
    setData(cachedApi<T>(url));
    setLoading(!_cache.has(url));

    const fetch_ = async () => {
      try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = (await res.json()) as T;
        _cache.set(url, json);
        if (!cancelled) { setData(json); setError(null); }
      } catch (e) {
        console.error(`[ashwax] fetch failed: ${url}`, e);
        if (!cancelled) setError(e instanceof Error ? e.message : "Unknown error");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetch_();
    timerRef.current = setInterval(fetch_, interval);
    return () => {
      cancelled = true;
      if (timerRef.current !== null) clearInterval(timerRef.current);
    };
  }, [url, interval]);

  return { data, loading, error };
}
