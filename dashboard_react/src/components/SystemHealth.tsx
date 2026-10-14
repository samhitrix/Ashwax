import { Shield, AlertTriangle, TrendingDown, Activity, CheckCircle2, XCircle } from 'lucide-react';
import { useApi } from '../hooks/useApi';
import { cn } from '../utils/cn';

interface GapRiskItem { symbol: string; action: string; gap_risk_pct: number; reason: string; }
interface EdgeDecayData { [strategy: string]: { verdict: string; reason: string } }
interface WeeklyPnL { weeks: Record<string, number>; consecutive_negative: number; halt_threshold: number; halted: boolean }
interface CostErosion { level: string; cost_ratio: number; trade_count: number; reason: string }
interface LlmProviderRow { provider: string; model: string; status: string; latency_ms?: number | null; error?: string }
interface LlmChainRow { chain: string; status: string; answered_by?: string | null; primary?: string; latency_ms?: number | null; error?: string }
interface LlmHealth {
  checked_at?: string; trigger?: string; overall?: string; failures?: string[];
  providers?: LlmProviderRow[]; chains?: LlmChainRow[];
  history?: { checked_at: string; trigger: string; overall: string }[];
}
interface LlmProviderUsage { tokens: number; requests: number }
interface LlmTokens { checked_at?: string | null; providers?: Record<string, LlmProviderUsage>; purposes?: Record<string, { spent: number; limit: number | null }> }
const LLM_STATUS_COLOR: Record<string, string> = {
  OK: 'text-green-400 bg-green-500/20',
  DEGRADED: 'text-yellow-400 bg-yellow-500/20',
  FAIL: 'text-red-400 bg-red-500/20',
};

const ACTION_COLOR: Record<string, string> = {
  CLOSE: 'text-red-400 bg-red-500/20',
  REDUCE: 'text-orange-400 bg-orange-500/20',
  MONITOR: 'text-yellow-400 bg-yellow-500/20',
  HOLD: 'text-green-400 bg-green-500/20',
};

const VERDICT_COLOR: Record<string, string> = {
  EDGE_HEALTHY: 'text-green-400',
  EDGE_FADING: 'text-yellow-400',
  EDGE_GONE: 'text-orange-400',
  RETIRED: 'text-red-400',
};

export function SystemHealth() {
  const { data: gapRisk } = useApi<GapRiskItem[]>('/api/gap_risk', 10000);
  const { data: edgeDecay } = useApi<EdgeDecayData>('/api/edge_decay', 30000);
  const { data: weeklyPnl } = useApi<WeeklyPnL>('/api/weekly_pnl', 10000);
  const { data: costErosion } = useApi<CostErosion>('/api/cost_erosion', 10000);
  const { data: llmHealth } = useApi<LlmHealth>('/api/llm_health', 30000);
  // Unmetered providers (no llm.token_budgets purpose bucket, e.g. ollama) show no
  // usage row here — 30s matches llmHealth's own poll interval, this table shares it.
  const { data: llmTokens } = useApi<LlmTokens>('/api/llm_tokens', 30000);

  const gapList = gapRisk ?? [];
  const decayEntries = Object.entries(edgeDecay ?? {});
  const atRisk = gapList.filter(g => g.action === 'CLOSE' || g.action === 'REDUCE').length;
  const fading = decayEntries.filter(([, v]) => v.verdict !== 'EDGE_HEALTHY').length;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-white">System Health</h2>
        <p className="text-gray-400">Gap risk, edge decay, cost erosion, weekly P&L status</p>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className={cn('w-10 h-10 rounded-lg flex items-center justify-center', atRisk > 0 ? 'bg-red-500/20' : 'bg-green-500/20')}>
              <Shield className={cn('w-5 h-5', atRisk > 0 ? 'text-red-400' : 'text-green-400')} />
            </div>
            <div>
              <div className="text-xs text-gray-400">Gap Risk Positions</div>
              <div className={cn('text-xl font-bold font-mono', atRisk > 0 ? 'text-red-400' : 'text-green-400')}>{atRisk}</div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className={cn('w-10 h-10 rounded-lg flex items-center justify-center', weeklyPnl?.halted ? 'bg-red-500/20' : 'bg-green-500/20')}>
              <Activity className={cn('w-5 h-5', weeklyPnl?.halted ? 'text-red-400' : 'text-green-400')} />
            </div>
            <div>
              <div className="text-xs text-gray-400">Neg. Weeks</div>
              <div className={cn('text-xl font-bold font-mono', (weeklyPnl?.consecutive_negative ?? 0) > 0 ? 'text-orange-400' : 'text-green-400')}>
                {weeklyPnl?.consecutive_negative ?? 0}/{weeklyPnl?.halt_threshold ?? 4}
              </div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className={cn('w-10 h-10 rounded-lg flex items-center justify-center',
              costErosion?.level === 'RED' ? 'bg-red-500/20' : costErosion?.level === 'YELLOW' ? 'bg-yellow-500/20' : 'bg-green-500/20')}>
              <TrendingDown className={cn('w-5 h-5',
                costErosion?.level === 'RED' ? 'text-red-400' : costErosion?.level === 'YELLOW' ? 'text-yellow-400' : 'text-green-400')} />
            </div>
            <div>
              <div className="text-xs text-gray-400">Cost Erosion</div>
              <div className={cn('text-xl font-bold font-mono',
                costErosion?.level === 'RED' ? 'text-red-400' : costErosion?.level === 'YELLOW' ? 'text-yellow-400' : 'text-green-400')}>
                {costErosion?.level ?? '—'}
              </div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className={cn('w-10 h-10 rounded-lg flex items-center justify-center', fading > 0 ? 'bg-orange-500/20' : 'bg-green-500/20')}>
              <AlertTriangle className={cn('w-5 h-5', fading > 0 ? 'text-orange-400' : 'text-green-400')} />
            </div>
            <div>
              <div className="text-xs text-gray-400">Strategies Fading</div>
              <div className={cn('text-xl font-bold font-mono', fading > 0 ? 'text-orange-400' : 'text-green-400')}>{fading}</div>
            </div>
          </div>
        </div>
      </div>

      {/* LLM health alert banner — any provider / gate chain not working */}
      {llmHealth?.overall && llmHealth.overall !== 'OK' && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-4 flex items-start gap-3">
          <XCircle className="w-5 h-5 text-red-400 flex-shrink-0 mt-0.5" />
          <div className="text-sm">
            <div className="text-red-400 font-medium">LLM {llmHealth.overall} — checked {llmHealth.checked_at} ({llmHealth.trigger})</div>
            {(llmHealth.failures ?? []).map((f) => (
              <div key={f} className="text-red-300/80 font-mono text-xs mt-1">{f}</div>
            ))}
          </div>
        </div>
      )}

      {/* Halt banner */}
      {weeklyPnl?.halted && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-4 flex items-center gap-3">
          <XCircle className="w-5 h-5 text-red-400 flex-shrink-0" />
          <span className="text-red-400 font-medium">
            Trading HALTED — {weeklyPnl.consecutive_negative} consecutive negative weeks (threshold: {weeklyPnl.halt_threshold})
          </span>
        </div>
      )}

      {/* LLM model health — morning / evening / startup probes of every provider and gate chain */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <h3 className="font-semibold text-white flex items-center gap-2 mb-1">
          <Activity className="w-5 h-5 text-blue-400" />
          LLM Models
          {llmHealth?.overall && (
            <span className={cn('ml-2 px-2 py-0.5 rounded text-xs font-semibold', LLM_STATUS_COLOR[llmHealth.overall] ?? 'text-gray-400 bg-gray-500/20')}>
              {llmHealth.overall}
            </span>
          )}
        </h3>
        <p className="text-xs text-gray-500 mb-4">
          {llmHealth?.checked_at ? `Last check ${llmHealth.checked_at} (${llmHealth.trigger}); runs at startup, 08:10 and 16:30 IST` : 'No check yet — runs ~45s after the orchestrator starts'}
        </p>
        {(llmHealth?.providers ?? []).length > 0 && (
          <table className="w-full text-sm mb-4">
            <thead>
              <tr className="text-left text-gray-400 border-b border-gray-800">
                <th className="pb-2 pr-4">Provider</th><th className="pb-2 pr-4">Model</th>
                <th className="pb-2 pr-4">Status</th><th className="pb-2 pr-4">Latency</th>
                <th className="pb-2 pr-4">Tokens Used / Left</th><th className="pb-2">Detail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {(llmHealth?.providers ?? []).map((p) => {
                const usage = llmTokens?.providers?.[p.provider];
                return (
                <tr key={`${p.provider}:${p.model}`}>
                  <td className="py-2 pr-4 font-mono text-white">{p.provider}</td>
                  <td className="py-2 pr-4 font-mono text-gray-300">{p.model || '—'}</td>
                  <td className="py-2 pr-4">
                    <span className={cn('px-2 py-0.5 rounded text-xs font-semibold', p.status === 'OK' ? LLM_STATUS_COLOR.OK : LLM_STATUS_COLOR.FAIL)}>{p.status}</span>
                  </td>
                  <td className="py-2 pr-4 font-mono text-gray-300">{p.latency_ms != null ? `${p.latency_ms} ms` : '—'}</td>
                  <td className="py-2 pr-4 font-mono text-gray-300">
                    {usage ? `${usage.tokens.toLocaleString()} tok / ${usage.requests} req` : '—'}
                  </td>
                  <td className="py-2 text-xs text-gray-400 break-all">{p.error ?? ''}</td>
                </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {(llmHealth?.chains ?? []).length > 0 && (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-gray-400 border-b border-gray-800">
                <th className="pb-2 pr-4">Gate chain</th><th className="pb-2 pr-4">Status</th>
                <th className="pb-2 pr-4">Answered by</th><th className="pb-2">Detail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {(llmHealth?.chains ?? []).map((c) => (
                <tr key={c.chain}>
                  <td className="py-2 pr-4 font-mono text-white">{c.chain}</td>
                  <td className="py-2 pr-4">
                    <span className={cn('px-2 py-0.5 rounded text-xs font-semibold', LLM_STATUS_COLOR[c.status] ?? 'text-gray-400 bg-gray-500/20')}>{c.status}</span>
                  </td>
                  <td className="py-2 pr-4 font-mono text-gray-300">{c.answered_by ?? '—'}</td>
                  <td className="py-2 text-xs text-gray-400 break-all">{c.error ?? ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Gap Risk table */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <h3 className="font-semibold text-white flex items-center gap-2 mb-4">
          <Shield className="w-5 h-5 text-orange-400" />
          Gap Risk — Open CNC Positions
        </h3>
        {gapList.length === 0 ? (
          <div className="flex items-center gap-2 text-gray-400 text-sm py-4">
            <CheckCircle2 className="w-4 h-4 text-green-400" />
            No open positions or gap risk not yet evaluated (runs Fri 14:30 IST)
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-gray-400 border-b border-gray-800">
                <th className="pb-3 pr-4">Symbol</th>
                <th className="pb-3 pr-4">Action</th>
                <th className="pb-3 pr-4">Gap Risk</th>
                <th className="pb-3">Reason</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {gapList.map((g) => (
                <tr key={g.symbol}>
                  <td className="py-3 pr-4 font-mono font-semibold text-white">{g.symbol.split(':').pop()}</td>
                  <td className="py-3 pr-4">
                    <span className={cn('px-2 py-1 rounded text-xs font-semibold', ACTION_COLOR[g.action] ?? 'text-gray-400 bg-gray-500/20')}>
                      {g.action}
                    </span>
                  </td>
                  <td className="py-3 pr-4 font-mono">{(g.gap_risk_pct * 100).toFixed(2)}%</td>
                  <td className="py-3 text-gray-400 text-xs">{g.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Edge Decay table */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <h3 className="font-semibold text-white flex items-center gap-2 mb-4">
          <TrendingDown className="w-5 h-5 text-purple-400" />
          Strategy Edge Decay
        </h3>
        {decayEntries.length === 0 ? (
          <p className="text-gray-400 text-sm py-4">No decay data yet — requires 40+ closed trades per strategy</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-gray-400 border-b border-gray-800">
                <th className="pb-3 pr-4">Strategy</th>
                <th className="pb-3 pr-4">Verdict</th>
                <th className="pb-3">Reason</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {decayEntries.map(([name, v]) => (
                <tr key={name}>
                  <td className="py-3 pr-4 font-mono text-white">{name}</td>
                  <td className="py-3 pr-4">
                    <span className={cn('font-semibold text-xs', VERDICT_COLOR[v.verdict] ?? 'text-gray-400')}>
                      {v.verdict.replace(/_/g, ' ')}
                    </span>
                  </td>
                  <td className="py-3 text-gray-400 text-xs">{v.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
