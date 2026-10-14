import { FlaskConical, RefreshCw, AlertTriangle, CheckCircle2, Clock, BarChart2, ChevronDown, ChevronRight } from 'lucide-react';
import { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi';

interface Strategy {
  name: string;
  display_name: string;
  direction: string;
  timeframe: string;
  product: string;
  condition_count: number;
  target_rr: number | null;
  source_format: string;
  health: string;
  enabled: boolean;
  executionMode: 'paper' | 'live';
}

interface RejectedEntry { name: string; reason: string; }

interface DiscoveryScan {
  accepted: string[];
  rejected: RejectedEntry[];
  unchanged: number;
  scanned_at: string;
}

interface BacktestStats {
  total_trades: number;
  long_trades: number;
  short_trades: number;
  win_rate: number;
  profit_factor: number;
  expectancy_r: number;
  avg_win_r: number;
  avg_loss_r: number;
  max_drawdown_r: number;
  symbols_with_signals: number;
  symbols_tested: number;
  passed: boolean;
  reject_reason: string;
  not_applicable?: boolean;
  computed_at: string;
}

interface Trade {
  symbol: string;
  direction: string;
  entry_time: string;
  entry: number;
  sl: number;
  target: number;
  exit_time: string;
  exit: number | null;
  exit_reason: string;
  pnl_r: number | null;
  pct: number;
}

interface EvolutionEntry {
  strategy: string;
  promoted: boolean;
  reason: string;
  best_expectancy_r: number;
}

interface EvolutionReport {
  evolutions: EvolutionEntry[];
  run_at: string;
}

function parseErrors(reason: string): string[] {
  const match = reason.match(/^Validation failed:\s*\[(.+)\]$/s);
  if (!match) return [reason];
  return match[1]
    .split(/",\s*"/)
    .map(e => e.replace(/^"+|"+$/g, '').replace(/\. Known:.*$/s, '').trim());
}

const healthBadge: Record<string, string> = {
  HEALTHY:   'text-green-400  bg-green-500/10  border-green-500/30',
  DEGRADED:  'text-yellow-400 bg-yellow-500/10 border-yellow-500/30',
  SUSPENDED: 'text-red-400    bg-red-500/10    border-red-500/30',
};

// Live-trading qualification thresholds — mirror src/strategies/backtest/engine.py
const QUALIFY = { trades: 20, winRate: 0.35, pf: 1.30, expectancy: 0.20 };
const passClr = (ok: boolean) => (ok ? 'text-green-400' : 'text-red-400');

const tfColor: Record<string, string> = {
  DAILY:  'text-blue-300 bg-blue-500/10',
  WEEKLY: 'text-purple-300 bg-purple-500/10',
  '5MIN': 'text-cyan-300 bg-cyan-500/10',
  '15MIN':'text-cyan-300 bg-cyan-500/10',
};

export function StrategyStudio() {
  const { data: strategies, loading } = useApi<Strategy[]>('/api/strategies', 30000);
  const { data: scan } = useApi<DiscoveryScan>('/api/discovery_scan', 30000);
  const { data: evo } = useApi<EvolutionReport>('/api/evolution_report', 60000);
  const { data: backtest } = useApi<Record<string, BacktestStats>>('/api/backtest_summary', 60000);
  const { data: trades } = useApi<Record<string, Trade[]>>('/api/backtest_trades', 60000);
  const [expanded, setExpanded] = useState<string | null>(null);

  const [enabledOverride, setEnabledOverride] = useState<Record<string, boolean>>({});
  const [modeOverride, setModeOverride] = useState<Record<string, 'paper' | 'live'>>({});
  const [pendingNames, setPendingNames] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (strategies) {
      setEnabledOverride({});
      setModeOverride({});
    }
  }, [strategies]);

  const handleSetExecutionMode = async (name: string, mode: 'paper' | 'live') => {
    const prevMode = modeOverride[name] ?? strategies?.find((s) => s.name === name)?.executionMode ?? 'paper';
    setModeOverride((prev) => ({ ...prev, [name]: mode }));
    try {
      const res = await fetch(`/api/strategies/${name}/execution_mode`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ executionMode: mode }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      console.error('[ashwax] failed to set strategy execution mode', name, e);
      setModeOverride((prev) => ({ ...prev, [name]: prevMode }));
    }
  };

  const handleToggleEnabled = async (name: string, current: boolean) => {
    if (pendingNames.has(name)) return;
    const next = !current;
    setPendingNames((prev) => new Set(prev).add(name));
    setEnabledOverride((prev) => ({ ...prev, [name]: next }));
    try {
      const res = await fetch(`/api/strategies/${name}/enabled`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      console.error('[ashwax] failed to toggle strategy', name, e);
      setEnabledOverride((prev) => ({ ...prev, [name]: current }));
    } finally {
      setPendingNames((prev) => {
        const nextSet = new Set(prev);
        nextSet.delete(name);
        return nextSet;
      });
    }
  };

  return (
    <div className="space-y-6">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white flex items-center gap-2">
            <FlaskConical className="w-6 h-6 text-purple-400" />
            Strategy Studio
          </h2>
          <p className="text-gray-400 mt-1">
            {strategies ? `${strategies.length} strategies loaded` : 'Loading…'}
            {scan?.scanned_at && (
              <span className="ml-3 text-gray-600">
                · scanned {new Date(scan.scanned_at).toLocaleTimeString()}
              </span>
            )}
          </p>
        </div>
        {scan && (
          <div className="flex gap-3 text-sm">
            <span className="flex items-center gap-1.5 text-green-400">
              <CheckCircle2 className="w-4 h-4" /> {scan.accepted.length} accepted
            </span>
            {scan.rejected.length > 0 && (
              <span className="flex items-center gap-1.5 text-red-400">
                <AlertTriangle className="w-4 h-4" /> {scan.rejected.length} rejected
              </span>
            )}
          </div>
        )}
      </div>

      {/* Strategy cards */}
      {loading ? (
        <p className="text-gray-500 text-sm">Loading strategies…</p>
      ) : !strategies?.length ? (
        <p className="text-gray-500 text-sm">No strategies found in strategies/builtin/</p>
      ) : (
        <div className="grid grid-cols-1 gap-3">
          {strategies.map((s) => (
            <div key={s.name} className="bg-[#161b22] border border-gray-800 rounded-xl overflow-hidden">
              <div className="p-4 flex items-center justify-between">
                <div>
                  <div className="font-semibold text-white">{s.display_name}</div>
                  <div className="text-xs text-gray-500 mt-0.5">
                    <span className="font-mono">{s.name}</span> · {s.product} · {s.condition_count} conditions
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={() => handleToggleEnabled(s.name, enabledOverride[s.name] ?? s.enabled)}
                    disabled={pendingNames.has(s.name)}
                    title={
                      (enabledOverride[s.name] ?? s.enabled)
                        ? 'Enabled — click to disable'
                        : 'Disabled — click to enable'
                    }
                    className={`w-12 h-7 rounded-full p-1 transition-colors shrink-0 ${
                      (enabledOverride[s.name] ?? s.enabled) ? 'bg-green-500' : 'bg-gray-600'
                    }`}
                  >
                    <div
                      className={`w-5 h-5 rounded-full bg-white transition-transform ${
                        (enabledOverride[s.name] ?? s.enabled) ? 'translate-x-5' : 'translate-x-0'
                      }`}
                    />
                  </button>
                  <select
                    value={modeOverride[s.name] ?? s.executionMode}
                    onChange={(e) => handleSetExecutionMode(s.name, e.target.value as 'paper' | 'live')}
                    title="Paper/live decision for this strategy (blocked while the account is paper-locked)"
                    className="text-xs px-2 py-1 rounded bg-gray-800 border border-gray-700 text-gray-300 font-medium"
                  >
                    <option value="paper">Paper</option>
                    <option value="live">Live</option>
                  </select>
                  <span className="text-xs px-2 py-1 rounded bg-gray-800 text-gray-300 font-medium">{s.direction}</span>
                  <span className={`text-xs px-2 py-1 rounded font-medium ${tfColor[s.timeframe] ?? 'text-gray-400 bg-gray-800'}`}>{s.timeframe}</span>
                  {s.target_rr != null && (
                    <span className="text-xs px-2 py-1 rounded bg-gray-800 text-cyan-300 font-mono">{s.target_rr}R</span>
                  )}
                  <span className={`text-xs px-2.5 py-1 rounded border font-medium ${healthBadge[s.health] ?? 'text-gray-400 bg-gray-800 border-gray-700'}`}>{s.health}</span>
                </div>
              </div>
              {backtest?.[s.name] ? (() => {
                const bt = backtest[s.name];
                if (bt.not_applicable) {
                  return (
                    <div className="border-t border-gray-800 bg-gray-900/40 px-4 py-3 text-xs text-gray-400 flex items-center gap-1.5">
                      <BarChart2 className="w-3 h-3 text-gray-500" />
                      Not backtested — {bt.reject_reason}
                    </div>
                  );
                }
                return (
                  <div className="border-t border-gray-800 bg-gray-900/40 px-4 py-3 space-y-2 text-xs">
                    {/* Coverage + direction breakdown */}
                    <div className="flex items-center gap-2 text-gray-400 flex-wrap">
                      <BarChart2 className="w-3 h-3 text-gray-500" />
                      Backtested on <span className="text-gray-200">{bt.symbols_with_signals}</span> of {bt.symbols_tested} stocks ·
                      <span className="text-gray-200">{bt.total_trades}</span> trades over full history
                      <span className="text-gray-600">(</span>
                      <span className="text-green-400" title="Trades taken to the long side">▲ {bt.long_trades} long</span>
                      <span className="text-gray-600">/</span>
                      <span className="text-red-400" title="Trades taken to the short side">▼ {bt.short_trades} short</span>
                      <span className="text-gray-600">)</span>
                    </div>
                    {/* Qualification metrics — each labeled with what it must beat */}
                    <div className="flex items-center gap-x-5 gap-y-1.5 flex-wrap">
                      <span title={`Win rate — share of trades that made money. Must be ≥ ${(QUALIFY.winRate * 100).toFixed(0)}% to qualify.`}>
                        <span className="text-gray-500">Win rate </span>
                        <span className={passClr(bt.win_rate >= QUALIFY.winRate)}>{(bt.win_rate * 100).toFixed(1)}%</span>
                        <span className="text-gray-600"> /need ≥{(QUALIFY.winRate * 100).toFixed(0)}%</span>
                      </span>
                      <span title={`Profit factor — gross profit ÷ gross loss. Must be ≥ ${QUALIFY.pf} to qualify.`}>
                        <span className="text-gray-500">Profit factor </span>
                        <span className={passClr(bt.profit_factor >= QUALIFY.pf)}>{bt.profit_factor.toFixed(2)}</span>
                        <span className="text-gray-600"> /need ≥{QUALIFY.pf.toFixed(2)}</span>
                      </span>
                      <span title={`Expectancy — average R earned per trade. Must be ≥ ${QUALIFY.expectancy}R to qualify.`}>
                        <span className="text-gray-500">Expectancy </span>
                        <span className={passClr(bt.expectancy_r >= QUALIFY.expectancy)}>{bt.expectancy_r.toFixed(2)}R/trade</span>
                        <span className="text-gray-600"> /need ≥{QUALIFY.expectancy.toFixed(2)}R</span>
                      </span>
                      <span className="text-gray-500" title="Average winning trade vs average losing trade, in R (risk multiples)">
                        Avg win <span className="text-green-400">+{bt.avg_win_r.toFixed(2)}R</span> · avg loss <span className="text-red-400">−{Math.abs(bt.avg_loss_r).toFixed(2)}R</span>
                      </span>
                      <span className="text-gray-500" title="Worst peak-to-trough equity dip during the backtest, in R">
                        Max drawdown <span className="text-gray-300">{bt.max_drawdown_r.toFixed(1)}R</span>
                      </span>
                    </div>
                    {/* Verdict */}
                    <div className="flex items-center gap-2">
                      <span className={`font-semibold ${bt.passed ? 'text-green-400' : 'text-red-400'}`}>
                        {bt.passed ? '✓ Qualifies for live trading' : '✗ Does not qualify'}
                      </span>
                      {!bt.passed && bt.reject_reason && (
                        <span className="text-gray-500">— {bt.reject_reason}</span>
                      )}
                      <span className="ml-auto text-gray-600">needs ≥{QUALIFY.trades} trades · WR≥{(QUALIFY.winRate * 100).toFixed(0)}% · PF≥{QUALIFY.pf} · E≥{QUALIFY.expectancy}R</span>
                    </div>
                    {/* Per-trade audit — the agent's modeled decisions (read-only); compare vs TradingView */}
                    {trades?.[s.name]?.length ? (
                      <div className="pt-1">
                        <button
                          onClick={() => setExpanded(expanded === s.name ? null : s.name)}
                          className="flex items-center gap-1 text-gray-400 hover:text-gray-200"
                        >
                          {expanded === s.name ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                          {expanded === s.name ? 'Hide' : 'View'} all {trades[s.name].length} backtest trades (across stocks &amp; months) — compare vs TradingView
                        </button>
                        {expanded === s.name && (
                          <div className="mt-2 overflow-x-auto">
                            <div className="text-gray-600 mb-1.5 font-sans leading-relaxed">
                              Historical backtest signals across many stocks over months — <span className="text-gray-400">not trades in a single day</span>.{' '}
                              <span className="text-gray-400">Profit (R)</span> = profit in risk-multiples (1R = the initial stop distance; +2R = made twice what was risked).{' '}
                              <span className="text-gray-400">Move %</span> = raw price change from entry to exit.
                            </div>
                            <table className="w-full text-[11px] font-mono">
                              <thead className="text-gray-500">
                                <tr className="text-left">
                                  <th className="pr-2">#</th><th className="pr-2">Sym</th><th className="pr-2">Dir</th>
                                  <th className="pr-2">Entry (IST)</th><th className="pr-2 text-right">Entry</th>
                                  <th className="pr-2 text-right">SL</th><th className="pr-2 text-right">Tgt</th>
                                  <th className="pr-2">Exit (IST)</th><th className="pr-2 text-right">Exit</th>
                                  <th className="pr-2">Reason</th>
                                  <th className="pr-2 text-right" title="Profit in R — multiples of the risk taken (1R = initial stop distance)">Profit (R)</th>
                                  <th className="text-right" title="Raw price move from entry to exit">Move %</th>
                                </tr>
                              </thead>
                              <tbody>
                                {trades[s.name].map((t, i) => (
                                  <tr key={i} className="border-t border-gray-800/60">
                                    <td className="pr-2 text-gray-600">{i + 1}</td>
                                    <td className="pr-2 text-gray-300">{t.symbol.split(':').pop()}</td>
                                    <td className={`pr-2 ${t.direction === 'LONG' ? 'text-green-400' : 'text-red-400'}`}>{t.direction}</td>
                                    <td className="pr-2 text-gray-400">{t.entry_time}</td>
                                    <td className="pr-2 text-right text-gray-300">{t.entry}</td>
                                    <td className="pr-2 text-right text-gray-500">{t.sl}</td>
                                    <td className="pr-2 text-right text-gray-500">{t.target}</td>
                                    <td className="pr-2 text-gray-400">{t.exit_time}</td>
                                    <td className="pr-2 text-right text-gray-300">{t.exit ?? '—'}</td>
                                    <td className="pr-2 text-gray-400">{t.exit_reason}</td>
                                    <td className={`pr-2 text-right ${(t.pnl_r ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>{t.pnl_r?.toFixed(2) ?? '—'}</td>
                                    <td className={`text-right ${t.pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>{t.pct?.toFixed(2)}%</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </div>
                    ) : null}
                  </div>
                );
              })() : (
                <div className="border-t border-gray-800 px-4 py-2 text-xs text-gray-600 flex items-center gap-1.5">
                  <BarChart2 className="w-3 h-3" /> Backtesting on startup…
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Rejection details — only when there are rejections */}
      {scan && scan.rejected.length > 0 && (
        <div className="bg-[#161b22] border border-red-900/40 rounded-xl p-5">
          <h3 className="text-sm font-semibold text-red-400 flex items-center gap-2 mb-3">
            <AlertTriangle className="w-4 h-4" />
            Validation Failures
          </h3>
          <div className="space-y-3">
            {scan.rejected.map((r) => (
              <div key={r.name} className="border-t border-gray-800 pt-3 first:border-0 first:pt-0">
                <div className="font-mono text-red-300 text-sm font-semibold mb-1">{r.name}</div>
                <ul className="space-y-0.5 pl-3">
                  {parseErrors(r.reason).map((e, i) => (
                    <li key={i} className="text-gray-400 text-xs flex gap-2">
                      <span className="text-red-600 shrink-0">·</span>{e}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Evolution history */}
      <div className="bg-[#161b22] border border-gray-800 rounded-xl p-5">
        <h3 className="font-semibold text-white flex items-center gap-2 mb-1">
          <RefreshCw className="w-4 h-4 text-purple-400" />
          Evolution History
        </h3>
        {!evo?.evolutions?.length ? (
          <div className="flex items-center gap-2 text-gray-600 text-sm mt-3">
            <Clock className="w-4 h-4" />
            Runs every Saturday 21:00 IST for DEGRADED strategies
          </div>
        ) : (
          <div className="mt-3 space-y-2">
            <p className="text-xs text-gray-600">Last run: {new Date(evo.run_at).toLocaleString()}</p>
            {evo.evolutions.map((e) => (
              <div key={e.strategy} className="flex items-center justify-between border-t border-gray-800 pt-2">
                <span className="font-mono text-white text-sm">{e.strategy}</span>
                <div className="flex items-center gap-4 text-sm">
                  <span className="font-mono text-cyan-300">{e.best_expectancy_r.toFixed(3)}R</span>
                  <span className={e.promoted ? 'text-green-400' : 'text-gray-500'}>
                    {e.promoted ? '✓ Promoted' : '✗ No change'}
                  </span>
                  <span className="text-gray-600 text-xs">{e.reason}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
