import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Download, RefreshCw, ScrollText } from 'lucide-react';
import { cn } from '../utils/cn';

const API = '';

interface LogFile {
  name: string;
  sizeBytes: number;
  modifiedAt: string;
}

// Response of GET /api/logs/{name}. Offsets are byte positions of line starts, so pages
// never split or repeat a line (see the paging notes above get_log_contents in server.py).
interface LogPage {
  name: string;
  lines: string[];
  startOffset: number;
  endOffset: number;
  fileSize: number;
  hasMore: boolean;
  reset: boolean;
}

const PAGE_SIZES = [300, 1000, 5000];

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

interface ParsedLine {
  ts: string;
  level: string;
  component: string;
  message: string;
}

// Log lines are "{ts} | {level:<8} | {component:<20} | {message}" (see
// _FileFormatter in src/infrastructure/logger.py). The fixed-width padding
// that makes columns line up in a raw text file is exactly what made this
// panel look sparse/hard to read once rendered as HTML — parse into fields
// and lay them out with CSS instead of dumping the padded text as-is.
function parseLine(line: string): ParsedLine {
  const parts = line.split(' | ');
  if (parts.length >= 4) {
    return {
      ts: parts[0].trim(),
      level: parts[1].trim(),
      component: parts[2].trim(),
      message: parts.slice(3).join(' | ').trim(),
    };
  }
  return { ts: '', level: '', component: '', message: line };
}

const LEVEL_CLASS: Record<string, string> = {
  ERROR: 'text-red-400 bg-red-500/10',
  CRITICAL: 'text-red-400 bg-red-500/10',
  WARNING: 'text-yellow-400 bg-yellow-500/10',
  SUCCESS: 'text-green-400 bg-green-500/10',
  DEBUG: 'text-gray-500 bg-gray-500/10',
  INFO: 'text-blue-400 bg-blue-500/10',
};

// Newest lines are shown at the TOP (as before). Scrolling DOWN reaches older lines and keeps
// loading them until the start of the file: there is no cap on how far back you can go.
export function Logs() {
  const [files, setFiles] = useState<LogFile[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [lines, setLines] = useState<string[]>([]); // chronological, oldest -> newest
  const [pending, setPending] = useState<string[]>([]); // new lines held back while scrolled away from the top
  const [startOffset, setStartOffset] = useState(0);
  const [fileSize, setFileSize] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [pageSize, setPageSize] = useState(1000);

  const scrollRef = useRef<HTMLDivElement>(null);
  const endOffsetRef = useRef(0); // follow-mode cursor (advances even while lines are buffered)
  const startOffsetRef = useRef(0);
  const loadingOlderRef = useRef(false);
  const selectedRef = useRef('');

  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  useEffect(() => {
    fetch(`${API}/api/logs/files`)
      .then((r) => r.json())
      .then((data: LogFile[]) => {
        setFiles(data);
        if (!selected && data.length > 0) {
          const preferred = data.find((f) => f.name === 'orchestrator.log') ?? data[0];
          setSelected(preferred.name);
        }
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const applyPage = useCallback((page: LogPage) => {
    setLines(page.lines);
    setPending([]);
    setStartOffset(page.startOffset);
    setFileSize(page.fileSize);
    setHasMore(page.hasMore);
    startOffsetRef.current = page.startOffset;
    endOffsetRef.current = page.endOffset;
  }, []);

  // Newest page (initial load, manual refresh, page-size change, or the file was rotated).
  const loadLatest = useCallback(() => {
    if (!selected) return;
    setLoading(true);
    fetch(`${API}/api/logs/${encodeURIComponent(selected)}?tail=${pageSize}`)
      .then((r) => r.json())
      .then((page: LogPage) => {
        if (selectedRef.current !== page.name) return;
        applyPage(page);
        if (scrollRef.current) scrollRef.current.scrollTop = 0;
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [selected, pageSize, applyPage]);

  // Older page: everything before what is already loaded, prepended in time (appended below).
  const loadOlder = useCallback(() => {
    if (!selected || loadingOlderRef.current || startOffsetRef.current <= 0) return;
    loadingOlderRef.current = true;
    setLoadingOlder(true);
    fetch(
      `${API}/api/logs/${encodeURIComponent(selected)}?tail=${pageSize}&before=${startOffsetRef.current}`,
    )
      .then((r) => r.json())
      .then((page: LogPage) => {
        if (selectedRef.current !== page.name) return;
        setLines((prev) => [...page.lines, ...prev]);
        setStartOffset(page.startOffset);
        setHasMore(page.hasMore);
        startOffsetRef.current = page.startOffset;
      })
      .catch(() => {})
      .finally(() => {
        loadingOlderRef.current = false;
        setLoadingOlder(false);
      });
  }, [selected, pageSize]);

  // Initial / file-change / page-size-change load. setState only happens in the async callback
  // (never synchronously in the effect body); the Refresh button uses loadLatest() instead.
  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    fetch(`${API}/api/logs/${encodeURIComponent(selected)}?tail=${pageSize}`)
      .then((r) => r.json())
      .then((page: LogPage) => {
        if (cancelled || selectedRef.current !== page.name) return;
        applyPage(page);
        if (scrollRef.current) scrollRef.current.scrollTop = 0;
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [selected, pageSize, applyPage]);

  // Follow mode: only fetch what was written since the cursor. If the reader has scrolled
  // away from the top, hold the new lines back instead of shifting the page under them.
  useEffect(() => {
    if (!selected || !autoRefresh) return;
    const tick = () => {
      fetch(
        `${API}/api/logs/${encodeURIComponent(selected)}?tail=5000&after=${endOffsetRef.current}`,
      )
        .then((r) => r.json())
        .then((page: LogPage) => {
          if (selectedRef.current !== page.name) return;
          setFileSize(page.fileSize);
          if (page.reset) {
            applyPage(page); // file rotated/truncated: start over from its new tail
            return;
          }
          endOffsetRef.current = page.endOffset;
          if (page.lines.length === 0) return;
          const atTop = (scrollRef.current?.scrollTop ?? 0) < 40;
          if (atTop) setLines((prev) => [...prev, ...page.lines]);
          else setPending((prev) => [...prev, ...page.lines]);
        })
        .catch(() => {});
    };
    const interval = setInterval(tick, 5000);
    return () => clearInterval(interval);
  }, [selected, autoRefresh, applyPage]);

  const showPending = () => {
    setLines((prev) => [...prev, ...pending]);
    setPending([]);
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  };

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 400 && hasMore) loadOlder();
  };

  const rows = useMemo(
    () =>
      [...lines].reverse().map((line) => {
        const p = parseLine(line);
        return { p, levelCls: LEVEL_CLASS[p.level] ?? '' };
      }),
    [lines],
  );

  const selectedMeta = files.find((f) => f.name === selected);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-white flex items-center gap-2">
          <ScrollText className="w-6 h-6 text-blue-400" /> Logs
        </h2>
        <p className="text-gray-400">
          Every component's log file, the whole file — scroll down for older lines
        </p>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <select
          value={selected}
          onChange={(e) => {
            setSelected(e.target.value);
            setLines([]); // don't show the previous file's lines while the new one loads
            setPending([]);
          }}
          className="bg-[#0d1117] border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200"
        >
          {files.length === 0 && <option value="">No log files found</option>}
          {files.map((f) => (
            <option key={f.name} value={f.name}>
              {f.name}
            </option>
          ))}
        </select>

        {selectedMeta && (
          <span className="text-xs text-gray-500">
            {fmtSize(Math.max(fileSize, selectedMeta.sizeBytes))} · updated{' '}
            {new Date(selectedMeta.modifiedAt).toLocaleTimeString()}
          </span>
        )}

        <select
          value={pageSize}
          onChange={(e) => setPageSize(Number(e.target.value))}
          title="Lines fetched per page"
          className="bg-[#0d1117] border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200"
        >
          {PAGE_SIZES.map((n) => (
            <option key={n} value={n}>
              {n} lines / page
            </option>
          ))}
        </select>

        <button
          onClick={loadLatest}
          className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium text-gray-400 bg-[#0d1117] border border-gray-800 hover:text-gray-200"
        >
          <RefreshCw className={cn('w-4 h-4', loading && 'animate-spin')} /> Refresh
        </button>

        {selected && (
          <a
            href={`${API}/api/logs/${encodeURIComponent(selected)}/download`}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium text-gray-400 bg-[#0d1117] border border-gray-800 hover:text-gray-200"
          >
            <Download className="w-4 h-4" /> Download full file
          </a>
        )}

        <label className="flex items-center gap-1.5 text-xs text-gray-500 ml-auto cursor-pointer select-none">
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
          />
          Follow (5s)
        </label>
      </div>

      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-4">
        {pending.length > 0 && (
          <button
            onClick={showPending}
            className="mb-2 w-full px-3 py-1.5 rounded-lg text-sm font-medium text-blue-300 bg-blue-500/10 border border-blue-500/30 hover:bg-blue-500/20"
          >
            {pending.length} new line{pending.length === 1 ? '' : 's'} — click to show
          </button>
        )}
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="font-mono text-[15px] leading-relaxed bg-[#0d1117] rounded-lg p-3 h-[75vh] min-h-[520px] overflow-y-auto overflow-x-auto"
        >
          {rows.length === 0 ? (
            <p className="text-gray-500 text-center py-8 text-sm">
              {selected ? (loading ? 'Loading…' : 'No lines in this log yet') : 'Select a log file above'}
            </p>
          ) : (
            <>
              {rows.map(({ p, levelCls }, i) => (
                <div
                  key={i}
                  className="grid gap-3 px-2 py-1.5 rounded hover:bg-white/5 items-baseline"
                  style={{ gridTemplateColumns: '170px 84px 170px 1fr' }}
                >
                  <span className="text-gray-500 whitespace-nowrap">{p.ts}</span>
                  {p.level ? (
                    <span className={cn('px-1.5 py-0.5 rounded text-xs font-bold text-center', levelCls)}>
                      {p.level}
                    </span>
                  ) : (
                    <span />
                  )}
                  <span className="text-gray-500 truncate" title={p.component}>
                    {p.component}
                  </span>
                  <span className="text-gray-100 whitespace-pre-wrap break-all">{p.message}</span>
                </div>
              ))}
              <div className="py-3 text-center text-xs text-gray-500">
                {loadingOlder ? (
                  'Loading older lines…'
                ) : hasMore ? (
                  <button onClick={loadOlder} className="text-blue-400 hover:underline">
                    Load {pageSize} older lines
                  </button>
                ) : (
                  `Start of file — ${lines.length.toLocaleString()} lines loaded`
                )}
              </div>
            </>
          )}
        </div>
        <p className="mt-2 text-xs text-gray-600">
          {lines.length.toLocaleString()} lines loaded
          {startOffset > 0 ? ` · ${fmtSize(startOffset)} of the file still older` : ''}
        </p>
      </div>
    </div>
  );
}
