import { useEffect } from 'react';
import { AlertCircle, X } from 'lucide-react';
import { useTradingStore } from '../store/tradingStore';
import type { ToastItem } from '../store/tradingStore';
import { cn } from '../utils/cn';

const AUTO_DISMISS_MS = 8000;

const TIER_STYLE: Record<ToastItem['tier'], { border: string; badge: string }> = {
  INFO: { border: 'border-gray-700', badge: 'bg-blue-500/20 text-blue-300' },
  WARNING: { border: 'border-yellow-500/30', badge: 'bg-yellow-500/20 text-yellow-300' },
  ALERT: { border: 'border-orange-500/30', badge: 'bg-orange-500/20 text-orange-300' },
  CRITICAL: { border: 'border-red-500/30', badge: 'bg-red-500/20 text-red-300' },
  EMERGENCY: { border: 'border-red-500/60', badge: 'bg-red-600/30 text-red-200' },
};

function ToastCard({ toast }: { toast: ToastItem }) {
  const { dismissToast, setActiveTab } = useTradingStore();
  const style = TIER_STYLE[toast.tier];
  const isCriticalOrHigher = toast.tier === 'CRITICAL' || toast.tier === 'EMERGENCY';

  useEffect(() => {
    const id = setTimeout(() => dismissToast(toast.id), AUTO_DISMISS_MS);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toast.id]);

  return (
    <div
      role="alert"
      onClick={() => {
        if (isCriticalOrHigher) setActiveTab('alerts');
        dismissToast(toast.id);
      }}
      className={cn(
        'w-[26rem] max-w-[calc(100vw-2rem)] cursor-pointer pointer-events-auto',
        'bg-[#161b22] rounded-xl border shadow-2xl p-3 flex items-start gap-3',
        style.border,
      )}
    >
      <div className={cn('w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0', style.badge,
        toast.tier === 'EMERGENCY' && 'animate-pulse')}>
        <AlertCircle className="w-4 h-4" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-0.5">
          <span className={cn('text-xs font-semibold px-2 py-0.5 rounded-full', style.badge)}>
            {toast.tier}
          </span>
          {isCriticalOrHigher && <span className="text-xs text-gray-500">click for Alerts tab</span>}
        </div>
        <p className="text-sm text-gray-200 break-words">{toast.message}</p>
      </div>
      <button
        onClick={(e) => {
          e.stopPropagation();
          dismissToast(toast.id);
        }}
        className="text-gray-500 hover:text-gray-300 flex-shrink-0"
        aria-label="Dismiss"
      >
        <X className="w-4 h-4" />
      </button>
    </div>
  );
}

/** Bottom-right toast stack — new system alerts and broker-style order lifecycle
 * events (placed/modified/executed/rejected/cancelled). Each card auto-dismisses
 * independently so a burst of near-simultaneous events (e.g. two orders placed
 * 200ms apart) all get their own card instead of clobbering one slot. */
export function AlertToast() {
  const { toasts } = useTradingStore();
  if (toasts.length === 0) return null;

  return (
    <div className="fixed right-4 bottom-4 z-[100] flex flex-col-reverse gap-2 pointer-events-none">
      {toasts.map((t) => <ToastCard key={t.id} toast={t} />)}
    </div>
  );
}
