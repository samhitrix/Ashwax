import { useDashboard } from '../store/dashboard'
import type { InvariantSeverity } from '../types'

const SEV_DOT:  Record<InvariantSeverity, string> = { OK: 'text-green-400', WARNING: 'text-yellow-400', CRITICAL: 'text-red-400' }
const SEV_ICON: Record<InvariantSeverity, string> = { OK: '?', WARNING: '?', CRITICAL: '?' }

export default function SystemHealth() {
  const { invariants, loops, systemLevel } = useDashboard()
  const criticals = invariants.filter((i) => i.severity === 'CRITICAL')
  const warnings  = invariants.filter((i) => i.severity === 'WARNING')
  const deadLoops = loops.filter((l) => !l.alive)

  const levelColor =
    systemLevel === 'FULL_OPS'   ? 'text-green-400' :
    systemLevel === 'EMERGENCY'  ? 'text-red-400 animate-pulse' :
    systemLevel === 'ELEVATED'   ? 'text-yellow-400' : 'text-orange-400'

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-3">
        <Stat label="System Level" value={systemLevel} color={levelColor} />
        <Stat
          label="Invariants"
          value={`${invariants.length - criticals.length - warnings.length}/${invariants.length} OK`}
          color={criticals.length > 0 ? 'text-red-400' : warnings.length > 0 ? 'text-yellow-400' : 'text-green-400'}
        />
        <Stat
          label="Loops"
          value={`${loops.length - deadLoops.length}/${loops.length} alive`}
          color={deadLoops.length > 0 ? 'text-red-400' : 'text-green-400'}
        />
      </div>

      <div className="bg-slate-900 border border-slate-800 rounded p-4">
        <p className="text-xs text-slate-500 mb-3">Invariants (15)</p>
        <div className="space-y-1">
          {invariants.map((inv) => (
            <div key={inv.id} className="flex items-start gap-2 text-xs">
              <span className={`shrink-0 ${SEV_DOT[inv.severity]}`}>{SEV_ICON[inv.severity]}</span>
              <span className={inv.severity === 'OK' ? 'text-slate-400' : inv.severity === 'WARNING' ? 'text-yellow-300' : 'text-red-300'}>
                {inv.message}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div className="bg-slate-900 border border-slate-800 rounded p-4">
        <p className="text-xs text-slate-500 mb-3">Loops (12)</p>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
          {loops.map((loop) => (
            <div
              key={loop.name}
              className={`flex items-center gap-2 text-xs p-2 rounded border ${
                loop.alive ? 'border-green-900/50 bg-green-950/20' : 'border-red-900/50 bg-red-950/20'
              }`}
            >
              <span className={loop.alive ? 'text-green-400' : 'text-red-400'}>{loop.alive ? '?' : '?'}</span>
              <div>
                <p className={loop.alive ? 'text-slate-300' : 'text-red-300'}>{loop.name}</p>
                <p className="text-slate-600 text-xs">{loop.lastTick}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded p-3 text-center">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`text-sm font-bold mt-1 ${color}`}>{value}</p>
    </div>
  )
}
