import {
  Activity,
  AlertCircle,
  CheckCircle2,
  Clock,
  Loader2,
  RefreshCw,
  Shield,
  TrendingUp,
  Wifi,
  WifiOff,
  Wrench,
  XCircle,
  Zap,
} from 'lucide-react';
import { useTradingStore } from '../store/tradingStore';
import { cn } from '../utils/cn';
import { format, formatDistanceToNow } from 'date-fns';
import type { SelfHealingAction } from '../types/trading';

const actionTypeConfig: Record<string, { icon: typeof Zap; color: string; label: string }> = {
  ORDER_RETRY: { icon: RefreshCw, color: 'text-blue-400', label: 'Order Retry' },
  SL_REPLACEMENT: { icon: Shield, color: 'text-yellow-400', label: 'SL Replacement' },
  MARKET_EXIT: { icon: Zap, color: 'text-red-400', label: 'Market Exit' },
  POSITION_RECONCILE: { icon: Activity, color: 'text-purple-400', label: 'Position Reconcile' },
  CONNECTION_RESET: { icon: Wifi, color: 'text-cyan-400', label: 'Connection Reset' },
};

const statusConfig: Record<string, { icon: typeof CheckCircle2; color: string; bg: string }> = {
  PENDING: { icon: Clock, color: 'text-yellow-400', bg: 'bg-yellow-500/20' },
  EXECUTING: { icon: Loader2, color: 'text-blue-400', bg: 'bg-blue-500/20' },
  SUCCESS: { icon: CheckCircle2, color: 'text-green-400', bg: 'bg-green-500/20' },
  FAILED: { icon: XCircle, color: 'text-red-400', bg: 'bg-red-500/20' },
};

interface ActionCardProps {
  action: SelfHealingAction;
}

function ActionCard({ action }: ActionCardProps) {
  const { positions, executeHealingAction } = useTradingStore();
  const config = actionTypeConfig[action.type];
  const status = statusConfig[action.status];
  const StatusIcon = status.icon;
  const TypeIcon = config.icon;

  const position = positions.find((p) => p.id === action.positionId);

  return (
    <div className="bg-[#161b22] rounded-xl border border-gray-800 p-4">
      <div className="flex items-start justify-between">
        <div className="flex items-start gap-4">
          <div className={cn(
            'w-12 h-12 rounded-lg flex items-center justify-center',
            status.bg
          )}>
            <TypeIcon className={cn('w-6 h-6', config.color)} />
          </div>

          <div className="flex-1">
            <div className="flex items-center gap-2 mb-1">
              <h3 className="font-semibold text-white">{config.label}</h3>
              <span className={cn(
                'flex items-center gap-1 text-xs px-2 py-0.5 rounded',
                status.bg, status.color
              )}>
                <StatusIcon className={cn(
                  'w-3 h-3',
                  action.status === 'EXECUTING' && 'animate-spin'
                )} />
                {action.status}
              </span>
            </div>

            <p className="text-sm text-gray-400 mb-2">{action.trigger}</p>

            {position && (
              <div className="flex items-center gap-2 text-xs">
                <span className="px-2 py-0.5 rounded bg-gray-700 text-gray-300 font-mono">
                  {position.tradingSymbol}
                </span>
                <span className="text-gray-500">•</span>
                <span className="text-gray-400">
                  {position.quantity} qty @ ₹{position.averagePrice.toFixed(2)}
                </span>
              </div>
            )}

            {action.result && (
              <div className={cn(
                'mt-2 text-sm px-3 py-2 rounded',
                action.status === 'SUCCESS' ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'
              )}>
                {action.result}
              </div>
            )}

            <div className="flex items-center gap-4 mt-3 text-xs text-gray-500">
              <span className="flex items-center gap-1">
                <Clock className="w-3 h-3" />
                Created: {formatDistanceToNow(action.createdAt)} ago
              </span>
              {action.executedAt && (
                <span className="flex items-center gap-1">
                  <CheckCircle2 className="w-3 h-3" />
                  Executed: {format(action.executedAt, 'HH:mm:ss')}
                </span>
              )}
              <span>Attempts: {action.attempts}/{action.maxAttempts}</span>
            </div>
          </div>
        </div>

        {action.status === 'PENDING' && (
          <button
            onClick={() => executeHealingAction(action.id)}
            className="flex items-center gap-2 px-4 py-2 bg-blue-500 text-white rounded-lg text-sm font-medium hover:bg-blue-600 transition-colors"
          >
            <Zap className="w-4 h-4" />
            Execute Now
          </button>
        )}
      </div>
    </div>
  );
}

export function SelfHealing() {
  const { selfHealingActions, agentState } = useTradingStore();

  const pendingActions = selfHealingActions.filter((a) => a.status === 'PENDING');
  const executingActions = selfHealingActions.filter((a) => a.status === 'EXECUTING');
  const completedActions = selfHealingActions.filter((a) => a.status === 'SUCCESS' || a.status === 'FAILED');

  // Health metrics
  const healthScore = (() => {
    let score = 100;
    if (agentState.circuitBreakers.connectivity.triggered) score -= 30;
    if (agentState.circuitBreakers.dailyLoss.triggered) score -= 20;
    if (agentState.circuitBreakers.consecutiveLosses.triggered) score -= 15;
    if (pendingActions.length > 0) score -= pendingActions.length * 5;
    if (executingActions.length > 0) score -= executingActions.length * 2;
    return Math.max(0, score);
  })();

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Self-Healing System</h2>
          <p className="text-gray-400">Automatic recovery and remediation actions</p>
        </div>
      </div>

      {/* Health Overview */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center justify-between mb-3">
            <span className="text-gray-400 text-sm">System Health</span>
            <Activity className={cn(
              'w-5 h-5',
              healthScore >= 80 ? 'text-green-400' :
              healthScore >= 60 ? 'text-yellow-400' :
              healthScore >= 40 ? 'text-orange-400' : 'text-red-400'
            )} />
          </div>
          <div className={cn(
            'text-3xl font-bold',
            healthScore >= 80 ? 'text-green-400' :
            healthScore >= 60 ? 'text-yellow-400' :
            healthScore >= 40 ? 'text-orange-400' : 'text-red-400'
          )}>
            {healthScore}%
          </div>
          <div className="w-full bg-gray-700 rounded-full h-2 mt-2">
            <div
              className={cn(
                'h-2 rounded-full transition-all',
                healthScore >= 80 ? 'bg-green-500' :
                healthScore >= 60 ? 'bg-yellow-500' :
                healthScore >= 40 ? 'bg-orange-500' : 'bg-red-500'
              )}
              style={{ width: `${healthScore}%` }}
            />
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center justify-between mb-3">
            <span className="text-gray-400 text-sm">Connectivity</span>
            {agentState.circuitBreakers.connectivity.triggered ? (
              <WifiOff className="w-5 h-5 text-red-400" />
            ) : (
              <Wifi className="w-5 h-5 text-green-400" />
            )}
          </div>
          <div className={cn(
            'text-lg font-semibold',
            agentState.circuitBreakers.connectivity.triggered ? 'text-red-400' : 'text-green-400'
          )}>
            {agentState.circuitBreakers.connectivity.triggered ? 'Disconnected' : 'Connected'}
          </div>
          <div className="text-xs text-gray-500 mt-1">
            Last check: {formatDistanceToNow(agentState.circuitBreakers.connectivity.lastCheck)} ago
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center justify-between mb-3">
            <span className="text-gray-400 text-sm">Pending Actions</span>
            <Clock className={cn(
              'w-5 h-5',
              pendingActions.length > 0 ? 'text-yellow-400' : 'text-green-400'
            )} />
          </div>
          <div className={cn(
            'text-3xl font-bold',
            pendingActions.length > 0 ? 'text-yellow-400' : 'text-green-400'
          )}>
            {pendingActions.length}
          </div>
          <div className="text-xs text-gray-500 mt-1">
            {executingActions.length} executing
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center justify-between mb-3">
            <span className="text-gray-400 text-sm">Success Rate</span>
            <TrendingUp className="w-5 h-5 text-green-400" />
          </div>
          <div className="text-3xl font-bold text-green-400">
            {completedActions.length > 0
              ? ((completedActions.filter((a) => a.status === 'SUCCESS').length / completedActions.length) * 100).toFixed(0)
              : 100}%
          </div>
          <div className="text-xs text-gray-500 mt-1">
            {completedActions.filter((a) => a.status === 'SUCCESS').length} / {completedActions.length} successful
          </div>
        </div>
      </div>

      {/* Circuit Breakers */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-4">
        <h3 className="font-semibold text-white mb-4 flex items-center gap-2">
          <Shield className="w-5 h-5 text-blue-400" />
          Circuit Breakers
        </h3>
        <div className="grid grid-cols-4 gap-4">
          <div className={cn(
            'p-4 rounded-lg border',
            agentState.circuitBreakers.dailyLoss.triggered
              ? 'bg-red-500/10 border-red-500/30'
              : 'bg-green-500/10 border-green-500/30'
          )}>
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium">Daily Loss</span>
              {agentState.circuitBreakers.dailyLoss.triggered ? (
                <AlertCircle className="w-5 h-5 text-red-400" />
              ) : (
                <CheckCircle2 className="w-5 h-5 text-green-400" />
              )}
            </div>
            <div className="text-lg font-mono">
              ₹{Math.abs(agentState.circuitBreakers.dailyLoss.current).toLocaleString()}
              <span className="text-xs text-gray-400"> / ₹{agentState.circuitBreakers.dailyLoss.limit.toLocaleString()}</span>
            </div>
            <div className="w-full bg-gray-700 rounded-full h-1.5 mt-2">
              <div
                className={cn(
                  'h-1.5 rounded-full',
                  agentState.circuitBreakers.dailyLoss.triggered ? 'bg-red-500' : 'bg-green-500'
                )}
                style={{ width: `${Math.min(100, (Math.abs(agentState.circuitBreakers.dailyLoss.current) / agentState.circuitBreakers.dailyLoss.limit) * 100)}%` }}
              />
            </div>
          </div>

          <div className={cn(
            'p-4 rounded-lg border',
            agentState.circuitBreakers.consecutiveLosses.triggered
              ? 'bg-red-500/10 border-red-500/30'
              : 'bg-green-500/10 border-green-500/30'
          )}>
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium">Consecutive Losses</span>
              {agentState.circuitBreakers.consecutiveLosses.triggered ? (
                <AlertCircle className="w-5 h-5 text-red-400" />
              ) : (
                <CheckCircle2 className="w-5 h-5 text-green-400" />
              )}
            </div>
            <div className="text-lg font-mono">
              {agentState.circuitBreakers.consecutiveLosses.count}
              <span className="text-xs text-gray-400"> / {agentState.circuitBreakers.consecutiveLosses.limit}</span>
            </div>
          </div>

          <div className={cn(
            'p-4 rounded-lg border',
            agentState.circuitBreakers.connectivity.triggered
              ? 'bg-red-500/10 border-red-500/30'
              : 'bg-green-500/10 border-green-500/30'
          )}>
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium">Connectivity</span>
              {agentState.circuitBreakers.connectivity.triggered ? (
                <WifiOff className="w-5 h-5 text-red-400" />
              ) : (
                <Wifi className="w-5 h-5 text-green-400" />
              )}
            </div>
            <div className="text-sm">
              {agentState.circuitBreakers.connectivity.triggered ? 'Disconnected' : 'Connected'}
            </div>
          </div>

          <div className={cn(
            'p-4 rounded-lg border',
            agentState.circuitBreakers.newsBlackout.active
              ? 'bg-yellow-500/10 border-yellow-500/30'
              : 'bg-green-500/10 border-green-500/30'
          )}>
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium">News Blackout</span>
              {agentState.circuitBreakers.newsBlackout.active ? (
                <AlertCircle className="w-5 h-5 text-yellow-400" />
              ) : (
                <CheckCircle2 className="w-5 h-5 text-green-400" />
              )}
            </div>
            <div className="text-sm">
              {agentState.circuitBreakers.newsBlackout.active ? 'Active' : 'Inactive'}
            </div>
            {agentState.circuitBreakers.newsBlackout.reason && (
              <div className="text-xs text-gray-400 mt-1">
                {agentState.circuitBreakers.newsBlackout.reason}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Healing Actions */}
      <div className="space-y-4">
        {pendingActions.length > 0 && (
          <div>
            <h3 className="font-semibold text-yellow-400 mb-3 flex items-center gap-2">
              <Clock className="w-5 h-5" />
              Pending Actions ({pendingActions.length})
            </h3>
            <div className="space-y-3">
              {pendingActions.map((action) => (
                <ActionCard key={action.id} action={action} />
              ))}
            </div>
          </div>
        )}

        {executingActions.length > 0 && (
          <div>
            <h3 className="font-semibold text-blue-400 mb-3 flex items-center gap-2">
              <Loader2 className="w-5 h-5 animate-spin" />
              Executing ({executingActions.length})
            </h3>
            <div className="space-y-3">
              {executingActions.map((action) => (
                <ActionCard key={action.id} action={action} />
              ))}
            </div>
          </div>
        )}

        <div>
          <h3 className="font-semibold text-gray-400 mb-3 flex items-center gap-2">
            <Wrench className="w-5 h-5" />
            Recent Actions ({completedActions.length})
          </h3>
          {completedActions.length > 0 ? (
            <div className="space-y-3">
              {completedActions.map((action) => (
                <ActionCard key={action.id} action={action} />
              ))}
            </div>
          ) : (
            <div className="bg-[#161b22] rounded-xl border border-gray-800 p-8 text-center">
              <CheckCircle2 className="w-12 h-12 text-gray-600 mx-auto mb-4" />
              <h3 className="text-lg font-semibold text-gray-400">No Recent Actions</h3>
              <p className="text-gray-500 mt-2">System is running smoothly</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
