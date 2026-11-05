import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Database,
  HardDrive,
  RefreshCw,
  Server,
  Wifi,
  WifiOff,
} from 'lucide-react';
import { useAdvancedStore } from '../store/advancedStore';
import { cn } from '../utils/cn';
import { format, formatDistanceToNow } from 'date-fns';

export function AdvancedSelfHealing() {
  const {
    reconciliationResults,
    webSocketHealth,
    processHealth,
    memoryPressureEvents,
  } = useAdvancedStore();

  // No local polling timer here: App.tsx drives refreshData()/refreshAdvancedData()
  // globally for every tab. A second timer on top of that one only added
  // duplicate in-flight requests to an API that was already saturated.

  const latestRecon = reconciliationResults[0];
  const healthyProcesses = processHealth.filter((p) => p.status === 'RUNNING').length;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Advanced Self-Healing</h2>
          <p className="text-gray-400">System health monitoring and auto-recovery</p>
        </div>
        <div className={cn(
          'flex items-center gap-2 px-4 py-2 rounded-full text-sm font-medium',
          webSocketHealth.status === 'CONNECTED'
            ? 'bg-green-500/20 text-green-400 border border-green-500/30'
            : 'bg-red-500/20 text-red-400 border border-red-500/30'
        )}>
          {webSocketHealth.status === 'CONNECTED' ? (
            <Wifi className="w-4 h-4" />
          ) : (
            <WifiOff className="w-4 h-4" />
          )}
          {webSocketHealth.status}
        </div>
      </div>

      {/* Live Stats */}
      <div className="grid grid-cols-5 gap-4">
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-green-500/20 flex items-center justify-center">
              <Server className="w-5 h-5 text-green-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Processes</div>
              <div className="text-xl font-bold text-green-400">
                {healthyProcesses}/{processHealth.length}
              </div>
            </div>
          </div>
        </div>
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center">
              <Activity className="w-5 h-5 text-blue-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Ticks/sec</div>
              <div className="text-xl font-bold font-mono">{webSocketHealth.ticksPerSecond}</div>
            </div>
          </div>
        </div>
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-purple-500/20 flex items-center justify-center">
              <Clock className="w-5 h-5 text-purple-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">WS Latency</div>
              <div className="text-xl font-bold font-mono">{webSocketHealth.latencyMs.toFixed(1)}ms</div>
            </div>
          </div>
        </div>
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-cyan-500/20 flex items-center justify-center">
              <Database className="w-5 h-5 text-cyan-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Last Recon</div>
              <div className="text-sm font-mono text-green-400">
                {latestRecon?.duration}ms
              </div>
            </div>
          </div>
        </div>
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-yellow-500/20 flex items-center justify-center">
              <RefreshCw className="w-5 h-5 text-yellow-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Reconnects</div>
              <div className="text-xl font-bold font-mono">{webSocketHealth.reconnectCount}</div>
            </div>
          </div>
        </div>
      </div>

      {/* WebSocket Health */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-4">
        <h3 className="font-semibold text-white flex items-center gap-2 mb-4">
          <Wifi className="w-5 h-5 text-green-400" />
          WebSocket Health Monitor
        </h3>
        <div className="grid grid-cols-4 gap-4">
          <div className="bg-[#0d1117] rounded-lg p-3">
            <div className="text-xs text-gray-400">Status</div>
            <div className={cn(
              'text-lg font-semibold',
              webSocketHealth.status === 'CONNECTED' ? 'text-green-400' : 'text-red-400'
            )}>
              {webSocketHealth.status}
            </div>
          </div>
          <div className="bg-[#0d1117] rounded-lg p-3">
            <div className="text-xs text-gray-400">Subscribed</div>
            <div className="text-lg font-mono">{webSocketHealth.subscribedSymbols}</div>
          </div>
          <div className="bg-[#0d1117] rounded-lg p-3">
            <div className="text-xs text-gray-400">Active</div>
            <div className="text-lg font-mono text-green-400">{webSocketHealth.activeSymbols}</div>
          </div>
          <div className="bg-[#0d1117] rounded-lg p-3">
            <div className="text-xs text-gray-400">Stale</div>
            <div className={cn(
              'text-lg font-mono',
              webSocketHealth.staleSymbols.length > 0 ? 'text-orange-400' : 'text-green-400'
            )}>
              {webSocketHealth.staleSymbols.length}
            </div>
          </div>
        </div>
        {webSocketHealth.staleSymbols.length > 0 && (
          <div className="mt-4 p-3 bg-orange-500/10 border border-orange-500/30 rounded-lg">
            <div className="text-sm text-orange-400 flex items-center gap-2">
              <AlertTriangle className="w-4 h-4" />
              Stale symbols (no ticks &gt;10s): {webSocketHealth.staleSymbols.join(', ')}
            </div>
          </div>
        )}
      </div>

      {/* Process Health */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
        <div className="p-4 border-b border-gray-800">
          <h3 className="font-semibold text-white flex items-center gap-2">
            <Server className="w-5 h-5 text-blue-400" />
            Process Health Monitor
          </h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="bg-[#0d1117] text-xs text-gray-400">
                <th className="text-left p-3">Process</th>
                <th className="text-left p-3">Status</th>
                <th className="text-right p-3">PID</th>
                <th className="text-right p-3">CPU</th>
                <th className="text-right p-3">Memory</th>
                <th className="text-right p-3">Restarts</th>
                <th className="text-right p-3">Errors 24h</th>
                <th className="text-right p-3">GC Pauses</th>
                <th className="text-left p-3">Heartbeat</th>
              </tr>
            </thead>
            <tbody>
              {processHealth.map((proc) => (
                <tr key={proc.name} className="border-t border-gray-800 hover:bg-gray-800/50">
                  <td className="p-3 font-semibold text-white">{proc.name}</td>
                  <td className="p-3">
                    <span className={cn(
                      'flex items-center gap-1 text-xs px-2 py-0.5 rounded w-fit',
                      proc.status === 'RUNNING' ? 'bg-green-500/20 text-green-400' :
                      proc.status === 'IDLE' ? 'bg-yellow-500/20 text-yellow-400' :
                      'bg-red-500/20 text-red-400'
                    )}>
                      {proc.status === 'RUNNING' && <CheckCircle2 className="w-3 h-3" />}
                      {proc.status}
                    </span>
                  </td>
                  <td className="p-3 text-right font-mono text-sm">{proc.pid}</td>
                  <td className="p-3 text-right">
                    <span className={cn(
                      'font-mono text-sm',
                      proc.cpu > 20 ? 'text-orange-400' : proc.cpu > 10 ? 'text-yellow-400' : 'text-green-400'
                    )}>
                      {proc.cpu.toFixed(1)}%
                    </span>
                  </td>
                  <td className="p-3 text-right">
                    <div className="flex items-center justify-end gap-2">
                      <div className="w-16 bg-gray-700 rounded-full h-2">
                        <div
                          className={cn(
                            'h-2 rounded-full',
                            proc.memory / proc.memoryLimit > 0.8 ? 'bg-red-500' :
                            proc.memory / proc.memoryLimit > 0.6 ? 'bg-yellow-500' : 'bg-green-500'
                          )}
                          style={{ width: `${(proc.memory / proc.memoryLimit) * 100}%` }}
                        />
                      </div>
                      <span className="font-mono text-sm text-gray-300">
                        {proc.memory.toFixed(0)}MB
                      </span>
                    </div>
                  </td>
                  <td className="p-3 text-right">
                    <span className={cn(
                      'font-mono text-sm',
                      proc.restartCount > 0 ? 'text-yellow-400' : 'text-gray-400'
                    )}>
                      {proc.restartCount}
                    </span>
                  </td>
                  <td className="p-3 text-right">
                    <span className={cn(
                      'font-mono text-sm',
                      proc.errors24h > 0 ? 'text-red-400' : 'text-green-400'
                    )}>
                      {proc.errors24h}
                    </span>
                  </td>
                  <td className="p-3 text-right text-sm text-gray-400">
                    {proc.gcPauses} ({proc.gcPauseAvgMs}ms avg)
                  </td>
                  <td className="p-3 text-sm text-gray-400">
                    {formatDistanceToNow(proc.lastHeartbeat)} ago
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Reconciliation History */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
        <div className="p-4 border-b border-gray-800">
          <h3 className="font-semibold text-white flex items-center gap-2">
            <Database className="w-5 h-5 text-cyan-400" />
            Position Reconciliation (every 30s)
          </h3>
        </div>
        <div className="p-4 space-y-3">
          {reconciliationResults.map((recon) => (
            <div
              key={recon.id}
              className={cn(
                'bg-[#0d1117] rounded-lg p-4',
                recon.mismatches.length > 0 ? 'border border-orange-500/30' : ''
              )}
            >
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-4">
                  <span className="text-sm text-gray-400">
                    {format(recon.runAt, 'HH:mm:ss')}
                  </span>
                  <span className={cn(
                    'text-xs px-2 py-0.5 rounded',
                    recon.mismatches.length === 0
                      ? 'bg-green-500/20 text-green-400'
                      : 'bg-orange-500/20 text-orange-400'
                  )}>
                    {recon.mismatches.length === 0 ? 'All Matched' : `${recon.mismatches.length} Mismatch`}
                  </span>
                </div>
                <span className="text-xs text-gray-500">{recon.duration}ms</span>
              </div>
              <div className="grid grid-cols-5 gap-4 text-sm">
                <div>
                  <span className="text-gray-400">DB: </span>
                  <span className="font-mono">{recon.dbPositions}</span>
                </div>
                <div>
                  <span className="text-gray-400">Broker: </span>
                  <span className="font-mono">{recon.brokerPositions}</span>
                </div>
                <div>
                  <span className="text-gray-400">Matched: </span>
                  <span className="font-mono text-green-400">{recon.matched}</span>
                </div>
                <div>
                  <span className="text-gray-400">GTTs: </span>
                  <span className="font-mono">{recon.gttsChecked}</span>
                </div>
                <div>
                  <span className="text-gray-400">Rearmed: </span>
                  <span className="font-mono">{recon.gttsRearmed}</span>
                </div>
              </div>
              {recon.mismatches.length > 0 && (
                <div className="mt-3 space-y-2">
                  {recon.mismatches.map((mm, i) => (
                    <div key={i} className="flex items-center justify-between bg-orange-500/10 rounded p-2">
                      <div className="flex items-center gap-2">
                        <span className="text-xs px-2 py-0.5 rounded bg-orange-500/20 text-orange-400">
                          {mm.type.replace('_', ' ')}
                        </span>
                        <span className="font-mono text-sm">{mm.symbol.split(':').pop()}</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-gray-400">{mm.action}</span>
                        {mm.resolved && <CheckCircle2 className="w-4 h-4 text-green-400" />}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Memory Pressure Events */}
      {memoryPressureEvents.length > 0 && (
        <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
          <div className="p-4 border-b border-gray-800">
            <h3 className="font-semibold text-white flex items-center gap-2">
              <HardDrive className="w-5 h-5 text-purple-400" />
              Memory Pressure Events
            </h3>
          </div>
          <div className="p-4 space-y-3">
            {memoryPressureEvents.map((event) => (
              <div key={event.id} className="flex items-center justify-between bg-[#0d1117] rounded-lg p-3">
                <div className="flex items-center gap-4">
                  <span className="font-medium text-white">{event.process}</span>
                  <span className={cn(
                    'text-xs px-2 py-0.5 rounded',
                    event.action === 'GC_TRIGGERED' ? 'bg-blue-500/20 text-blue-400' :
                    event.action === 'RESTART_INITIATED' ? 'bg-yellow-500/20 text-yellow-400' :
                    'bg-red-500/20 text-red-400'
                  )}>
                    {event.action.replace('_', ' ')}
                  </span>
                </div>
                <div className="flex items-center gap-4">
                  <span className="text-sm text-gray-400">
                    {event.rssBeforeMB}MB → {event.rssAfterMB}MB
                  </span>
                  <span className="text-sm text-green-400">
                    -{event.freedMB}MB
                  </span>
                  <span className="text-xs text-gray-500">
                    {formatDistanceToNow(event.timestamp)} ago
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
