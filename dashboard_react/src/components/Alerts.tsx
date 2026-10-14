import { AlertCircle, CheckCircle2 } from 'lucide-react';
import { useTradingStore } from '../store/tradingStore';
import type { AlertItem } from '../store/tradingStore';
import { cn } from '../utils/cn';

const TIER_STYLE: Record<AlertItem['tier'], { badge: string; border: string; pulse: boolean }> = {
  INFO:      { badge: 'bg-blue-500/20 text-blue-300',    border: 'border-blue-500/20',   pulse: false },
  WARNING:   { badge: 'bg-yellow-500/20 text-yellow-300', border: 'border-yellow-500/20', pulse: false },
  ALERT:     { badge: 'bg-orange-500/20 text-orange-300', border: 'border-orange-500/20', pulse: false },
  CRITICAL:  { badge: 'bg-red-500/20 text-red-300',      border: 'border-red-500/30',    pulse: false },
  EMERGENCY: { badge: 'bg-red-600/30 text-red-200',      border: 'border-red-500/60',    pulse: true  },
};

function SystemAlertCard({ alert }: { alert: AlertItem }) {
  const style = TIER_STYLE[alert.tier];
  return (
    <div className={cn('bg-[#161b22] rounded-xl p-4 border flex items-start gap-3', style.border)}>
      <div className={cn('mt-0.5 w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0', style.badge,
        style.pulse && 'animate-pulse')}>
        <AlertCircle className="w-4 h-4" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1">
          <span className={cn('text-xs font-semibold px-2 py-0.5 rounded-full', style.badge)}>
            {alert.tier}
          </span>
          <span className="text-xs text-gray-500">{alert.ts}</span>
          {alert.source && <span className="text-xs text-gray-600">{alert.source}</span>}
        </div>
        <p className="text-sm text-gray-200 break-words">{alert.message}</p>
      </div>
      {alert.tier !== 'EMERGENCY' && (
        <span className="text-xs text-gray-600 ml-2 flex-shrink-0">
          {alert.acknowledged ? 'ACK' : ''}
        </span>
      )}
    </div>
  );
}

// Order-level alerts (SL_BREACH/ORDER_REJECTED/etc, with Critical/High/Active/Resolved
// tiles) used to render here too, but nothing in the app has ever populated that store
// field — it was permanently empty, always showing "No Active Alerts" regardless of
// what was actually happening (2026-09-07: CGCL sat unprotected for 12+ hours while
// this said everything was fine). Removed until there's a real backend feed for it —
// see System Alerts below for what's actually wired to the supervisor.
export function Alerts() {
  const { systemAlerts } = useTradingStore();

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">System Alerts</h2>
          <p className="text-gray-400">
            Everything sent to Telegram, plus supervisor CRITICAL / EMERGENCY conditions — newest first
          </p>
        </div>
      </div>

      {systemAlerts.length > 0 ? (
        <div className="space-y-3">
          {[...systemAlerts].reverse().map((a) => <SystemAlertCard key={a.id} alert={a} />)}
        </div>
      ) : (
        <div className="bg-[#161b22] rounded-xl border border-gray-800 p-12 text-center">
          <CheckCircle2 className="w-12 h-12 text-green-400 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-gray-400">No System Alerts</h3>
          <p className="text-gray-500 mt-2">Nothing has been sent to Telegram and no supervisor alert is active.</p>
        </div>
      )}
    </div>
  );
}
