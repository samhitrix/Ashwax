import { useState } from 'react';
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Eye,
  RefreshCw,
  Shield,
  TrendingDown,
  TrendingUp,
  Zap,
} from 'lucide-react';
import { useAdvancedStore } from '../store/advancedStore';
import { useModeFilterStore } from '../store/modeFilterStore';
import { ModeToggle } from './ModeToggle';
import { cn } from '../utils/cn';
import { format, formatDistanceToNow } from 'date-fns';

const sym = (s: string) => s.split(':').pop();

export function AdvancedOrderMonitor() {
  const {
    orderMonitoringState,
    breachEvents,
    gttOrders,
    partialFills,
    openPositions: allOpenPositions,
    closedPositions: allClosedPositions,
    swingPositions: allSwingPositions,
    resolveBreachEvent,
  } = useAdvancedStore();

  const [posTab, setPosTab] = useState<'open' | 'closed'>('open');
  const { modeFilter } = useModeFilterStore();

  // No local polling timer here: App.tsx drives refreshData()/refreshAdvancedData()
  // globally for every tab. A second timer on top of that one only added
  // duplicate in-flight requests to an API that was already saturated.

  // GTT orders and partial fills are inherently live-only (paper never places
  // real GTTs or reaches the broker's partial-fill states) — so the Paper view
  // must show none of them, not the unfiltered live list.
  const wantPaper = modeFilter === 'paper';
  const byMode = <T extends { paper: boolean }>(rows: T[]) =>
    modeFilter === 'all' ? rows : rows.filter((r) => r.paper === wantPaper);

  const openPositions = byMode(allOpenPositions);
  const closedPositions = byMode(allClosedPositions);
  const swingPositions = byMode(allSwingPositions);
  const visibleGttOrders = wantPaper ? [] : gttOrders;
  const visiblePartialFills = wantPaper ? [] : partialFills;

  const openPnl = openPositions.reduce((s, p) => s + p.pnl, 0);
  const closedPnl = closedPositions.reduce((s, p) => s + p.pnl, 0);
  const combinedPnl = openPnl + closedPnl;
  const swingPnl = swingPositions.reduce((s, p) => s + (p.pnl ?? 0), 0);
  const swingBookedPnl = swingPositions.reduce((s, p) => s + (p.partialPnlBooked ?? 0), 0);
  const closedWins = closedPositions.filter((p) => p.pnl > 0).length;
  const closedWinRate = closedPositions.length
    ? Math.round((closedWins / closedPositions.length) * 100)
    : 0;

  const activeBreaches = breachEvents.filter((e) => !e.resolvedAt);
  const resolvedBreaches = breachEvents.filter((e) => e.resolvedAt);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Advanced Order Monitor</h2>
          <p className="text-gray-400">Real-time order tracking with auto-actions</p>
        </div>
        <div className="flex items-center gap-3">
          <ModeToggle />
          <div className={cn(
            'flex items-center gap-2 px-4 py-2 rounded-full text-sm font-medium',
            orderMonitoringState.isActive
              ? 'bg-green-500/20 text-green-400 border border-green-500/30'
              : 'bg-red-500/20 text-red-400 border border-red-500/30'
          )}>
            <div className={cn(
              'w-2 h-2 rounded-full',
              orderMonitoringState.isActive ? 'bg-green-400 animate-pulse' : 'bg-red-400'
            )} />
            Monitor {orderMonitoringState.isActive ? 'Active' : 'Stopped'}
          </div>
        </div>
      </div>

      {/* Live Stats */}
      <div className="grid grid-cols-6 gap-4">
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center">
              <RefreshCw className="w-5 h-5 text-blue-400 animate-spin" style={{ animationDuration: '3s' }} />
            </div>
            <div>
              <div className="text-xs text-gray-400">Check Interval</div>
              <div className="text-lg font-mono font-bold">{orderMonitoringState.checkIntervalMs}ms</div>
            </div>
          </div>
        </div>
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-green-500/20 flex items-center justify-center">
              <Activity className="w-5 h-5 text-green-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Checks/Min</div>
              <div className="text-lg font-mono font-bold">{orderMonitoringState.checksPerMinute}</div>
            </div>
          </div>
        </div>
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-purple-500/20 flex items-center justify-center">
              <Clock className="w-5 h-5 text-purple-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Avg Latency</div>
              <div className="text-lg font-mono font-bold">{orderMonitoringState.avgLatencyMs}ms</div>
            </div>
          </div>
        </div>
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-cyan-500/20 flex items-center justify-center">
              <Eye className="w-5 h-5 text-cyan-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Orders Watched</div>
              <div className="text-lg font-mono font-bold">{orderMonitoringState.ordersMonitored}</div>
            </div>
          </div>
        </div>
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-orange-500/20 flex items-center justify-center">
              <AlertTriangle className="w-5 h-5 text-orange-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Breaches</div>
              <div className="text-lg font-mono font-bold text-orange-400">{orderMonitoringState.breachesDetected}</div>
            </div>
          </div>
        </div>
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-green-500/20 flex items-center justify-center">
              <Zap className="w-5 h-5 text-green-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Auto-Actions</div>
              <div className="text-lg font-mono font-bold text-green-400">{orderMonitoringState.autoActionsExecuted}</div>
            </div>
          </div>
        </div>
      </div>

      {/* Active Breach Alerts */}
      {activeBreaches.length > 0 && (
        <div className="space-y-3">
          <h3 className="font-semibold text-red-400 flex items-center gap-2">
            <AlertCircle className="w-5 h-5 animate-pulse" />
            Active Breach Alerts ({activeBreaches.length})
          </h3>
          {activeBreaches.map((breach) => (
            <div
              key={breach.id}
              className={cn(
                'bg-[#161b22] rounded-xl border p-4',
                breach.breachType.includes('BREACH')
                  ? 'border-red-500/50 ring-2 ring-red-500/20'
                  : 'border-orange-500/50'
              )}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-4">
                  <div className={cn(
                    'w-12 h-12 rounded-lg flex items-center justify-center',
                    breach.breachType.includes('TARGET')
                      ? 'bg-green-500/20'
                      : 'bg-red-500/20'
                  )}>
                    {breach.breachType.includes('TARGET') ? (
                      <TrendingUp className="w-6 h-6 text-green-400" />
                    ) : (
                      <TrendingDown className="w-6 h-6 text-red-400" />
                    )}
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-white">{sym(breach.symbol)}</span>
                      <span className={cn(
                        'text-xs px-2 py-0.5 rounded',
                        breach.breachType.includes('BREACH')
                          ? 'bg-red-500/20 text-red-400'
                          : 'bg-orange-500/20 text-orange-400'
                      )}>
                        {breach.breachType.replace('_', ' ')}
                      </span>
                      <span className="text-xs px-2 py-0.5 rounded bg-gray-700 text-gray-300">
                        {breach.orderType}
                      </span>
                    </div>
                    <div className="text-sm text-gray-400 mt-1">
                      LTP: <span className="font-mono text-white">₹{breach.ltp.toFixed(2)}</span>
                      {' → '}
                      Expected: <span className="font-mono text-white">₹{breach.expectedPrice.toFixed(2)}</span>
                      {' '}
                      <span className={cn(
                        'font-mono',
                        breach.gapPercent > 0 ? 'text-green-400' : 'text-red-400'
                      )}>
                        ({breach.gapPercent > 0 ? '+' : ''}{breach.gapPercent.toFixed(2)}%)
                      </span>
                    </div>
                    <div className="text-xs text-gray-500 mt-1">
                      Detected {formatDistanceToNow(breach.detectedAt)} ago
                    </div>
                  </div>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => resolveBreachEvent(breach.positionId, 'MARKET_EXIT')}
                    disabled={breach.actionResult === 'PENDING'}
                    className="flex items-center gap-2 px-4 py-2 bg-red-500 text-white rounded-lg text-sm font-medium hover:bg-red-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <Zap className="w-4 h-4" />
                    {breach.actionResult === 'PENDING' ? 'Exiting…' : 'Market Exit'}
                  </button>
                  <button
                    disabled
                    title="Not wired yet — needs a price-input UI to set the new SL/target before this can do anything real."
                    className="flex items-center gap-2 px-4 py-2 bg-yellow-500/10 text-yellow-400/50 rounded-lg text-sm cursor-not-allowed"
                  >
                    Modify Order
                  </button>
                </div>
                {breach.actionResult === 'FAILED' && (
                  <div className="text-xs text-red-400 mt-2">
                    Exit request failed — retry or check the backend.
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Positions */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
        <div className="flex items-center justify-between border-b border-gray-800">
          <div className="flex">
            <button
              onClick={() => setPosTab('open')}
              className={cn(
                'flex items-center gap-2 px-6 py-4 text-sm font-medium border-b-2 transition-colors',
                posTab === 'open'
                  ? 'border-blue-500 text-white'
                  : 'border-transparent text-gray-400 hover:text-gray-200'
              )}
            >
              <Eye className="w-4 h-4" />
              Open Positions ({openPositions.length})
              {openPositions.length > 0 && (
                <span className={cn('font-mono', openPnl >= 0 ? 'text-green-400' : 'text-red-400')}>
                  {openPnl >= 0 ? '+' : ''}₹{openPnl.toFixed(2)}
                </span>
              )}
            </button>
            <button
              onClick={() => setPosTab('closed')}
              className={cn(
                'flex items-center gap-2 px-6 py-4 text-sm font-medium border-b-2 transition-colors',
                posTab === 'closed'
                  ? 'border-blue-500 text-white'
                  : 'border-transparent text-gray-400 hover:text-gray-200'
              )}
            >
              <CheckCircle2 className="w-4 h-4" />
              Closed Positions ({closedPositions.length})
              {closedPositions.length > 0 && (
                <span className="text-gray-400">{closedWinRate}% WR</span>
              )}
            </button>
          </div>
          {(openPositions.length > 0 || closedPositions.length > 0) && (
            <div className="text-right pr-6">
              <div className="text-xs text-gray-500 uppercase tracking-wide">Total P&amp;L</div>
              <div className={cn(
                'text-lg font-mono font-semibold',
                combinedPnl >= 0 ? 'text-green-400' : 'text-red-400'
              )}>
                {combinedPnl >= 0 ? '+' : ''}₹{combinedPnl.toFixed(2)}
              </div>
            </div>
          )}
        </div>

        {posTab === 'open' ? (
          <div className="overflow-x-auto">
            {openPositions.length === 0 ? (
              <div className="p-8 text-center text-gray-500 text-sm">No open positions</div>
            ) : (
              <table className="w-full">
                <thead>
                  <tr className="bg-[#0d1117] text-xs font-bold text-gray-300">
                    <th className="text-left p-3">Symbol</th>
                    <th className="text-left p-3">Direction</th>
                    <th className="text-left p-3">Type</th>
                    <th className="text-right p-3">Qty</th>
                    <th className="text-right p-3">Entry</th>
                    <th className="text-right p-3">LTP</th>
                    <th className="text-right p-3">Target</th>
                    <th className="text-right p-3">SL</th>
                    <th className="text-right p-3">P&amp;L</th>
                    <th className="text-left p-3">Strategy</th>
                    <th className="text-left p-3">Entry Time</th>
                  </tr>
                </thead>
                <tbody className="font-semibold">
                  {openPositions.map((p) => (
                    <tr key={p.symbol} className="border-t border-gray-800 hover:bg-gray-800/50">
                      <td className="p-3 font-semibold text-white">
                        {sym(p.symbol)}
                        {p.paper && (
                          <span className="ml-2 text-xs px-2 py-0.5 rounded bg-yellow-900 text-yellow-300 border border-yellow-700">
                            PAPER
                          </span>
                        )}
                        {p.external && (
                          <span className="ml-2 text-xs px-2 py-0.5 rounded bg-gray-700 text-gray-400">
                            EXTERNAL
                          </span>
                        )}
                      </td>
                      <td className="p-3">
                        <span className={cn(
                          'flex items-center gap-1 text-xs',
                          p.direction === 'LONG' ? 'text-green-400' : 'text-red-400'
                        )}>
                          {p.direction === 'LONG' ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                          {p.direction}
                        </span>
                      </td>
                      <td className="p-3 text-xs text-gray-200">{p.product || '-'}</td>
                      <td className="p-3 text-right font-mono">{p.quantity}</td>
                      <td className="p-3 text-right font-mono">₹{p.entryPrice.toFixed(2)}</td>
                      <td className="p-3 text-right font-mono">₹{p.ltp.toFixed(2)}</td>
                      <td className="p-3 text-right font-mono text-green-400">
                        {p.target ? `₹${p.target.toFixed(2)}` : '—'}
                      </td>
                      <td className="p-3 text-right font-mono text-red-400">
                        {p.stopLoss ? `₹${p.stopLoss.toFixed(2)}` : '—'}
                      </td>
                      <td className={cn(
                        'p-3 text-right font-mono',
                        p.pnl >= 0 ? 'text-green-400' : 'text-red-400'
                      )}>
                        {p.pnl >= 0 ? '+' : ''}₹{p.pnl.toFixed(2)}
                      </td>
                      <td className="p-3 text-xs text-gray-200">{p.strategy || '—'}</td>
                      <td className="p-3 text-sm text-gray-200">
                        {p.entryTime ? new Date(p.entryTime).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ) : (
          <div className="overflow-x-auto">
            {closedPositions.length === 0 ? (
              <div className="p-8 text-center text-gray-500 text-sm">No closed positions</div>
            ) : (
              <table className="w-full">
                <thead>
                  <tr className="bg-[#0d1117] text-xs font-bold text-gray-300">
                    <th className="text-left p-3">Symbol</th>
                    <th className="text-left p-3">Direction</th>
                    <th className="text-right p-3">Qty</th>
                    <th className="text-right p-3">Entry</th>
                    <th className="text-right p-3">Exit</th>
                    <th className="text-right p-3">P&amp;L</th>
                    <th className="text-left p-3">Strategy</th>
                    <th className="text-left p-3">Entry Time</th>
                    <th className="text-left p-3">Exit Time</th>
                  </tr>
                </thead>
                <tbody className="font-semibold">
                  {closedPositions.map((p, i) => (
                    <tr key={`${p.symbol}-${i}`} className="border-t border-gray-800 hover:bg-gray-800/50">
                      <td className="p-3 font-semibold text-white">
                        {sym(p.symbol)}
                        {p.paper && (
                          <span className="ml-2 text-xs px-2 py-0.5 rounded bg-yellow-900 text-yellow-300 border border-yellow-700">
                            PAPER
                          </span>
                        )}
                      </td>
                      <td className="p-3">
                        <span className={cn(
                          'text-xs',
                          p.direction === 'LONG' ? 'text-green-400' : 'text-red-400'
                        )}>
                          {p.direction}
                        </span>
                      </td>
                      <td className="p-3 text-right font-mono">{p.quantity}</td>
                      <td className="p-3 text-right font-mono">₹{p.entryPrice.toFixed(2)}</td>
                      <td className="p-3 text-right font-mono">₹{p.exitPrice.toFixed(2)}</td>
                      <td className={cn(
                        'p-3 text-right font-mono',
                        p.pnl >= 0 ? 'text-green-400' : 'text-red-400'
                      )}>
                        {p.pnl >= 0 ? '+' : ''}₹{p.pnl.toFixed(2)}
                      </td>
                      <td className="p-3 text-sm text-gray-200">{p.strategy || '-'}</td>
                      <td className="p-3 text-sm text-gray-200">
                        {p.entryAt ? new Date(p.entryAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '-'}
                      </td>
                      <td className="p-3 text-sm text-gray-200">
                        {p.closedAt ? new Date(p.closedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>

      {/* Swing Positions — CNC/delivery, held multi-day. Only shown while at
          least one is open; hidden entirely otherwise. */}
      {swingPositions.length > 0 && (
        <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
          <div className="p-4 border-b border-gray-800 flex items-center justify-between">
            <div>
              <h3 className="font-semibold text-white flex items-center gap-2">
                <TrendingUp className="w-5 h-5 text-purple-400" />
                Swing Positions ({swingPositions.length})
              </h3>
              <p className="text-sm text-gray-400 mt-1">
                CNC/delivery trades — held overnight, exit via their own SL/target/CRM path
              </p>
            </div>
            <div className="flex items-center gap-6">
              <div className="text-right">
                <div className="text-xs text-gray-500 uppercase tracking-wide">Booked P&amp;L</div>
                <div className={cn(
                  'text-lg font-mono font-semibold',
                  swingBookedPnl >= 0 ? 'text-green-400' : 'text-red-400'
                )}>
                  {swingBookedPnl >= 0 ? '+' : ''}₹{swingBookedPnl.toFixed(2)}
                </div>
              </div>
              <div className="text-right">
                <div className="text-xs text-gray-500 uppercase tracking-wide">Total P&amp;L</div>
                <div className={cn(
                  'text-lg font-mono font-semibold',
                  swingPnl >= 0 ? 'text-green-400' : 'text-red-400'
                )}>
                  {swingPnl >= 0 ? '+' : ''}₹{swingPnl.toFixed(2)}
                </div>
              </div>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-[#0d1117] text-xs font-bold text-gray-300">
                  <th className="text-left p-3">Symbol</th>
                  <th className="text-left p-3">Dir</th>
                  <th className="text-left p-3">Strategy</th>
                  <th className="text-right p-3">Qty</th>
                  <th className="text-right p-3">Entry</th>
                  <th className="text-right p-3">StopLoss</th>
                  <th className="text-right p-3">Target</th>
                  <th className="text-right p-3">To Target</th>
                  <th className="text-right p-3">LTP</th>
                  <th className="text-right p-3">P&amp;L</th>
                  <th className="text-right p-3">Day %Chg</th>
                  <th className="text-right p-3">%Net Change</th>
                  <th className="text-left p-3">Entry Date</th>
                </tr>
              </thead>
              <tbody className="font-semibold">
                {swingPositions.map((p) => (
                  <tr key={p.tradeId} className="border-t border-gray-800 hover:bg-gray-800/50">
                    <td className="p-3 font-semibold text-white">
                      {sym(p.symbol)}
                      {p.paper && (
                        <span className="ml-2 text-xs px-2 py-0.5 rounded bg-yellow-900 text-yellow-300 border border-yellow-700">
                          PAPER
                        </span>
                      )}
                    </td>
                    <td className="p-3">
                      <span className={cn('text-xs', p.direction === 'LONG' ? 'text-green-400' : 'text-red-400')}>
                        {p.direction}
                      </span>
                    </td>
                    <td className="p-3 text-sm text-gray-200">{p.strategyName || '-'}</td>
                    <td className="p-3 text-right font-mono">{p.quantity}</td>
                    <td className="p-3 text-right font-mono">₹{p.entryPrice.toFixed(2)}</td>
                    <td className="p-3 text-right font-mono text-red-400">
                      {p.stopLoss != null ? `₹${p.stopLoss.toFixed(2)}` : '-'}
                      {p.stopLoss != null && p.entryPrice > 0 && (() => {
                        const raw = ((p.stopLoss - p.entryPrice) / p.entryPrice) * 100;
                        const slPct = p.direction === 'SHORT' ? -raw : raw;
                        return (
                          <span className={cn('block text-xs', slPct >= 0 ? 'text-green-400' : 'text-red-400')}>
                            {slPct >= 0 ? '+' : ''}{slPct.toFixed(1)}%
                          </span>
                        );
                      })()}
                    </td>
                    <td className="p-3 text-right font-mono text-green-400">
                      {p.target != null ? `₹${p.target.toFixed(2)}` : '-'}
                      {p.target != null && p.entryPrice > 0 && (() => {
                        const raw = ((p.target - p.entryPrice) / p.entryPrice) * 100;
                        const tgtPct = p.direction === 'SHORT' ? -raw : raw;
                        return (
                          <span className="block text-xs text-green-400">
                            +{tgtPct.toFixed(1)}%
                          </span>
                        );
                      })()}
                    </td>
                    {(() => {
                      const current = p.exitPrice ?? p.ltp;
                      if (current == null || p.target == null || !p.entryPrice) {
                        return <td className="p-3 text-right font-mono text-gray-600">—</td>;
                      }
                      const denom = p.direction === 'SHORT' ? p.entryPrice - p.target : p.target - p.entryPrice;
                      if (!denom) {
                        return <td className="p-3 text-right font-mono text-gray-600">—</td>;
                      }
                      const numer = p.direction === 'SHORT' ? p.entryPrice - current : current - p.entryPrice;
                      const toTarget = (numer / denom) * 100;
                      return (
                        <td className={cn(
                          'p-3 text-right font-mono',
                          toTarget >= 100 ? 'text-green-400' : toTarget >= 0 ? 'text-gray-300' : 'text-red-400'
                        )}>
                          {toTarget.toFixed(0)}%
                        </td>
                      );
                    })()}
                    <td className={cn(
                      'p-3 text-right font-mono font-bold',
                      p.ltp == null ? 'text-gray-600' : (p.pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'
                    )}>
                      {p.ltp != null ? `₹${p.ltp.toFixed(2)}` : '—'}
                    </td>
                    <td className={cn(
                      'p-3 text-right font-mono',
                      p.pnl == null ? 'text-gray-600' : p.pnl >= 0 ? 'text-green-400' : 'text-red-400'
                    )}>
                      {p.pnl != null ? `${p.pnl >= 0 ? '+' : ''}₹${p.pnl.toFixed(2)}` : '—'}
                      {p.partialPnlBooked !== 0 && (
                        <span className="block text-xs text-green-400" title="Already booked via partial exit">
                          ₹{p.partialPnlBooked.toFixed(2)} booked
                        </span>
                      )}
                    </td>
                    <td className={cn(
                      'p-3 text-right font-mono',
                      p.dayChangePct == null ? 'text-gray-600' : p.dayChangePct >= 0 ? 'text-green-400' : 'text-red-400'
                    )}>
                      {p.dayChangePct != null
                        ? `${p.dayChangePct >= 0 ? '+' : ''}${p.dayChangePct.toFixed(2)}%`
                        : '—'}
                    </td>
                    {(() => {
                      const current = p.exitPrice ?? p.ltp;
                      if (current == null || !p.entryPrice) {
                        return <td className="p-3 text-right font-mono text-gray-600">—</td>;
                      }
                      const raw = ((current - p.entryPrice) / p.entryPrice) * 100;
                      const pct = p.direction === 'SHORT' ? -raw : raw;
                      return (
                        <td className={cn('p-3 text-right font-mono', pct >= 0 ? 'text-green-400' : 'text-red-400')}>
                          {`${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`}
                        </td>
                      );
                    })()}
                    <td className="p-3 text-sm text-gray-200">
                      {p.entryTime ? new Date(p.entryTime).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* GTT Orders */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
        <div className="p-4 border-b border-gray-800">
          <h3 className="font-semibold text-white flex items-center gap-2">
            <Shield className="w-5 h-5 text-blue-400" />
            GTT Backup Orders ({visibleGttOrders.length})
          </h3>
          <p className="text-sm text-gray-400 mt-1">
            Good Till Triggered orders as safety net for each position
          </p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="bg-[#0d1117] text-xs text-gray-400">
                <th className="text-left p-3">Symbol</th>
                <th className="text-left p-3">Type</th>
                <th className="text-right p-3">SL Trigger</th>
                <th className="text-right p-3">SL Limit</th>
                <th className="text-right p-3">Target Trigger</th>
                <th className="text-right p-3">Qty</th>
                <th className="text-left p-3">Status</th>
                <th className="text-left p-3">Expires</th>
              </tr>
            </thead>
            <tbody>
              {visibleGttOrders.map((gtt) => (
                <tr key={gtt.id} className="border-t border-gray-800 hover:bg-gray-800/50">
                  <td className="p-3 font-semibold text-white">{sym(gtt.symbol)}</td>
                  <td className="p-3">
                    <span className={cn(
                      'text-xs px-2 py-0.5 rounded',
                      gtt.triggerType === 'OCO'
                        ? 'bg-purple-500/20 text-purple-400'
                        : 'bg-blue-500/20 text-blue-400'
                    )}>
                      {gtt.triggerType}
                    </span>
                  </td>
                  <td className="p-3 text-right font-mono text-red-400">₹{gtt.slTrigger}</td>
                  <td className="p-3 text-right font-mono text-red-300">₹{gtt.slLimit}</td>
                  <td className="p-3 text-right font-mono text-green-400">
                    {gtt.targetTrigger ? `₹${gtt.targetTrigger}` : '-'}
                  </td>
                  <td className="p-3 text-right font-mono">{gtt.quantity}</td>
                  <td className="p-3">
                    <span className={cn(
                      'flex items-center gap-1 text-xs',
                      gtt.status === 'ACTIVE' ? 'text-green-400' : 'text-gray-400'
                    )}>
                      {gtt.status === 'ACTIVE' && <CheckCircle2 className="w-3 h-3" />}
                      {gtt.status}
                    </span>
                  </td>
                  <td className="p-3 text-sm text-gray-400">
                    {format(gtt.expiresAt, 'dd MMM')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Partial Fills */}
      {visiblePartialFills.length > 0 && (
        <div className="bg-[#161b22] rounded-xl border border-yellow-500/30 p-4">
          <h3 className="font-semibold text-yellow-400 flex items-center gap-2 mb-4">
            <Clock className="w-5 h-5" />
            Partial Fills Waiting ({visiblePartialFills.length})
          </h3>
          {visiblePartialFills.map((pf) => (
            <div key={pf.id} className="flex items-center justify-between bg-[#0d1117] rounded-lg p-4">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-bold text-white">{sym(pf.symbol)}</span>
                  <span className="text-xs px-2 py-0.5 rounded bg-yellow-500/20 text-yellow-400">
                    {pf.fillPercent}% filled
                  </span>
                </div>
                <div className="text-sm text-gray-400 mt-1">
                  {pf.filledQuantity}/{pf.totalQuantity} qty • {(pf.elapsedMs / 1000).toFixed(1)}s elapsed
                </div>
                <div className="w-48 bg-gray-700 rounded-full h-2 mt-2">
                  <div
                    className="bg-yellow-500 h-2 rounded-full transition-all"
                    style={{ width: `${pf.fillPercent}%` }}
                  />
                </div>
              </div>
              <div className="flex gap-2">
                <button className="px-3 py-2 bg-blue-500/20 text-blue-400 rounded-lg text-sm hover:bg-blue-500/30">
                  Convert to Market
                </button>
                <button className="px-3 py-2 bg-gray-700 text-gray-300 rounded-lg text-sm hover:bg-gray-600">
                  Wait
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Resolved Events */}
      {resolvedBreaches.length > 0 && (
        <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
          <div className="p-4 border-b border-gray-800">
            <h3 className="font-semibold text-gray-400 flex items-center gap-2">
              <CheckCircle2 className="w-5 h-5 text-green-400" />
              Recently Resolved ({resolvedBreaches.length})
            </h3>
          </div>
          <div className="p-4 space-y-2">
            {resolvedBreaches.map((breach) => (
              <div key={breach.id} className="flex items-center justify-between bg-[#0d1117] rounded-lg p-3">
                <div className="flex items-center gap-3">
                  <CheckCircle2 className="w-5 h-5 text-green-400" />
                  <div>
                    <span className="font-medium text-white">{sym(breach.symbol)}</span>
                    <span className="text-gray-400 mx-2">•</span>
                    <span className="text-sm text-gray-400">{breach.breachType.replace('_', ' ')}</span>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <span className="text-xs px-2 py-1 rounded bg-green-500/20 text-green-400">
                    {breach.autoAction}
                  </span>
                  <span className="text-xs text-gray-500">
                    {formatDistanceToNow(breach.resolvedAt!)} ago
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
