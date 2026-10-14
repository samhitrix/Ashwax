import {
  Activity,
  ArrowDown,
  ArrowUp,
  BarChart3,
  DollarSign,
  TrendingDown,
  TrendingUp,
  Target,
  Percent,
  Clock,
} from 'lucide-react';
import { useTradingStore } from '../store/tradingStore';
import { cn } from '../utils/cn';
import { XAxis, YAxis, Tooltip, ResponsiveContainer, Area, AreaChart } from 'recharts';
import { formatDistanceToNowStrict } from 'date-fns';

export function Overview() {
  const { positions, agentState, marketContext, funds, pnlHistory } = useTradingStore();
  // Intraday P&L curve = running total of open (unrealized) + today's already
  // closed (realized) P&L, sampled each refresh into the store — so a position
  // squared off mid-day doesn't drop out of the day's total.
  const pnlData = pnlHistory;

  // No local polling timer here: App.tsx drives refreshData()/refreshAdvancedData()
  // globally for every tab. A second timer on top of that one only added
  // duplicate in-flight requests to an API that was already saturated.

  const winningPositions = positions.filter((p) => p.pnl > 0);
  const losingPositions = positions.filter((p) => p.pnl < 0);

  // Total unrealized P&L calculated from positions
  const _totalUnrealizedPnL = positions.reduce((acc, p) => acc + p.pnl, 0);
  void _totalUnrealizedPnL; // Used for future features
  const avgWin = winningPositions.length > 0
    ? winningPositions.reduce((acc, p) => acc + p.pnl, 0) / winningPositions.length
    : 0;
  const avgLoss = losingPositions.length > 0
    ? Math.abs(losingPositions.reduce((acc, p) => acc + p.pnl, 0) / losingPositions.length)
    : 0;

  return (
    <div className="space-y-6">
      {/* Header Stats */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-3">
            <div className={cn(
              'w-10 h-10 rounded-lg flex items-center justify-center',
              agentState.dailyPnL >= 0 ? 'bg-green-500/20' : 'bg-red-500/20'
            )}>
              <DollarSign className={cn(
                'w-5 h-5',
                agentState.dailyPnL >= 0 ? 'text-green-400' : 'text-red-400'
              )} />
            </div>
            <div>
              <div className="text-xs text-gray-400">Today's P&L</div>
              <div className={cn(
                'text-xl font-bold font-mono',
                agentState.dailyPnL >= 0 ? 'text-green-400' : 'text-red-400'
              )}>
                {agentState.dailyPnL >= 0 ? '+' : ''}₹{agentState.dailyPnL.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
              </div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-10 h-10 rounded-lg bg-emerald-500/20 flex items-center justify-center">
              <DollarSign className="w-5 h-5 text-emerald-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Available Cash</div>
              <div className="text-xl font-bold font-mono">
                {funds
                  ? `₹${funds.availableCash.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
                  : <span className="text-sm text-gray-500">Not connected</span>}
              </div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-10 h-10 rounded-lg bg-orange-500/20 flex items-center justify-center">
              <BarChart3 className="w-5 h-5 text-orange-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Margin (used / total)</div>
              <div className="text-xl font-bold font-mono">
                {funds
                  ? `₹${funds.usedMargin.toLocaleString('en-IN', { maximumFractionDigits: 0 })} / ₹${(funds.net + funds.usedMargin).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
                  : <span className="text-sm text-gray-500">Not connected</span>}
              </div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center">
              <Target className="w-5 h-5 text-blue-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Open Positions</div>
              <div className="text-xl font-bold">
                {agentState.openPositions}
              </div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-10 h-10 rounded-lg bg-green-500/20 flex items-center justify-center">
              <TrendingUp className="w-5 h-5 text-green-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Winning</div>
              <div className="text-xl font-bold text-green-400">
                {winningPositions.length}
                <span className="text-sm text-gray-400 ml-2">Avg: ₹{avgWin.toFixed(0)}</span>
              </div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-10 h-10 rounded-lg bg-red-500/20 flex items-center justify-center">
              <TrendingDown className="w-5 h-5 text-red-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Losing</div>
              <div className="text-xl font-bold text-red-400">
                {losingPositions.length}
                <span className="text-sm text-gray-400 ml-2">Avg: ₹{avgLoss.toFixed(0)}</span>
              </div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-10 h-10 rounded-lg bg-purple-500/20 flex items-center justify-center">
              <BarChart3 className="w-5 h-5 text-purple-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Exposure</div>
              <div className="text-xl font-bold">
                ₹{(agentState.totalExposure / 1000).toFixed(0)}K
              </div>
            </div>
          </div>
        </div>

      </div>

      {/* Charts Row */}
      <div className="grid grid-cols-3 gap-6">
        {/* P&L Chart */}
        <div className="col-span-2 bg-[#161b22] rounded-xl border border-gray-800 p-4">
          <h3 className="font-semibold text-white mb-4 flex items-center gap-2">
            <Activity className="w-5 h-5 text-blue-400" />
            Intraday P&L
          </h3>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={pnlData}>
                <defs>
                  <linearGradient id="pnlGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#22c55e" stopOpacity={0.3}/>
                    <stop offset="95%" stopColor="#22c55e" stopOpacity={0}/>
                  </linearGradient>
                </defs>
                <XAxis
                  dataKey="time"
                  stroke="#4b5563"
                  fontSize={12}
                  tickLine={false}
                />
                <YAxis
                  stroke="#4b5563"
                  fontSize={12}
                  tickLine={false}
                  tickFormatter={(v) => `₹${v}`}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: '#161b22',
                    border: '1px solid #374151',
                    borderRadius: '8px',
                  }}
                  labelStyle={{ color: '#9ca3af' }}
                  formatter={(value) => [`₹${Number(value).toLocaleString()}`, 'P&L']}
                />
                <Area
                  type="monotone"
                  dataKey="pnl"
                  stroke="#22c55e"
                  strokeWidth={2}
                  fill="url(#pnlGradient)"
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* Market Context */}
        <div className="bg-[#161b22] rounded-xl border border-gray-800 p-4">
          <h3 className="font-semibold text-white mb-4 flex items-center gap-2">
            <BarChart3 className="w-5 h-5 text-purple-400" />
            Market Context
          </h3>
          <div className="space-y-4">
            <div className="flex justify-between items-center">
              <span className="text-gray-400">NIFTY 50</span>
              <div className="text-right">
                <div className="font-mono font-semibold">{marketContext.niftyLevel.toLocaleString()}</div>
                <div className={cn(
                  'text-sm',
                  marketContext.niftyChangePercent >= 0 ? 'text-green-400' : 'text-red-400'
                )}>
                  {marketContext.niftyChangePercent >= 0 ? '+' : ''}{marketContext.niftyChangePercent.toFixed(2)}%
                </div>
              </div>
            </div>

            <div className="flex justify-between items-center">
              <span className="text-gray-400">VIX</span>
              <div className="text-right">
                <div className="font-mono font-semibold">{marketContext.vixLevel.toFixed(2)}</div>
                <div className={cn(
                  'text-sm',
                  marketContext.vixChange <= 0 ? 'text-green-400' : 'text-red-400'
                )}>
                  {marketContext.vixChange > 0 ? '+' : ''}{marketContext.vixChange.toFixed(2)}
                </div>
              </div>
            </div>

            <div className="flex justify-between items-center">
              <span className="text-gray-400">Trend</span>
              <span className={cn(
                'px-2 py-1 rounded text-sm',
                marketContext.marketTrend === 'BULLISH' ? 'bg-green-500/20 text-green-400' :
                marketContext.marketTrend === 'BEARISH' ? 'bg-red-500/20 text-red-400' :
                'bg-yellow-500/20 text-yellow-400'
              )}>
                {marketContext.marketTrend}
              </span>
            </div>

            <div className="flex justify-between items-center">
              <span className="text-gray-400">
                Intraday Trend
                <span className="ml-1 text-[10px] text-gray-500">(today, 5-min refresh)</span>
              </span>
              <span className={cn(
                'px-2 py-1 rounded text-sm',
                marketContext.intradayTrend === 'BULLISH' ? 'bg-green-500/20 text-green-400' :
                marketContext.intradayTrend === 'BEARISH' ? 'bg-red-500/20 text-red-400' :
                marketContext.intradayTrend === 'SIDEWAYS' ? 'bg-yellow-500/20 text-yellow-400' :
                'bg-gray-500/20 text-gray-400'
              )}>
                {marketContext.intradayTrend}
              </span>
            </div>

            <div className="border-t border-gray-700 pt-4">
              <div className="text-xs text-gray-400 mb-2">Market Breadth</div>
              <div className="flex items-center gap-2">
                <div className="flex-1 h-2 bg-gray-700 rounded-full overflow-hidden flex">
                  <div
                    className="h-full bg-green-500"
                    style={{ width: `${(marketContext.breadth.advances / (marketContext.breadth.advances + marketContext.breadth.declines)) * 100}%` }}
                  />
                  <div
                    className="h-full bg-red-500"
                    style={{ width: `${(marketContext.breadth.declines / (marketContext.breadth.advances + marketContext.breadth.declines)) * 100}%` }}
                  />
                </div>
              </div>
              <div className="flex justify-between text-xs mt-1">
                <span className="text-green-400">{marketContext.breadth.advances} ▲</span>
                <span className="text-red-400">{marketContext.breadth.declines} ▼</span>
              </div>
            </div>

            <div className="border-t border-gray-700 pt-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <div className="text-xs text-gray-400 mb-1">FII Activity</div>
                  <div className={cn(
                    'text-base font-mono font-semibold',
                    marketContext.fiiActivity.netCash >= 0 ? 'text-green-400' : 'text-red-400'
                  )}>
                    {marketContext.fiiActivity.netCash >= 0 ? '+' : ''}₹{marketContext.fiiActivity.netCash.toFixed(0)} Cr
                  </div>
                </div>
                <div>
                  <div className="text-xs text-gray-400 mb-1">DII Activity</div>
                  <div className={cn(
                    'text-base font-mono font-semibold',
                    marketContext.diiActivity.netCash >= 0 ? 'text-green-400' : 'text-red-400'
                  )}>
                    {marketContext.diiActivity.netCash >= 0 ? '+' : ''}₹{marketContext.diiActivity.netCash.toFixed(0)} Cr
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Positions Table */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
        <div className="p-4 border-b border-gray-800">
          <h3 className="font-semibold text-white flex items-center gap-2">
            <Target className="w-5 h-5 text-blue-400" />
            Open Positions
          </h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="bg-[#0d1117] text-xs text-gray-400">
                <th className="text-left p-3">Symbol</th>
                <th className="text-left p-3">Direction</th>
                <th className="text-right p-3">Qty</th>
                <th className="text-right p-3">Avg Price</th>
                <th className="text-right p-3">LTP</th>
                <th className="text-right p-3">P&L</th>
                <th className="text-right p-3">SL</th>
                <th className="text-right p-3">Target</th>
                <th className="text-left p-3">Strategy</th>
                <th className="text-right p-3">Holding</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((pos) => (
                <tr key={pos.id} className="border-t border-gray-800 hover:bg-gray-800/50">
                  <td className="p-3">
                    <span className="font-semibold text-white">{pos.tradingSymbol}</span>
                    <span className="text-xs text-gray-500 ml-2">{pos.exchange}</span>
                    {pos.external && (
                      <span className="text-[10px] px-1.5 py-0.5 ml-2 rounded bg-amber-500/20 text-amber-400 font-medium">
                        EXTERNAL
                      </span>
                    )}
                  </td>
                  <td className="p-3">
                    <span className={cn(
                      'flex items-center gap-1 text-sm',
                      pos.direction === 'LONG' ? 'text-green-400' : 'text-red-400'
                    )}>
                      {pos.direction === 'LONG' ? <ArrowUp className="w-4 h-4" /> : <ArrowDown className="w-4 h-4" />}
                      {pos.direction}
                    </span>
                  </td>
                  <td className="p-3 text-right font-mono">{pos.quantity}</td>
                  <td className="p-3 text-right font-mono">₹{pos.averagePrice.toFixed(2)}</td>
                  <td className="p-3 text-right font-mono">₹{pos.ltp.toFixed(2)}</td>
                  <td className={cn(
                    'p-3 text-right font-mono font-semibold',
                    pos.pnl >= 0 ? 'text-green-400' : 'text-red-400'
                  )}>
                    {pos.pnl >= 0 ? '+' : ''}₹{pos.pnl.toFixed(2)}
                    <span className="text-xs ml-1">({pos.pnlPercent.toFixed(2)}%)</span>
                  </td>
                  <td className="p-3 text-right font-mono text-red-400">
                    {pos.external || !pos.currentSL ? '—' : `₹${pos.currentSL.toFixed(2)}`}
                  </td>
                  <td className="p-3 text-right font-mono text-green-400">
                    {pos.external || !pos.currentTarget ? '—' : `₹${pos.currentTarget.toFixed(2)}`}
                  </td>
                  <td className="p-3">
                    <span className="text-xs px-2 py-1 rounded bg-gray-700 text-gray-300">
                      {pos.strategy || '—'}
                    </span>
                  </td>
                  <td className="p-3 text-right text-sm text-gray-400 flex items-center gap-1 justify-end">
                    <Clock className="w-3 h-3" />
                    {formatDistanceToNowStrict(pos.entryTimestamp)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Sector Rotation */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-4">
        <h3 className="font-semibold text-white mb-4 flex items-center gap-2">
          <Percent className="w-5 h-5 text-cyan-400" />
          Sector Performance
        </h3>
        <div className="grid grid-cols-4 gap-4">
          {marketContext.sectorRotation.map((sector) => (
            <div
              key={sector.sector}
              className={cn(
                'p-3 rounded-lg border',
                sector.trend === 'LEADING' ? 'bg-green-500/10 border-green-500/30' :
                sector.trend === 'LAGGING' ? 'bg-red-500/10 border-red-500/30' :
                'bg-gray-800 border-gray-700'
              )}
            >
              <div className="flex justify-between items-center">
                <span className="font-medium text-sm">{sector.sector}</span>
                <span className={cn(
                  'text-xs px-2 py-0.5 rounded',
                  sector.trend === 'LEADING' ? 'bg-green-500/20 text-green-400' :
                  sector.trend === 'LAGGING' ? 'bg-red-500/20 text-red-400' :
                  'bg-gray-700 text-gray-400'
                )}>
                  {sector.trend}
                </span>
              </div>
              <div className={cn(
                'text-lg font-mono mt-1',
                sector.performance >= 0 ? 'text-green-400' : 'text-red-400'
              )}>
                {sector.performance >= 0 ? '+' : ''}{sector.performance.toFixed(2)}%
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
