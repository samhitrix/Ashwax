import { useState } from 'react'
import { useDashboard } from '../store/dashboard'

export default function Controls() {
  const { masterSwitch, autonomyMode, paperMode, setMasterSwitch, setAutonomyMode, setPaperMode } = useDashboard()
  const [liveGate, setLiveGate] = useState('')
  const [countdown, setCountdown] = useState<number | null>(null)

  const startLiveSwitch = () => {
    if (liveGate !== 'LIVE') return
    let n = 30
    setCountdown(n)
    const iv = setInterval(() => {
      n -= 1
      setCountdown(n)
      if (n <= 0) {
        clearInterval(iv)
        setCountdown(null)
        setPaperMode(false)
        setLiveGate('')
      }
    }, 1000)
  }

  return (
    <div className="space-y-4 max-w-lg">
      {/* Master switch */}
      <div className="bg-slate-900 border border-slate-800 rounded p-4">
        <p className="text-xs text-slate-500 mb-3">Master Switch</p>
        <div className="flex items-center gap-4">
          <button
            onClick={() => setMasterSwitch(!masterSwitch)}
            className={`px-4 py-2 rounded text-sm font-mono border transition-colors ${
              masterSwitch
                ? 'bg-green-900/40 border-green-700 text-green-300 hover:bg-green-900/60'
                : 'bg-slate-800 border-slate-700 text-slate-400 hover:bg-slate-700'
            }`}
          >
            {masterSwitch ? '? ON' : '? OFF'}
          </button>
          <span className="text-xs text-slate-500">
            {masterSwitch ? 'System is trading.' : 'All new orders blocked.'}
          </span>
        </div>
      </div>

      {/* Autonomy mode */}
      <div className="bg-slate-900 border border-slate-800 rounded p-4">
        <p className="text-xs text-slate-500 mb-3">Autonomy Mode</p>
        <div className="flex gap-2">
          {(['manual', 'assisted', 'autonomous'] as const).map((m) => (
            <button
              key={m}
              onClick={() => setAutonomyMode(m)}
              className={`px-3 py-1.5 rounded text-xs font-mono border transition-colors ${
                autonomyMode === m
                  ? 'bg-indigo-900/60 border-indigo-600 text-indigo-200'
                  : 'bg-slate-800 border-slate-700 text-slate-500 hover:text-slate-300'
              }`}
            >
              {m}
            </button>
          ))}
        </div>
        <p className="text-xs text-slate-600 mt-2">
          {autonomyMode === 'manual'     && 'No autonomous orders. Human approves each trade.'}
          {autonomyMode === 'assisted'   && 'LLM suggests. Human approves. Risk gates enforced.'}
          {autonomyMode === 'autonomous' && 'Fully autonomous. LLM + risk gates only.'}
        </p>
      </div>

      {/* Paper / Live gate */}
      <div className="bg-slate-900 border border-slate-800 rounded p-4">
        <p className="text-xs text-slate-500 mb-3">Trading Mode</p>
        <div className={`text-xs mb-3 px-2 py-1 rounded border inline-block ${
          paperMode ? 'border-yellow-700 text-yellow-300' : 'border-green-700 text-green-300'
        }`}>
          {paperMode ? 'PAPER' : 'LIVE'}
        </div>

        {paperMode ? (
          <div className="space-y-2">
            <p className="text-xs text-slate-500">
              Type <code className="text-yellow-400">LIVE</code> to switch to live trading:
            </p>
            <input
              value={liveGate}
              onChange={(e) => setLiveGate(e.target.value.toUpperCase())}
              placeholder="Type LIVE..."
              className="bg-slate-800 border border-slate-700 rounded px-3 py-1.5 text-xs font-mono text-white w-full outline-none focus:border-indigo-500"
            />
            {countdown !== null ? (
              <p className="text-xs text-red-400 animate-pulse">
                Switching to LIVE in {countdown}s... close this tab to abort.
              </p>
            ) : (
              <button
                disabled={liveGate !== 'LIVE'}
                onClick={startLiveSwitch}
                className="px-3 py-1.5 rounded text-xs font-mono border border-red-700 text-red-300 bg-red-950/30 disabled:opacity-30 disabled:cursor-not-allowed hover:bg-red-900/50"
              >
                Switch to LIVE
              </button>
            )}
          </div>
        ) : (
          <button
            onClick={() => setPaperMode(true)}
            className="px-3 py-1.5 rounded text-xs border border-slate-700 text-slate-400 hover:text-white hover:border-slate-500"
          >
            Switch to Paper
          </button>
        )}
      </div>
    </div>
  )
}
