import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import { useDashboard } from '../store/dashboard'

export default function Overview() {
  const { equityCurve, currentEquity, startingCapital, dailyPnl, dailyLossSoFar, positions } = useDashboard()
  const pnlPct = ((currentEquity - startingCapital) / startingCapital * 100).toFixed(2)
  const pnlPos = dailyPnl >= 0

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <KpiCard label="Equity" value={`₹${currentEquity.toLocaleString('en-IN')}`} />
        <KpiCard label="Day P&L" value={`${pnlPos ? '+' : ''}₹${dailyPnl.toLocaleString('en-IN')}`} sub={`${pnlPos ? '+' : ''}${pnlPct}%`} ok={pnlPos} />
        <KpiCard label="Loss So Far" value={`₹${dailyLossSoFar.toLocaleString('en-IN')}`} ok={dailyLossSoFar === 0} />
        <KpiCard label="Positions" value={String(positions.length)} />
      </div>

      <div className="bg-slate-900 rounded p-4 border border-slate-800">
        <p className="text-xs text-slate-500 mb-3">Equity Curve (today)</p>
        <ResponsiveContainer width="100%" height={220}>
          <AreaChart data={equityCurve}>
            <defs>
              <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#6366f1" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#6366f1" stopOpacity={0} />
              </linearGradient>
            </defs>
            <XAxis dataKey="ts" tick={{ fontSize: 10, fill: '#64748b' }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fontSize: 10, fill: '#64748b' }} axisLine={false} tickLine={false} width={72} tickFormatter={(v: number) => `₹${(v / 1000).toFixed(0)}k`} />
            <Tooltip
              contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 11 }}
              formatter={(v) => [`₹${Number(v).toLocaleString('en-IN')}`, 'Equity']}
            />
            <Area type="monotone" dataKey="equity" stroke="#6366f1" fill="url(#eq)" strokeWidth={2} dot={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {positions.length > 0 && (
        <div className="bg-slate-900 rounded p-4 border border-slate-800">
          <p className="text-xs text-slate-500 mb-3">Open Positions</p>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-500 border-b border-slate-800">
                {['Symbol', 'Dir', 'Qty', 'Avg', 'LTP', 'P&L', 'Product'].map((h) => (
                  <th key={h} className="text-left py-1 pr-4 font-normal">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.symbol} className="border-b border-slate-800/50">
                  <td className="py-1.5 pr-4 text-white font-medium">{p.symbol.split(':').pop()}</td>
                  <td className={`pr-4 ${p.direction === 'long' ? 'text-green-400' : 'text-red-400'}`}>{p.direction.toUpperCase()}</td>
                  <td className="pr-4 text-slate-300">{p.quantity}</td>
                  <td className="pr-4 text-slate-300">₹{p.avgPrice.toFixed(2)}</td>
                  <td className="pr-4 text-slate-300">₹{p.ltp.toFixed(2)}</td>
                  <td className={`pr-4 ${p.pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>{p.pnl >= 0 ? '+' : ''}₹{p.pnl.toFixed(0)}</td>
                  <td className="text-slate-500">{p.product}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function KpiCard({ label, value, sub, ok }: { label: string; value: string; sub?: string; ok?: boolean }) {
  const col = ok === undefined ? 'text-white' : ok ? 'text-green-400' : 'text-red-400'
  return (
    <div className="bg-slate-900 rounded p-4 border border-slate-800">
      <p className="text-xs text-slate-500 mb-1">{label}</p>
      <p className={`text-xl font-bold ${col}`}>{value}</p>
      {sub && <p className={`text-xs ${col} opacity-70`}>{sub}</p>}
    </div>
  )
}
