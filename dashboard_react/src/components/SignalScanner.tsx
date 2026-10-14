import { useEffect, useState } from 'react';
import { Search, TrendingUp, Star, LineChart } from 'lucide-react';

interface NightlySetup {
  symbol: string;
  score?: number;
  stage?: string;
  days_to_pivot?: number;
  rs_rank?: number;
  setup_type?: string;
}

interface RSLeader {
  symbol: string;
  rs_score?: number;
  percentile_rank?: number;
  rs_new_high?: boolean;
}

interface GateResult {
  gate_id: string;
  decision: 'PASS' | 'BLOCK' | 'MODIFY';
  reason: string;
}

interface SwingSignal {
  symbol: string;
  strategy_name?: string;
  direction?: string;
  entry?: number;
  sl?: number;
  target?: number;
  decision: 'executed' | 'blocked';
  gate?: string | null;
  gate_results?: GateResult[];
  ts?: string;
}

function formatSignalTs(ts?: string): string {
  if (!ts) return '—';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('en-IN', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

interface ScannerData {
  nightly_setups: NightlySetup[];
  rs_leaders: RSLeader[];
  swing_signals: SwingSignal[];
  swing_scan_ts: string | null;
}

const API_BASE = '/api';
const REFRESH_MS = 15_000;

function scoreColor(s?: number): string {
  if (!s) return 'text-gray-400';
  if (s >= 75) return 'text-green-400';
  if (s >= 60) return 'text-yellow-400';
  return 'text-red-400';
}

export function SignalScanner() {
  const [scanner, setScanner] = useState<ScannerData>({
    nightly_setups: [],
    rs_leaders: [],
    swing_signals: [],
    swing_scan_ts: null,
  });
  const [lastRefresh, setLastRefresh] = useState<Date>(new Date());
  const [error, setError] = useState<string | null>(null);

  const fetchData = async () => {
    try {
      const scanRes = await fetch(`${API_BASE}/scanner`);
      if (scanRes.ok) {
        const data = await scanRes.json();
        setScanner({
          nightly_setups: Array.isArray(data?.nightly_setups) ? data.nightly_setups : [],
          rs_leaders: Array.isArray(data?.rs_leaders) ? data.rs_leaders : [],
          swing_signals: Array.isArray(data?.swing_signals) ? data.swing_signals : [],
          swing_scan_ts: typeof data?.swing_scan_ts === 'string' ? data.swing_scan_ts : null,
        });
      }
      setLastRefresh(new Date());
      setError(null);
    } catch {
      setError('Failed to load scanner data');
    }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, REFRESH_MS);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center">
            <Search className="w-5 h-5 text-blue-400" />
          </div>
          <div>
            <h2 className="text-xl font-bold text-white">Signals / Scanner</h2>
            <p className="text-sm text-gray-400">
              Live signals · Nightly setups · RS leaders · 15s refresh
            </p>
          </div>
        </div>
        <div className="text-xs text-gray-500">
          Last updated: {lastRefresh.toLocaleTimeString('en-IN')}
        </div>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-800 rounded-lg p-3 text-red-400 text-sm">
          {error}
        </div>
      )}

      {/* Swing (DAILY strategy) signals — real evaluation output, not the cosmetic nightly screen */}
      <div className="bg-[#161b22] border border-gray-800 rounded-xl overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-800 flex items-center justify-between flex-wrap gap-2">
          <div className="flex items-center gap-2">
            <LineChart className="w-4 h-4 text-purple-400" />
            <span className="font-semibold text-white">Swing Signals (DAILY strategies)</span>
            <span className="text-xs text-gray-500">{scanner.swing_signals.length} evaluated</span>
          </div>
          {scanner.swing_scan_ts && (
            <span className="text-sm font-mono font-semibold text-purple-300 bg-purple-900/20 border border-purple-800/60 rounded-md px-3 py-1">
              Last scan: {new Date(scanner.swing_scan_ts).toLocaleString('en-IN', {
                day: '2-digit',
                month: '2-digit',
                year: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
              })}
            </span>
          )}
        </div>
        {scanner.swing_signals.length === 0 ? (
          <div className="px-5 py-8 text-center text-gray-500 text-sm">
            No swing signals yet. The daily scan runs once after market close (15:35 IST).
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-800">
                  <th className="px-5 py-3 text-left text-xs text-gray-400 uppercase tracking-wide">Time</th>
                  <th className="px-5 py-3 text-left text-xs text-gray-400 uppercase tracking-wide">Symbol</th>
                  <th className="px-5 py-3 text-left text-xs text-gray-400 uppercase tracking-wide">Strategy</th>
                  <th className="px-5 py-3 text-left text-xs text-gray-400 uppercase tracking-wide">Dir</th>
                  <th className="px-5 py-3 text-right text-xs text-gray-400 uppercase tracking-wide">Entry</th>
                  <th className="px-5 py-3 text-right text-xs text-gray-400 uppercase tracking-wide">SL</th>
                  <th className="px-5 py-3 text-right text-xs text-gray-400 uppercase tracking-wide">Target</th>
                  <th className="px-5 py-3 text-left text-xs text-gray-400 uppercase tracking-wide">Decision</th>
                </tr>
              </thead>
              <tbody>
                {scanner.swing_signals.map((s, i) => (
                  <tr key={`${s.symbol}-${s.strategy_name}-${i}`} className="border-b border-gray-800/50 hover:bg-white/[0.02]">
                    <td className="px-5 py-3 font-mono text-sm font-bold text-gray-300 whitespace-nowrap">{formatSignalTs(s.ts)}</td>
                    <td className="px-5 py-3 font-mono font-semibold text-white">{s.symbol.split(':').pop()}</td>
                    <td className="px-5 py-3 text-gray-300">{s.strategy_name ?? '—'}</td>
                    <td className="px-5 py-3 text-gray-300">{s.direction ?? '—'}</td>
                    <td className="px-5 py-3 text-right font-mono text-gray-300">{s.entry?.toFixed(2) ?? '—'}</td>
                    <td className="px-5 py-3 text-right font-mono text-gray-300">{s.sl?.toFixed(2) ?? '—'}</td>
                    <td className="px-5 py-3 text-right font-mono text-gray-300">{s.target?.toFixed(2) ?? '—'}</td>
                    <td className="px-5 py-3">
                      {(() => {
                        const gateTitle = s.gate_results?.length
                          ? s.gate_results
                              .map((g) => `${g.gate_id}=${g.decision}${g.reason ? ` (${g.reason})` : ''}`)
                              .join('\n')
                          : undefined;
                        if (s.decision === 'executed') {
                          return (
                            <span
                              title={gateTitle}
                              className="text-xs px-2 py-0.5 rounded-full bg-green-900/30 text-green-400 border border-green-800"
                            >
                              EXECUTED
                            </span>
                          );
                        }
                        // A real CRM gate rejection ("crm_blocked:G3") vs every other
                        // reason (not_filled, already_held, sizing_zero, ...) — those
                        // mean the signal PASSED all gates and either never reached CRM
                        // (pre-checks) or was approved but failed at order placement.
                        // Collapsing both into "BLOCKED" hides which one happened.
                        const isGateBlock = s.gate?.startsWith('crm_blocked:');
                        const label = isGateBlock
                          ? `BLOCKED: ${s.gate!.replace('crm_blocked:', '')}`
                          : s.gate === 'not_filled' || s.gate === 'lifecycle_err'
                            ? `APPROVED, NOT FILLED: ${s.gate}`
                            : `NOT ATTEMPTED: ${s.gate ?? 'unknown'}`;
                        const color = isGateBlock
                          ? 'bg-red-900/30 text-red-400 border-red-800'
                          : 'bg-yellow-900/30 text-yellow-400 border-yellow-800';
                        return (
                          <span
                            title={gateTitle}
                            className={`text-xs px-2 py-0.5 rounded-full border ${color}`}
                          >
                            {label}
                          </span>
                        );
                      })()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Nightly Setups */}
        <div className="bg-[#161b22] border border-gray-800 rounded-xl overflow-hidden">
          <div className="px-5 py-4 border-b border-gray-800 flex items-center gap-2">
            <TrendingUp className="w-4 h-4 text-green-400" />
            <span className="font-semibold text-white">Nightly Setups</span>
            <span className="ml-auto text-xs text-gray-500">{scanner.nightly_setups.length} setups</span>
          </div>
          {scanner.nightly_setups.length === 0 ? (
            <div className="px-5 py-6 text-center text-gray-500 text-sm">No setups loaded yet.</div>
          ) : (
            <div className="divide-y divide-gray-800/50">
              {scanner.nightly_setups.slice(0, 15).map((s, i) => (
                <div key={s.symbol ?? i} className="px-5 py-3 flex items-center justify-between hover:bg-white/[0.02]">
                  <div>
                    <span className="font-mono font-semibold text-white">{s.symbol.split(':').pop()}</span>
                    {s.setup_type && (
                      <span className="ml-2 text-xs text-gray-500">{s.setup_type}</span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 text-xs">
                    {s.rs_rank != null && (
                      <span className="text-blue-400">RS#{s.rs_rank}</span>
                    )}
                    {s.days_to_pivot != null && (
                      <span className="text-gray-400">{s.days_to_pivot}d to pivot</span>
                    )}
                    {s.score != null && (
                      <span className={scoreColor(s.score)}>{s.score}/100</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* RS Leaders */}
        <div className="bg-[#161b22] border border-gray-800 rounded-xl overflow-hidden">
          <div className="px-5 py-4 border-b border-gray-800 flex items-center gap-2">
            <Star className="w-4 h-4 text-yellow-400" />
            <span className="font-semibold text-white">RS Leaders</span>
            <span className="ml-auto text-xs text-gray-500">{scanner.rs_leaders.length} leaders</span>
          </div>
          {scanner.rs_leaders.length === 0 ? (
            <div className="px-5 py-6 text-center text-gray-500 text-sm">No RS data loaded yet.</div>
          ) : (
            <div className="divide-y divide-gray-800/50">
              {scanner.rs_leaders.slice(0, 15).map((r, i) => (
                <div key={r.symbol ?? i} className="px-5 py-3 flex items-center justify-between hover:bg-white/[0.02]">
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-semibold text-white">{r.symbol.split(':').pop()}</span>
                    {r.rs_new_high && (
                      <span className="text-xs px-1.5 py-0.5 rounded bg-yellow-900/30 text-yellow-400 border border-yellow-800">
                        NEW HIGH
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 text-xs">
                    {r.percentile_rank != null && (
                      <span className="text-green-400">P{r.percentile_rank}</span>
                    )}
                    {r.rs_score != null && (
                      <span className="text-gray-400 font-mono">{Number(r.rs_score).toFixed(2)}</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
