import { useDashboard } from '../store/dashboard'
import type { AlertTier } from '../types'

const TIER_STYLE: Record<AlertTier, string> = {
  INFO:      'border-blue-800/60 bg-blue-950/30 text-blue-300',
  WARNING:   'border-yellow-800/60 bg-yellow-950/30 text-yellow-300',
  ALERT:     'border-orange-800/60 bg-orange-950/30 text-orange-300',
  CRITICAL:  'border-red-800/60 bg-red-950/30 text-red-300',
  EMERGENCY: 'border-red-600 bg-red-900/50 text-red-200 animate-pulse',
}
const TIER_BADGE: Record<AlertTier, string> = {
  INFO:      'bg-blue-900 text-blue-200',
  WARNING:   'bg-yellow-900 text-yellow-200',
  ALERT:     'bg-orange-900 text-orange-200',
  CRITICAL:  'bg-red-900 text-red-200',
  EMERGENCY: 'bg-red-700 text-white',
}

export default function Alerts() {
  const { alerts, acknowledgeAlert } = useDashboard()
  const unacked = alerts.filter((a) => !a.acknowledged)
  const acked   = alerts.filter((a) =>  a.acknowledged)

  return (
    <div className="space-y-3">
      {unacked.length === 0 && (
        <p className="text-xs text-slate-500 py-2">No unacknowledged alerts.</p>
      )}
      {[...unacked].reverse().map((a) => (
        <div key={a.id} className={`rounded border px-4 py-3 flex items-start justify-between gap-4 ${TIER_STYLE[a.tier]}`}>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <span className={`text-xs px-1.5 py-0.5 rounded font-mono ${TIER_BADGE[a.tier]}`}>{a.tier}</span>
              <span className="text-xs text-slate-500">{a.ts}</span>
            </div>
            <p className="text-xs">{a.message}</p>
          </div>
          <button
            onClick={() => acknowledgeAlert(a.id)}
            className="text-xs text-slate-500 hover:text-white shrink-0 border border-slate-700 rounded px-2 py-0.5"
          >
            ACK
          </button>
        </div>
      ))}

      {acked.length > 0 && (
        <>
          <p className="text-xs text-slate-600 mt-4 mb-2">Acknowledged</p>
          {[...acked].reverse().map((a) => (
            <div key={a.id} className="rounded border border-slate-800 px-4 py-2 flex items-center gap-3 opacity-40">
              <span className="text-xs text-slate-500">{a.ts}</span>
              <span className={`text-xs px-1 rounded ${TIER_BADGE[a.tier]}`}>{a.tier}</span>
              <p className="text-xs text-slate-500">{a.message}</p>
            </div>
          ))}
        </>
      )}
    </div>
  )
}
