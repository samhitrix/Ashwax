import { useState, useEffect } from 'react';
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  Clock,
  Edit2,
  RefreshCw,
  Shield,
  Target,
  TrendingDown,
  XCircle,
  Zap,
} from 'lucide-react';
import { useTradingStore } from '../store/tradingStore';
import { cn } from '../utils/cn';
import type { Position, Order } from '../types/trading';
import { format, formatDistanceToNow } from 'date-fns';

interface OrderMonitorCardProps {
  position: Position;
  orders: Order[];
}

function OrderMonitorCard({ position, orders }: OrderMonitorCardProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  const slOrder = orders.find((o) => o.parentOrderId === position.entryOrderId && o.tag?.startsWith('SL-'));
  const targetOrder = orders.find((o) => o.parentOrderId === position.entryOrderId && o.tag?.startsWith('TGT-'));

  // Calculate distances
  const distanceToSL = ((position.ltp - position.currentSL) / position.ltp) * 100;
  const distanceToTarget = ((position.currentTarget - position.ltp) / position.ltp) * 100;

  // Determine risk levels
  const slRisk = distanceToSL < 1 ? 'CRITICAL' : distanceToSL < 2 ? 'HIGH' : distanceToSL < 3 ? 'MEDIUM' : 'LOW';
  const targetProximity = distanceToTarget < 1 ? 'IMMINENT' : distanceToTarget < 2 ? 'CLOSE' : distanceToTarget < 5 ? 'APPROACHING' : 'FAR';

  // Check for potential issues
  const ltpAboveTarget = position.ltp > position.currentTarget;
  const ltpBelowSL = position.ltp < position.currentSL;

  const getRiskColor = (risk: string) => {
    switch (risk) {
      case 'CRITICAL': return 'text-red-500 bg-red-500/20';
      case 'HIGH': return 'text-orange-500 bg-orange-500/20';
      case 'MEDIUM': return 'text-yellow-500 bg-yellow-500/20';
      default: return 'text-green-500 bg-green-500/20';
    }
  };

  return (
    <div className={cn(
      'bg-[#161b22] rounded-xl border overflow-hidden transition-all',
      ltpAboveTarget || ltpBelowSL
        ? 'border-red-500/50 ring-2 ring-red-500/20'
        : slRisk === 'CRITICAL'
        ? 'border-orange-500/50'
        : 'border-gray-800'
    )}>
      {/* Header */}
      <div
        className="p-4 cursor-pointer hover:bg-gray-800/50 transition-colors"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div className={cn(
              'w-12 h-12 rounded-lg flex items-center justify-center font-bold text-lg',
              position.direction === 'LONG'
                ? 'bg-green-500/20 text-green-400'
                : 'bg-red-500/20 text-red-400'
            )}>
              {position.direction === 'LONG' ? <ArrowUp className="w-6 h-6" /> : <ArrowDown className="w-6 h-6" />}
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h3 className="text-lg font-bold text-white">{position.tradingSymbol}</h3>
                <span className="text-xs px-2 py-0.5 rounded bg-gray-700 text-gray-300">{position.strategy}</span>
              </div>
              <div className="flex items-center gap-4 text-sm text-gray-400">
                <span>{position.quantity} qty @ ₹{position.averagePrice.toFixed(2)}</span>
                <span>•</span>
                <span>Checks: {position.checksCount}</span>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-6">
            {/* LTP */}
            <div className="text-right">
              <div className="text-xs text-gray-400">LTP</div>
              <div className="text-xl font-mono font-bold text-white">₹{position.ltp.toFixed(2)}</div>
            </div>

            {/* P&L */}
            <div className="text-right min-w-[100px]">
              <div className="text-xs text-gray-400">P&L</div>
              <div className={cn(
                'text-lg font-mono font-semibold',
                position.pnl >= 0 ? 'text-green-400' : 'text-red-400'
              )}>
                {position.pnl >= 0 ? '+' : ''}₹{position.pnl.toFixed(2)}
                <span className="text-xs ml-1">({position.pnlPercent.toFixed(2)}%)</span>
              </div>
            </div>

            {/* Status Indicators */}
            <div className="flex flex-col gap-1">
              {ltpAboveTarget && (
                <span className="flex items-center gap-1 text-xs px-2 py-1 rounded bg-red-500/20 text-red-400">
                  <AlertCircle className="w-3 h-3" />
                  TARGET BREACH
                </span>
              )}
              {ltpBelowSL && (
                <span className="flex items-center gap-1 text-xs px-2 py-1 rounded bg-red-500/20 text-red-400">
                  <AlertCircle className="w-3 h-3" />
                  SL BREACH
                </span>
              )}
              {!ltpAboveTarget && !ltpBelowSL && (
                <span className={cn(
                  'flex items-center gap-1 text-xs px-2 py-1 rounded',
                  getRiskColor(slRisk)
                )}>
                  <Shield className="w-3 h-3" />
                  {slRisk} RISK
                </span>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* SL & Target Progress Bar */}
      <div className="px-4 pb-4">
        <div className="relative h-8 bg-gray-800 rounded-lg overflow-hidden">
          {/* SL Zone */}
          <div
            className="absolute left-0 top-0 bottom-0 bg-red-500/30"
            style={{ width: `${Math.max(0, Math.min(30, (1 - position.currentSL / position.currentTarget) * 100))}%` }}
          />
          {/* Target Zone */}
          <div
            className="absolute right-0 top-0 bottom-0 bg-green-500/30"
            style={{ width: '20%' }}
          />
          {/* Current Price Marker */}
          <div
            className="absolute top-0 bottom-0 w-1 bg-blue-500"
            style={{
              left: `${Math.max(0, Math.min(100, ((position.ltp - position.currentSL) / (position.currentTarget - position.currentSL)) * 100))}%`,
            }}
          />
          {/* Labels */}
          <div className="absolute inset-0 flex items-center justify-between px-3 text-xs">
            <span className="text-red-400 font-mono">SL: ₹{position.currentSL}</span>
            <span className="text-blue-400 font-mono">LTP: ₹{position.ltp.toFixed(2)}</span>
            <span className="text-green-400 font-mono">TGT: ₹{position.currentTarget}</span>
          </div>
        </div>
        <div className="flex justify-between mt-2 text-xs text-gray-400">
          <span>{distanceToSL.toFixed(2)}% to SL</span>
          <span>{distanceToTarget.toFixed(2)}% to Target</span>
        </div>
      </div>

      {/* Expanded Details */}
      {isExpanded && (
        <div className="border-t border-gray-800 p-4 space-y-4">
          {/* Order Details Grid */}
          <div className="grid grid-cols-2 gap-4">
            {/* Stop Loss Order */}
            <div className="bg-[#0d1117] rounded-lg p-4">
              <div className="flex items-center gap-2 mb-3">
                <TrendingDown className="w-5 h-5 text-red-400" />
                <h4 className="font-semibold text-red-400">Stop Loss Order</h4>
              </div>
              {slOrder ? (
                <div className="space-y-2">
                  <div className="flex justify-between text-sm">
                    <span className="text-gray-400">Order ID</span>
                    <span className="font-mono">{slOrder.orderId}</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-gray-400">Type</span>
                    <span className={cn(
                      'px-2 py-0.5 rounded text-xs',
                      slOrder.orderType === 'SL' ? 'bg-yellow-500/20 text-yellow-400' : 'bg-blue-500/20 text-blue-400'
                    )}>
                      {slOrder.orderType}
                    </span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-gray-400">Trigger Price</span>
                    <span className="font-mono">₹{slOrder.triggerPrice}</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-gray-400">Limit Price</span>
                    <span className="font-mono">₹{slOrder.price}</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-gray-400">Status</span>
                    <span className={cn(
                      'px-2 py-0.5 rounded text-xs',
                      slOrder.status === 'TRIGGER_PENDING' ? 'bg-yellow-500/20 text-yellow-400' :
                      slOrder.status === 'COMPLETE' ? 'bg-green-500/20 text-green-400' :
                      'bg-gray-500/20 text-gray-400'
                    )}>
                      {slOrder.status}
                    </span>
                  </div>
                  <div className="flex gap-2 mt-3">
                    <button className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-yellow-500/20 text-yellow-400 rounded-lg text-sm hover:bg-yellow-500/30 transition-colors">
                      <Edit2 className="w-4 h-4" />
                      Modify
                    </button>
                    <button className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-red-500/20 text-red-400 rounded-lg text-sm hover:bg-red-500/30 transition-colors">
                      <Zap className="w-4 h-4" />
                      Market Exit
                    </button>
                  </div>
                </div>
              ) : (
                <div className="flex items-center gap-2 text-red-400">
                  <XCircle className="w-5 h-5" />
                  <span>No SL Order Found!</span>
                </div>
              )}
            </div>

            {/* Target Order */}
            <div className="bg-[#0d1117] rounded-lg p-4">
              <div className="flex items-center gap-2 mb-3">
                <Target className="w-5 h-5 text-green-400" />
                <h4 className="font-semibold text-green-400">Target Order</h4>
              </div>
              {targetOrder ? (
                <div className="space-y-2">
                  <div className="flex justify-between text-sm">
                    <span className="text-gray-400">Order ID</span>
                    <span className="font-mono">{targetOrder.orderId}</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-gray-400">Type</span>
                    <span className={cn(
                      'px-2 py-0.5 rounded text-xs',
                      targetOrder.orderType === 'LIMIT' ? 'bg-blue-500/20 text-blue-400' : 'bg-yellow-500/20 text-yellow-400'
                    )}>
                      {targetOrder.orderType}
                    </span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-gray-400">Limit Price</span>
                    <span className="font-mono">₹{targetOrder.price}</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-gray-400">Status</span>
                    <span className={cn(
                      'px-2 py-0.5 rounded text-xs',
                      targetOrder.status === 'OPEN' ? 'bg-blue-500/20 text-blue-400' :
                      targetOrder.status === 'COMPLETE' ? 'bg-green-500/20 text-green-400' :
                      'bg-gray-500/20 text-gray-400'
                    )}>
                      {targetOrder.status}
                    </span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-gray-400">Proximity</span>
                    <span className={cn(
                      'px-2 py-0.5 rounded text-xs',
                      targetProximity === 'IMMINENT' ? 'bg-green-500/20 text-green-400' :
                      targetProximity === 'CLOSE' ? 'bg-yellow-500/20 text-yellow-400' :
                      'bg-gray-500/20 text-gray-400'
                    )}>
                      {targetProximity}
                    </span>
                  </div>
                  <div className="flex gap-2 mt-3">
                    <button className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-yellow-500/20 text-yellow-400 rounded-lg text-sm hover:bg-yellow-500/30 transition-colors">
                      <Edit2 className="w-4 h-4" />
                      Trail Stop
                    </button>
                    <button className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-green-500/20 text-green-400 rounded-lg text-sm hover:bg-green-500/30 transition-colors">
                      <CheckCircle2 className="w-4 h-4" />
                      Book Profit
                    </button>
                  </div>
                </div>
              ) : (
                <div className="text-gray-400 text-sm">
                  No explicit target order (using trailing stop)
                </div>
              )}
            </div>
          </div>

          {/* Monitoring Stats */}
          <div className="bg-[#0d1117] rounded-lg p-4">
            <h4 className="font-semibold mb-3 flex items-center gap-2">
              <Clock className="w-5 h-5 text-blue-400" />
              Monitoring Statistics
            </h4>
            <div className="grid grid-cols-4 gap-4">
              <div>
                <div className="text-xs text-gray-400">Entry Time</div>
                <div className="font-mono text-sm">{format(position.entryTimestamp, 'HH:mm:ss')}</div>
                <div className="text-xs text-gray-500">{formatDistanceToNow(position.entryTimestamp)} ago</div>
              </div>
              <div>
                <div className="text-xs text-gray-400">Last Checked</div>
                <div className="font-mono text-sm">{format(position.lastCheckedAt, 'HH:mm:ss')}</div>
              </div>
              <div>
                <div className="text-xs text-gray-400">Missed Targets</div>
                <div className={cn(
                  'font-mono text-lg',
                  position.missedTargetCount > 0 ? 'text-orange-400' : 'text-green-400'
                )}>
                  {position.missedTargetCount}
                </div>
              </div>
              <div>
                <div className="text-xs text-gray-400">Missed SLs</div>
                <div className={cn(
                  'font-mono text-lg',
                  position.missedSLCount > 0 ? 'text-red-400' : 'text-green-400'
                )}>
                  {position.missedSLCount}
                </div>
              </div>
            </div>
          </div>

          {/* Action Recommendations */}
          {(ltpAboveTarget || ltpBelowSL || slRisk === 'CRITICAL') && (
            <div className={cn(
              'rounded-lg p-4 border',
              ltpAboveTarget || ltpBelowSL
                ? 'bg-red-500/10 border-red-500/30'
                : 'bg-orange-500/10 border-orange-500/30'
            )}>
              <h4 className="font-semibold mb-2 flex items-center gap-2">
                <AlertCircle className={cn(
                  'w-5 h-5',
                  ltpAboveTarget || ltpBelowSL ? 'text-red-400' : 'text-orange-400'
                )} />
                Recommended Action
              </h4>
              {ltpAboveTarget && (
                <div className="space-y-2">
                  <p className="text-sm text-gray-300">
                    LTP (₹{position.ltp.toFixed(2)}) has exceeded target (₹{position.currentTarget}).
                    The LIMIT order may not execute. Consider:
                  </p>
                  <ul className="text-sm text-gray-400 list-disc list-inside space-y-1">
                    <li>Cancel limit order and place MARKET sell to book profits</li>
                    <li>Modify target order to current LTP or above</li>
                    <li>Enable trailing stop to protect gains</li>
                  </ul>
                  <button className="mt-2 flex items-center gap-2 px-4 py-2 bg-green-500 text-white rounded-lg text-sm font-medium hover:bg-green-600 transition-colors">
                    <Zap className="w-4 h-4" />
                    Execute Market Exit Now
                  </button>
                </div>
              )}
              {ltpBelowSL && (
                <div className="space-y-2">
                  <p className="text-sm text-gray-300">
                    LTP (₹{position.ltp.toFixed(2)}) has breached SL (₹{position.currentSL}).
                    SL order may have failed or slipped. IMMEDIATE ACTION REQUIRED:
                  </p>
                  <button className="mt-2 flex items-center gap-2 px-4 py-2 bg-red-500 text-white rounded-lg text-sm font-medium hover:bg-red-600 transition-colors">
                    <Zap className="w-4 h-4" />
                    Emergency Market Exit
                  </button>
                </div>
              )}
              {slRisk === 'CRITICAL' && !ltpBelowSL && (
                <div className="space-y-2">
                  <p className="text-sm text-gray-300">
                    Price is within {distanceToSL.toFixed(2)}% of stop loss. Consider:
                  </p>
                  <ul className="text-sm text-gray-400 list-disc list-inside space-y-1">
                    <li>Verify SL order is active and at correct price</li>
                    <li>Consider manual exit if strategy conviction is low</li>
                    <li>Prepare market order as backup</li>
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function OrderMonitor() {
  const { positions, orders, refreshData, orderMonitorConfig } = useTradingStore();
  const [lastRefresh, setLastRefresh] = useState(new Date());

  useEffect(() => {
    // Floor the poll at 2s: checkIntervalMs (default 500ms) would otherwise fire
    // refreshData 4×/s on top of the global App loop, flooding the browser. The
    // store-level single-flight guard dedupes overlap; this avoids wasteful churn.
    const _pollMs = Math.max(orderMonitorConfig.checkIntervalMs, 2000);
    const interval = setInterval(() => {
      refreshData();
      setLastRefresh(new Date());
    }, _pollMs);

    return () => clearInterval(interval);
  }, [refreshData, orderMonitorConfig.checkIntervalMs]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Order Monitoring</h2>
          <p className="text-gray-400">Real-time SL & Target tracking with instant action capabilities</p>
        </div>
        <div className="flex items-center gap-4">
          <div className="text-sm text-gray-400">
            <span>Last refresh: </span>
            <span className="font-mono">{format(lastRefresh, 'HH:mm:ss')}</span>
          </div>
          <button
            onClick={() => {
              refreshData();
              setLastRefresh(new Date());
            }}
            className="flex items-center gap-2 px-4 py-2 bg-blue-500/20 text-blue-400 rounded-lg hover:bg-blue-500/30 transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            Refresh
          </button>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-2">
            <div className="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center">
              <Target className="w-5 h-5 text-blue-400" />
            </div>
            <div>
              <div className="text-2xl font-bold">{positions.length}</div>
              <div className="text-xs text-gray-400">Active Positions</div>
            </div>
          </div>
        </div>
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-2">
            <div className="w-10 h-10 rounded-lg bg-green-500/20 flex items-center justify-center">
              <CheckCircle2 className="w-5 h-5 text-green-400" />
            </div>
            <div>
              <div className="text-2xl font-bold text-green-400">
                {positions.filter((p) => p.slStatus === 'ACTIVE').length}
              </div>
              <div className="text-xs text-gray-400">SL Orders Active</div>
            </div>
          </div>
        </div>
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-2">
            <div className="w-10 h-10 rounded-lg bg-orange-500/20 flex items-center justify-center">
              <AlertCircle className="w-5 h-5 text-orange-400" />
            </div>
            <div>
              <div className="text-2xl font-bold text-orange-400">
                {positions.filter((p) => {
                  const dist = ((p.ltp - p.currentSL) / p.ltp) * 100;
                  return dist < 2;
                }).length}
              </div>
              <div className="text-xs text-gray-400">Near SL Level</div>
            </div>
          </div>
        </div>
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-2">
            <div className="w-10 h-10 rounded-lg bg-purple-500/20 flex items-center justify-center">
              <RefreshCw className="w-5 h-5 text-purple-400" />
            </div>
            <div>
              <div className="text-2xl font-bold font-mono">{orderMonitorConfig.checkIntervalMs}ms</div>
              <div className="text-xs text-gray-400">Check Interval</div>
            </div>
          </div>
        </div>
      </div>

      {/* Position Cards */}
      <div className="space-y-4">
        {positions.map((position) => (
          <OrderMonitorCard
            key={position.id}
            position={position}
            orders={orders}
          />
        ))}

        {positions.length === 0 && (
          <div className="bg-[#161b22] rounded-xl border border-gray-800 p-12 text-center">
            <Target className="w-12 h-12 text-gray-600 mx-auto mb-4" />
            <h3 className="text-lg font-semibold text-gray-400">No Active Positions</h3>
            <p className="text-gray-500 mt-2">Positions will appear here when trades are opened</p>
          </div>
        )}
      </div>
    </div>
  );
}
