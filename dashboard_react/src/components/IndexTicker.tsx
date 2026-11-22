import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { cn } from '../utils/cn';

const API_BASE = '';

// Slow, readable scroll speed in pixels per second. Loop duration = content width / this.
const SCROLL_PX_PER_SEC = 100;
const INDIA_POLL_MS = 3000;
const GLOBAL_POLL_MS = 60000;

interface IndiaIndex {
  name: string;
  sym: string;
  decimals?: number;
}

// NIFTY 50 and INDIA VIX stay first so they lead every loop.
const INDIA_INDICES: IndiaIndex[] = [
  { name: 'NIFTY 50', sym: 'NSE:NIFTY 50' },
  { name: 'INDIA VIX', sym: 'NSE:INDIA VIX' },
  { name: 'SENSEX', sym: 'BSE:SENSEX' },
  { name: 'BANK NIFTY', sym: 'NSE:NIFTY BANK' },
  { name: 'FIN NIFTY', sym: 'NSE:NIFTY FIN SERVICE' },
  { name: 'NIFTY MIDCAP 100', sym: 'NSE:NIFTY MIDCAP 100' },
  { name: 'NIFTY IT', sym: 'NSE:NIFTY IT' },
  { name: 'NIFTY NEXT 50', sym: 'NSE:NIFTY NEXT 50' },
];

interface TickerRow {
  name: string;
  price: number;
  changePct: number | null; // null = no previous close available, hide the badge
  open: boolean;
  delayed: boolean;
}

/** NSE/BSE cash session, Mon-Fri 09:15-15:30 IST. Holidays are not modelled (dot only). */
function indiaMarketOpen(now = new Date()): boolean {
  const ist = new Date(now.getTime() + (now.getTimezoneOffset() + 330) * 60000);
  const day = ist.getDay();
  const mins = ist.getHours() * 60 + ist.getMinutes();
  return day >= 1 && day <= 5 && mins >= 9 * 60 + 15 && mins < 15 * 60 + 30;
}

// Last values are kept in localStorage so a page refresh paints the strip instantly
// instead of waiting on the first (sometimes slow) API responses.
const CACHE_KEY = 'ashwax.indexTicker.v1';

function readCache(kind: 'india' | 'world'): TickerRow[] {
  try {
    const raw = JSON.parse(localStorage.getItem(`${CACHE_KEY}.${kind}`) ?? '[]');
    if (!Array.isArray(raw)) return [];
    const open = indiaMarketOpen();
    // Indian open/closed is recomputed now; the cached flag could be hours old.
    return raw.map((r: TickerRow) => (kind === 'india' ? { ...r, open } : r));
  } catch {
    return [];
  }
}

function writeCache(kind: 'india' | 'world', rows: TickerRow[]): void {
  try {
    localStorage.setItem(`${CACHE_KEY}.${kind}`, JSON.stringify(rows));
  } catch {
    /* storage unavailable: skip caching */
  }
}

function Item({ row }: { row: TickerRow }) {
  const prev = useRef(row.price);
  const [flash, setFlash] = useState<'up' | 'down' | null>(null);

  useEffect(() => {
    if (row.price === prev.current) return;
    setFlash(row.price > prev.current ? 'up' : 'down');
    prev.current = row.price;
    const t = setTimeout(() => setFlash(null), 700);
    return () => clearTimeout(t);
  }, [row.price]);

  const up = (row.changePct ?? 0) >= 0;
  return (
    <div className="flex items-center gap-2.5 px-5 py-2 whitespace-nowrap border-r border-gray-700">
      <span className="text-xs text-gray-400">
        <span
          className={cn(
            'inline-block w-1.5 h-1.5 rounded-full mr-1.5 align-middle',
            row.open ? 'bg-green-400' : 'bg-gray-500',
          )}
          title={row.open ? 'Market open' : 'Market closed'}
        />
        {row.name}
        {row.delayed && <span className="ml-1 text-[10px] opacity-60">delayed</span>}
      </span>
      <span
        className={cn(
          'font-mono font-semibold text-sm px-1 rounded transition-colors duration-700',
          flash === 'up' && 'bg-green-500/20',
          flash === 'down' && 'bg-red-500/20',
        )}
      >
        {row.price.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
      </span>
      {row.changePct !== null && (
        <span
          className={cn(
            'px-2 py-0.5 rounded text-xs font-medium',
            up ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400',
          )}
        >
          {up ? '+' : ''}
          {row.changePct.toFixed(2)}%
        </span>
      )}
    </div>
  );
}

async function fetchJson(url: string, ms = 8000): Promise<unknown> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(url, { signal: ctl.signal });
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

/** Slowly scrolling strip of Indian and delayed global index levels for the header. */
export function IndexTicker() {
  const [india, setIndia] = useState<TickerRow[]>(() => readCache('india'));
  const [world, setWorld] = useState<TickerRow[]>(() => readCache('world'));
  const setRef = useRef<HTMLDivElement>(null);
  const [durationS, setDurationS] = useState(180);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const syms = INDIA_INDICES.map((i) => i.sym).join(',');
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const data: any = await fetchJson(`${API_BASE}/api/ltp?symbols=${encodeURIComponent(syms)}`);
        if (!alive || !data || data.error) return;
        const open = indiaMarketOpen();
        const rows: TickerRow[] = [];
        for (const idx of INDIA_INDICES) {
          const e = data[idx.sym];
          const price = Number(e?.last_price ?? e?.ltp ?? 0);
          if (!(price > 0)) continue;
          const close = Number(e?.ohlc?.close ?? 0);
          rows.push({
            name: idx.name,
            price,
            changePct: close > 0 ? ((price - close) / close) * 100 : null,
            open,
            delayed: false,
          });
        }
        // Keep the last good strip if a poll returns nothing usable.
        if (rows.length) {
          setIndia(rows);
          writeCache('india', rows);
        }
      } catch {
        /* keep previous values */
      }
    };
    load();
    const id = setInterval(load, INDIA_POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const data: any = await fetchJson(`${API_BASE}/api/global_indices`);
        if (!alive || !Array.isArray(data?.rows) || !data.rows.length) return;
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const rows: TickerRow[] = data.rows.map((r: any) => ({
          name: String(r.name),
          price: Number(r.price),
          changePct: Number(r.change_pct),
          open: Boolean(r.open),
          delayed: true,
        }));
        setWorld(rows);
        writeCache('world', rows);
      } catch {
        /* keep previous values */
      }
    };
    load();
    const id = setInterval(load, GLOBAL_POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const rows = [...india, ...world];
  const rowCount = rows.length;

  // Constant pixel speed: duration follows the width of one copy of the strip.
  useLayoutEffect(() => {
    const w = setRef.current?.offsetWidth ?? 0;
    if (w > 0) setDurationS(Math.round(w / SCROLL_PX_PER_SEC));
  }, [rowCount]);

  if (!rowCount) {
    return <div className="flex-1 min-w-0 px-4 py-2 text-xs text-gray-400 bg-[#0d1117] rounded-lg">Loading indices…</div>;
  }

  return (
    <div
      className="index-ticker flex-1 min-w-0 overflow-hidden bg-[#0d1117] rounded-lg"
      style={{
        WebkitMaskImage: 'linear-gradient(90deg, transparent, #000 36px, #000 calc(100% - 36px), transparent)',
        maskImage: 'linear-gradient(90deg, transparent, #000 36px, #000 calc(100% - 36px), transparent)',
      }}
    >
      <div className="index-ticker-track" style={{ animationDuration: `${durationS}s` }}>
        <div ref={setRef} className="flex">
          {rows.map((r) => (
            <Item key={r.name} row={r} />
          ))}
        </div>
        <div className="flex" aria-hidden="true">
          {rows.map((r) => (
            <Item key={r.name} row={r} />
          ))}
        </div>
      </div>
    </div>
  );
}
