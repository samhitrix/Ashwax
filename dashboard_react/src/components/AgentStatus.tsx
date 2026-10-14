import { useEffect, useState } from 'react';
import {
  Activity,
  Bot,
  CheckCircle2,
  Clock,
  Cpu,
  Database,
  Globe,
  HardDrive,
  Power,
  RefreshCw,
  Server,
  Wifi,
  XCircle,
  Zap,
} from 'lucide-react';
import { useTradingStore } from '../store/tradingStore';
import { useApi } from '../hooks/useApi';
import { cn } from '../utils/cn';
import { formatDistanceToNow, format, isValid } from 'date-fns';

function safeFormat(d: Date | null | undefined, fmt: string): string {
  try {
    if (!d || !isValid(d)) return '—';
    return format(d, fmt);
  } catch {
    return '—';
  }
}

function safeDistance(d: Date | null | undefined): string {
  try {
    if (!d || !isValid(d)) return '—';
    return formatDistanceToNow(d);
  } catch {
    return '—';
  }
}

interface DbStatus { connected: boolean; pool_size: number; idle: number; latency_ms: number | null }
interface LlmSanity { bypass_llm: boolean; pending_signals: number }
interface LlmProviderRow { provider: string; model: string; status: string; latency_ms?: number | null; error?: string }
interface LlmChainRow { chain: string; status: string; primary?: string }
interface LlmHealth { overall?: string; checked_at?: string; providers?: LlmProviderRow[]; chains?: LlmChainRow[] }
interface LlmProviderUsage { tokens: number; requests: number }
interface LlmTokens { providers?: Record<string, LlmProviderUsage> }

interface ComponentInfo {
  name: string;
  status: string;
  pid: number;
  cpu: number;
  memory: number;
  core: string;
}

const API_BASE = 'http://localhost:3000';

export function AgentStatus() {
  const { agentState, kiteConnected } = useTradingStore();
  const { data: dbStatus } = useApi<DbStatus>('/api/db_status', 5000);
  const { data: llmSanity } = useApi<LlmSanity>('/api/llm_sanity', 5000);
  // Same endpoint SystemHealth.tsx's "LLM Models" table reads (src/llm/model_health.py,
  // checked at startup/08:10/16:30 IST) -- 30s poll matches that cadence, no point
  // polling faster than the underlying check ever refreshes.
  const { data: llmHealth } = useApi<LlmHealth>('/api/llm_health', 30000);
  // Per-provider spend so an exhausted free tier is visible here, not just inferred
  // from a failed trade (2026-09-23).
  const { data: llmTokens } = useApi<LlmTokens>('/api/llm_tokens', 30000);
  // This card used to hardcode "Gemini" -- stale the moment config changes which
  // provider actually originates trade ideas (anthropic, since 2026-09-22: "the big
  // decision should go to anthropic"). Read the ACTUAL configured primary from the
  // 'discretionary' chain (src/llm/model_health.py's health check already reports
  // it) instead of assuming, so this card follows config rather than fighting it.
  const primaryProvider =
    llmHealth?.chains?.find((c) => c.chain === 'discretionary')?.primary ?? 'anthropic';
  const primaryUsage = llmTokens?.providers?.[primaryProvider];
  const primaryRows = (llmHealth?.providers ?? []).filter((p) => p.provider === primaryProvider);
  // The Model row used to hardcode "gemini-2.0-flash" regardless of which model was
  // actually configured or reachable. Show the model that's ACTUALLY answering (first
  // OK row); if none are OK, say so instead of implying health that isn't there.
  const primaryActiveModel = primaryRows.find((p) => p.status === 'OK')?.model ?? null;
  const primaryAllDown = primaryRows.length > 0 && primaryActiveModel === null;
  const primaryLabel = primaryProvider.charAt(0).toUpperCase() + primaryProvider.slice(1);
  const [components, setComponents] = useState<ComponentInfo[]>([]);

  useEffect(() => {
    const fetchComponents = () => {
      fetch(`${API_BASE}/api/components`)
        .then((r) => (r.ok ? r.json() : null))
        .then((data: ComponentInfo[] | null) => {
          // Only update on a real array. A 503 (transient DB blip) or network
          // error keeps the last-good grid instead of blanking it to 0 engines.
          if (Array.isArray(data)) setComponents(data);
        })
        .catch((err) => {
          console.error('[ashwax] /api/components failed:', err);
        });
    };
    fetchComponents();
    const id = setInterval(fetchComponents, 5000);
    return () => clearInterval(id);
  }, []);

  const runningCount = components.filter((c) => c.status === 'RUNNING').length;
  const avgCpu =
    components.length > 0
      ? components.reduce((a, c) => a + c.cpu, 0) / components.length
      : 0;
  const totalMemory = components.reduce((a, c) => a + c.memory, 0);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Agent Status</h2>
          <p className="text-gray-400">System health and process monitoring</p>
        </div>
        <div className="flex items-center gap-4">
          <button className="flex items-center gap-2 px-4 py-2 bg-green-500/20 text-green-400 rounded-lg hover:bg-green-500/30 transition-colors">
            <Power className="w-4 h-4" />
            Agent Running
          </button>
          <button className="flex items-center gap-2 px-4 py-2 bg-gray-700 text-gray-300 rounded-lg hover:bg-gray-600 transition-colors">
            <RefreshCw className="w-4 h-4" />
            Restart
          </button>
        </div>
      </div>

      {/* Main Status Card */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-6">
            <div className={cn(
              'w-20 h-20 rounded-2xl flex items-center justify-center',
              agentState.isActive ? 'bg-green-500/20' : 'bg-red-500/20'
            )}>
              <Bot className={cn(
                'w-10 h-10',
                agentState.isActive ? 'text-green-400' : 'text-red-400'
              )} />
            </div>
            <div>
              <h3 className="text-2xl font-bold text-white">AshwaX</h3>
              <p className="text-gray-400">Autonomous Trading System</p>
              <div className="flex items-center gap-4 mt-2">
                <span className={cn(
                  'px-3 py-1 rounded-full text-sm font-medium',
                  agentState.isActive
                    ? 'bg-green-500/20 text-green-400'
                    : 'bg-red-500/20 text-red-400'
                )}>
                  {agentState.isActive ? 'Active' : 'Stopped'}
                </span>
                {(['intraday', 'swing', 'options'] as const).map((seg) => (
                  <span
                    key={seg}
                    className={cn(
                      'px-3 py-1 rounded-full text-sm font-medium capitalize',
                      agentState.segments[seg] === 'LIVE'
                        ? 'bg-red-500/20 text-red-400'
                        : 'bg-blue-500/20 text-blue-400'
                    )}
                    title={`${seg} segment`}
                  >
                    {seg} · {agentState.segments[seg]}
                  </span>
                ))}
                <span className="px-3 py-1 rounded-full text-sm font-medium bg-purple-500/20 text-purple-400">
                  {agentState.autonomyLevel}
                </span>
              </div>
            </div>
          </div>

          <div className="text-right space-y-2">
            <div className="text-sm text-gray-400">Last Heartbeat</div>
            <div className="font-mono text-lg text-white">
              {safeFormat(agentState.lastHeartbeat, 'HH:mm:ss')}
            </div>
            <div className="text-xs text-gray-500">
              {safeDistance(agentState.lastHeartbeat)} ago
            </div>
          </div>
        </div>

        <div className="mt-6 pt-6 border-t border-gray-800">
          <div className="text-sm text-gray-400 mb-2">Current Task</div>
          <div className="flex items-center gap-2 text-white">
            <Activity className="w-5 h-5 text-blue-400 animate-pulse" />
            {agentState.currentTask}
          </div>
        </div>
      </div>

      {/* Metrics Grid */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center">
              <Server className="w-5 h-5 text-blue-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Processes</div>
              <div className="text-xl font-bold">
                {runningCount}
                <span className="text-sm text-gray-400 ml-1">/ {components.length}</span>
              </div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-10 h-10 rounded-lg bg-purple-500/20 flex items-center justify-center">
              <Cpu className="w-5 h-5 text-purple-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Avg CPU</div>
              <div className="text-xl font-bold font-mono">
                {avgCpu.toFixed(1)}%
              </div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-10 h-10 rounded-lg bg-cyan-500/20 flex items-center justify-center">
              <HardDrive className="w-5 h-5 text-cyan-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Total Memory</div>
              <div className="text-xl font-bold font-mono">
                {totalMemory.toFixed(0)} MB
              </div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-3">
            <div className={cn(
              'w-10 h-10 rounded-lg flex items-center justify-center',
              agentState.circuitBreakers.connectivity.triggered
                ? 'bg-red-500/20'
                : 'bg-green-500/20'
            )}>
              <Wifi className={cn(
                'w-5 h-5',
                agentState.circuitBreakers.connectivity.triggered
                  ? 'text-red-400'
                  : 'text-green-400'
              )} />
            </div>
            <div>
              <div className="text-xs text-gray-400">Connection</div>
              <div className={cn(
                'text-lg font-semibold',
                agentState.circuitBreakers.connectivity.triggered
                  ? 'text-red-400'
                  : 'text-green-400'
              )}>
                {agentState.circuitBreakers.connectivity.triggered ? 'Offline' : 'Online'}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Process Table */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
        <div className="p-4 border-b border-gray-800">
          <h3 className="font-semibold text-white flex items-center gap-2">
            <Server className="w-5 h-5 text-blue-400" />
            Process Monitor
          </h3>
        </div>
        {components.length === 0 ? (
          <div className="p-8 text-center text-gray-500">
            <Server className="w-10 h-10 mx-auto mb-3 opacity-30" />
            <p className="text-sm">Trading engine not running.</p>
            <p className="text-xs mt-1">Start the orchestrator to see live process data.</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="bg-[#0d1117] text-xs text-gray-400">
                  <th className="text-left p-3">Process</th>
                  <th className="text-left p-3">Status</th>
                  <th className="text-right p-3">PID</th>
                  <th className="text-right p-3">Core</th>
                  <th className="text-right p-3">CPU %</th>
                  <th className="text-right p-3">Memory</th>
                  <th className="text-right p-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {components.map((comp) => (
                  <tr key={comp.name} className="border-t border-gray-800 hover:bg-gray-800/50">
                    <td className="p-3">
                      <span className="font-medium text-white">{comp.name}</span>
                    </td>
                    <td className="p-3">
                      <span className={cn(
                        'flex items-center gap-1 text-sm',
                        comp.status === 'RUNNING' ? 'text-green-400' :
                        comp.status === 'IDLE' ? 'text-yellow-400' : 'text-red-400'
                      )}>
                        {comp.status === 'RUNNING' ? (
                          <CheckCircle2 className="w-4 h-4" />
                        ) : comp.status === 'IDLE' ? (
                          <Clock className="w-4 h-4" />
                        ) : (
                          <XCircle className="w-4 h-4" />
                        )}
                        {comp.status}
                      </span>
                    </td>
                    <td className="p-3 text-right font-mono text-sm">{comp.pid}</td>
                    <td className="p-3 text-right">
                      <span className="px-2 py-0.5 rounded bg-gray-700 text-gray-300 text-xs">
                        {comp.core}
                      </span>
                    </td>
                    <td className="p-3 text-right">
                      <span className={cn(
                        'font-mono text-sm',
                        comp.cpu > 10 ? 'text-yellow-400' : 'text-green-400'
                      )}>
                        {comp.cpu.toFixed(1)}%
                      </span>
                    </td>
                    <td className="p-3 text-right font-mono text-sm">{comp.memory.toFixed(1)} MB</td>
                    <td className="p-3 text-right">
                      <button className="text-xs px-2 py-1 rounded bg-gray-700 text-gray-300 hover:bg-gray-600">
                        Restart
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Connections Status */}
      <div className="grid grid-cols-3 gap-4">
        <div className="bg-[#161b22] rounded-xl border border-gray-800 p-4">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 rounded-lg bg-green-500/20 flex items-center justify-center">
              <Globe className="w-5 h-5 text-green-400" />
            </div>
            <div>
              <h4 className="font-semibold text-white">Kite Connect</h4>
              <p className="text-xs text-gray-400">Broker API</p>
            </div>
          </div>
          <div className="space-y-2">
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">REST API</span>
              {kiteConnected ? (
                <span className="text-green-400 flex items-center gap-1">
                  <CheckCircle2 className="w-4 h-4" />
                  Connected
                </span>
              ) : (
                <span className="text-red-400 flex items-center gap-1">
                  <XCircle className="w-4 h-4" />
                  Not connected
                </span>
              )}
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">Access token</span>
              <span className={kiteConnected ? 'text-green-400' : 'text-red-400'}>
                {kiteConnected ? 'Valid' : 'Invalid / missing'}
              </span>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl border border-gray-800 p-4">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center">
              <Database className="w-5 h-5 text-blue-400" />
            </div>
            <div>
              <h4 className="font-semibold text-white">PostgreSQL</h4>
              <p className="text-xs text-gray-400">Database</p>
            </div>
          </div>
          <div className="space-y-2">
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">Status</span>
              <span className={dbStatus?.connected ? 'text-green-400 flex items-center gap-1' : 'text-red-400 flex items-center gap-1'}>
                {dbStatus?.connected ? <CheckCircle2 className="w-4 h-4" /> : <XCircle className="w-4 h-4" />}
                {dbStatus?.connected ? 'Connected' : 'Disconnected'}
              </span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">Pool (idle/total)</span>
              <span className="font-mono text-gray-300">
                {dbStatus ? `${dbStatus.idle}/${dbStatus.pool_size}` : '—'}
              </span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">Latency</span>
              <span className="font-mono text-gray-300">
                {dbStatus?.latency_ms != null ? `${dbStatus.latency_ms} ms` : '—'}
              </span>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl border border-gray-800 p-4">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 rounded-lg bg-purple-500/20 flex items-center justify-center">
              <Zap className="w-5 h-5 text-purple-400" />
            </div>
            <div>
              <h4 className="font-semibold text-white">{primaryLabel} LLM</h4>
              <p className="text-xs text-gray-400">AI Provider (origination primary)</p>
            </div>
          </div>
          <div className="space-y-2">
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">Model</span>
              <span className={cn('font-mono', primaryAllDown ? 'text-red-400' : 'text-gray-300')}>
                {primaryActiveModel ?? (primaryAllDown ? 'none available' : '—')}
              </span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">Model Health</span>
              <span className={primaryAllDown ? 'text-red-400' : 'text-green-400'}>
                {primaryRows.length === 0
                  ? '—'
                  : primaryAllDown
                    ? 'DOWN'
                    : (() => {
                        const downCount = primaryRows.filter((p) => p.status !== 'OK').length;
                        return downCount > 0 ? `OK (${downCount} fallback down)` : 'OK';
                      })()}
              </span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">Gate Status</span>
              <span className={llmSanity?.bypass_llm ? 'text-orange-400' : 'text-green-400'}>
                {llmSanity?.bypass_llm ? 'Bypassed' : 'Active'}
              </span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">Pending Signals</span>
              <span className="font-mono text-gray-300">
                {llmSanity?.pending_signals ?? '—'}
              </span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">Tokens (today)</span>
              <span className="font-mono text-gray-300">
                {primaryUsage
                  ? `${primaryUsage.tokens.toLocaleString()} tok / ${primaryUsage.requests} req`
                  : '—'}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
