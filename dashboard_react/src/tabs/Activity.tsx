import { useDashboard } from '../store/dashboard'

export default function Activity() {
  const { activity } = useDashboard()

  return (
    <div className="bg-slate-900 rounded border border-slate-800">
      <p className="text-xs text-slate-500 px-4 pt-4 pb-2">Trade Activity ({activity.length})</p>
      {activity.length === 0 ? (
        <p className="px-4 py-6 text-slate-600 text-xs text-center">No trades today</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-slate-800 text-slate-500">
              {['Time', 'Symbol', 'Dir', 'Qty', 'Fill Price', 'P&L', 'Strategy'].map((h) => (
                <th key={h} className="text-left px-4 py-2 font-normal">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {[...activity].reverse().map((a) => (
              <tr key={a.id} className="border-b border-slate-800/50 hover:bg-slate-800/30">
                <td className="px-4 py-2 text-slate-500">{a.ts}</td>
                <td className="px-4 py-2 text-white font-medium">{a.symbol.split(':').pop()}</td>
                <td className={`px-4 py-2 ${a.direction === 'BUY' ? 'text-green-400' : 'text-red-400'}`}>{a.direction}</td>
                <td className="px-4 py-2 text-slate-300">{a.quantity}</td>
                <td className="px-4 py-2 text-slate-300">&#8377;{a.fillPrice.toFixed(2)}</td>
                <td className={`px-4 py-2 ${a.pnl === null ? 'text-slate-500' : a.pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {a.pnl === null ? '&#8212;' : `${a.pnl >= 0 ? '+' : ''}&#8377;${a.pnl.toFixed(0)}`}
                </td>
                <td className="px-4 py-2 text-slate-500">{a.strategy}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
