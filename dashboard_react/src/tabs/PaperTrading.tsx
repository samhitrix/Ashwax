import { useDashboard } from '../store/dashboard'

export default function PaperTrading() {
  const { paperMode, paperPositions, paperOrders } = useDashboard()

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 text-xs">
        <span className={`px-2 py-1 rounded border font-mono ${
          paperMode
            ? 'border-yellow-700 text-yellow-300 bg-yellow-950/30'
            : 'border-slate-700 text-slate-500'
        }`}>
          {paperMode ? '? PAPER MODE ACTIVE' : '? LIVE MODE'}
        </span>
        {paperMode && <span className="text-yellow-500">Simulated fills. No real orders sent.</span>}
      </div>

      <div className="bg-slate-900 border border-slate-800 rounded p-4">
        <p className="text-xs text-slate-500 mb-3">Paper Positions ({paperPositions.length})</p>
        {paperPositions.length === 0 ? (
          <p className="text-xs text-slate-600">No paper positions open.</p>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-500 border-b border-slate-800">
                {['Symbol', 'Dir', 'Qty', 'Avg', 'LTP', 'P&L'].map((h) => (
                  <th key={h} className="text-left py-1 pr-4 font-normal">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {paperPositions.map((p) => (
                <tr key={p.symbol} className="border-b border-slate-800/50">
                  <td className="py-1.5 pr-4 text-white">{p.symbol.split(':').pop()}</td>
                  <td className={`pr-4 ${p.direction === 'long' ? 'text-green-400' : 'text-red-400'}`}>{p.direction.toUpperCase()}</td>
                  <td className="pr-4 text-slate-300">{p.quantity}</td>
                  <td className="pr-4 text-slate-300">?{p.avgPrice.toFixed(2)}</td>
                  <td className="pr-4 text-slate-300">?{p.ltp.toFixed(2)}</td>
                  <td className={p.pnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                    {p.pnl >= 0 ? '+' : ''}?{p.pnl.toFixed(0)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="bg-slate-900 border border-slate-800 rounded p-4">
        <p className="text-xs text-slate-500 mb-3">Paper Orders ({paperOrders.length})</p>
        {paperOrders.length === 0 && <p className="text-xs text-slate-600">No paper orders.</p>}
      </div>
    </div>
  )
}
