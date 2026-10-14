import { useEffect } from 'react';
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Brain,
  CheckCircle2,
  ShieldAlert,
  Target,
  Zap,
} from 'lucide-react';
import { useAdvancedStore } from '../store/advancedStore';
import { useApi } from '../hooks/useApi';
import { cn } from '../utils/cn';
import { format } from 'date-fns';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts';

export function Intelligence() {
  const {
    executionQuality,
    strategyAttributions,
    marketRegime,
    confidenceCalibrations,
    refreshAdvancedData,
  } = useAdvancedStore();

  // Intelligence panels read from the advanced store, which is only populated by
  // refreshAdvancedData(); drive it here so the page works without first visiting
  // the Order Monitor+ / Self-Healing tabs.
  useEffect(() => {
    refreshAdvancedData();
  }, [refreshAdvancedData]);

  const { data: llmSanity } = useApi<{ bypass_llm: boolean; pending_signals: number }>(
    '/api/llm_sanity', 5000
  );

  const regimeColors = {
    TRENDING_UP: 'text-green-400 bg-green-500/20',
    TRENDING_DOWN: 'text-red-400 bg-red-500/20',
    RANGING: 'text-yellow-400 bg-yellow-500/20',
    VOLATILE: 'text-orange-400 bg-orange-500/20',
    CRASH: 'text-red-500 bg-red-500/30',
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Trading Intelligence</h2>
          <p className="text-gray-400">Execution analytics, strategy attribution, and regime detection</p>
        </div>
      </div>

      {/* Market Regime */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <div className="flex items-center justify-between mb-6">
          <h3 className="font-semibold text-white flex items-center gap-2">
            <Activity className="w-5 h-5 text-purple-400" />
            Market Regime Detection
          </h3>
          <span className="text-xs text-gray-400">
            Accuracy: {(marketRegime.historicalAccuracy * 100).toFixed(0)}%
          </span>
        </div>

        <div className="grid grid-cols-4 gap-6">
          <div className="col-span-1">
            <div className={cn(
              'rounded-xl p-6 text-center',
              regimeColors[marketRegime.current]
            )}>
              <div className="text-3xl font-bold mb-2">
                {marketRegime.current.replace('_', ' ')}
              </div>
              <div className="text-sm opacity-80">
                {(marketRegime.confidence * 100).toFixed(0)}% confidence
              </div>
              <div className="text-xs mt-2 opacity-60">
                Since {format(marketRegime.since, 'dd MMM')}
              </div>
            </div>
            <div className="mt-4 text-sm text-gray-400">
              Position multiplier: <span className="text-white font-mono">{marketRegime.positionSizeMultiplier}x</span>
            </div>
          </div>

          <div className="col-span-2">
            <div className="grid grid-cols-3 gap-4 mb-4">
              <div className="bg-[#0d1117] rounded-lg p-3">
                <div className="text-xs text-gray-400">ADX</div>
                <div className={cn(
                  'text-xl font-mono font-bold',
                  marketRegime.indicators.adx > 25 ? 'text-green-400' : 'text-yellow-400'
                )}>
                  {marketRegime.indicators.adx}
                </div>
                <div className="text-xs text-gray-500">
                  {marketRegime.indicators.adx > 25 ? 'Trending' : 'Weak'}
                </div>
              </div>
              <div className="bg-[#0d1117] rounded-lg p-3">
                <div className="text-xs text-gray-400">ATR %</div>
                <div className={cn(
                  'text-xl font-mono font-bold',
                  marketRegime.indicators.atrPercent > 2 ? 'text-orange-400' : 'text-green-400'
                )}>
                  {marketRegime.indicators.atrPercent}%
                </div>
                <div className="text-xs text-gray-500">
                  {marketRegime.indicators.atrPercent > 2 ? 'High Vol' : 'Normal'}
                </div>
              </div>
              <div className="bg-[#0d1117] rounded-lg p-3">
                <div className="text-xs text-gray-400">Breadth</div>
                <div className={cn(
                  'text-xl font-mono font-bold',
                  marketRegime.indicators.breadth > 0.5 ? 'text-green-400' : 'text-red-400'
                )}>
                  {(marketRegime.indicators.breadth * 100).toFixed(0)}%
                </div>
                <div className="text-xs text-gray-500">
                  {marketRegime.indicators.breadth > 0.5 ? 'Healthy' : 'Weak'}
                </div>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="bg-[#0d1117] rounded-lg p-3">
                <div className="text-xs text-gray-400">VIX</div>
                <div className={cn(
                  'text-xl font-mono font-bold',
                  marketRegime.indicators.vix > 20 ? 'text-orange-400' : 'text-green-400'
                )}>
                  {marketRegime.indicators.vix}
                </div>
              </div>
              <div className="bg-[#0d1117] rounded-lg p-3">
                <div className="text-xs text-gray-400">Trends</div>
                <div className="flex gap-2 mt-1">
                  <span className={cn(
                    'text-xs px-2 py-0.5 rounded',
                    marketRegime.indicators.trend50 === 'UP' ? 'bg-green-500/20 text-green-400' :
                    marketRegime.indicators.trend50 === 'DOWN' ? 'bg-red-500/20 text-red-400' :
                    'bg-gray-500/20 text-gray-400'
                  )}>
                    50D: {marketRegime.indicators.trend50}
                  </span>
                  <span className={cn(
                    'text-xs px-2 py-0.5 rounded',
                    marketRegime.indicators.trend200 === 'UP' ? 'bg-green-500/20 text-green-400' :
                    marketRegime.indicators.trend200 === 'DOWN' ? 'bg-red-500/20 text-red-400' :
                    'bg-gray-500/20 text-gray-400'
                  )}>
                    200D: {marketRegime.indicators.trend200}
                  </span>
                </div>
              </div>
            </div>
          </div>

          <div className="col-span-1">
            <div className="mb-4">
              <div className="text-xs text-gray-400 mb-2">✅ Suitable Strategies</div>
              <div className="space-y-1">
                {marketRegime.suitableStrategies.map((s) => (
                  <div key={s} className="text-sm text-green-400 flex items-center gap-1">
                    <CheckCircle2 className="w-3 h-3" />
                    {s.replace('_', ' ')}
                  </div>
                ))}
              </div>
            </div>
            <div>
              <div className="text-xs text-gray-400 mb-2">❌ Avoid</div>
              <div className="space-y-1">
                {marketRegime.avoidStrategies.map((s) => (
                  <div key={s} className="text-sm text-red-400 flex items-center gap-1">
                    <AlertTriangle className="w-3 h-3" />
                    {s.replace('_', ' ')}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Strategy Attribution */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
        <div className="p-4 border-b border-gray-800">
          <h3 className="font-semibold text-white flex items-center gap-2">
            <Target className="w-5 h-5 text-blue-400" />
            Strategy Performance Attribution
          </h3>
        </div>
        <div className="p-6 grid grid-cols-2 gap-6">
          {strategyAttributions.length === 0 && (
            <p className="col-span-2 text-sm text-gray-500 text-center py-6">
              No attribution data yet — populates after closed trades are attributed.
            </p>
          )}
          {strategyAttributions.map((strat) => (
            <div key={strat.strategy} className="bg-[#0d1117] rounded-xl p-4">
              <div className="flex items-center justify-between mb-4">
                <h4 className="font-semibold text-white">{strat.strategy.replace('_', ' ')}</h4>
                <span className={cn(
                  'text-sm font-mono',
                  strat.profitFactor > 1.5 ? 'text-green-400' : 'text-yellow-400'
                )}>
                  PF: {strat.profitFactor.toFixed(2)}
                </span>
              </div>

              <div className="grid grid-cols-4 gap-3 mb-4">
                <div className="text-center">
                  <div className="text-xs text-gray-400">Win Rate</div>
                  <div className="text-lg font-mono font-bold text-white">
                    {(strat.winRate * 100).toFixed(0)}%
                  </div>
                </div>
                <div className="text-center">
                  <div className="text-xs text-gray-400">Avg R:R</div>
                  <div className="text-lg font-mono font-bold text-green-400">
                    {strat.avgRR.toFixed(1)}
                  </div>
                </div>
                <div className="text-center">
                  <div className="text-xs text-gray-400">Sharpe</div>
                  <div className="text-lg font-mono font-bold text-blue-400">
                    {strat.sharpeRatio.toFixed(2)}
                  </div>
                </div>
                <div className="text-center">
                  <div className="text-xs text-gray-400">Max DD</div>
                  <div className="text-lg font-mono font-bold text-red-400">
                    {strat.maxDrawdown.toFixed(1)}%
                  </div>
                </div>
              </div>

              <div className="mb-4">
                <div className="text-xs text-gray-400 mb-2">P&L Attribution</div>
                <div className="h-24">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={strat.attributions} layout="vertical">
                      <XAxis type="number" hide />
                      <YAxis dataKey="factor" type="category" width={80} tick={{ fill: '#9ca3af', fontSize: 10 }} />
                      <Tooltip
                        contentStyle={{
                          backgroundColor: '#161b22',
                          border: '1px solid #374151',
                          borderRadius: '8px',
                        }}
                        formatter={(value) => [`${Number(value)}%`, 'Contribution']}
                      />
                      <Bar dataKey="contribution" radius={[0, 4, 4, 0]}>
                        {strat.attributions.map((entry, index) => (
                          <Cell
                            key={`cell-${index}`}
                            fill={entry.contribution > 20 ? '#22c55e' : entry.contribution > 10 ? '#eab308' : '#6b7280'}
                          />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>

              <div>
                <div className="text-xs text-gray-400 mb-2">Recommendations</div>
                <div className="space-y-1">
                  {strat.recommendations.slice(0, 2).map((rec, i) => (
                    <div key={i} className="text-xs text-gray-300 flex items-start gap-1">
                      <ArrowRight className="w-3 h-3 text-blue-400 mt-0.5 flex-shrink-0" />
                      {rec}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Execution Quality */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
        <div className="p-4 border-b border-gray-800">
          <h3 className="font-semibold text-white flex items-center gap-2">
            <Zap className="w-5 h-5 text-yellow-400" />
            Execution Quality Analysis
          </h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="bg-[#0d1117] text-xs text-gray-400">
                <th className="text-left p-3">Symbol</th>
                <th className="text-right p-3">Trades</th>
                <th className="text-right p-3">Avg Slippage</th>
                <th className="text-right p-3">Fill Time</th>
                <th className="text-right p-3">Limit Fill %</th>
                <th className="text-right p-3">SL Accuracy</th>
                <th className="text-left p-3">Best Time</th>
                <th className="text-left p-3">Recommendation</th>
              </tr>
            </thead>
            <tbody>
              {executionQuality.length === 0 && (
                <tr>
                  <td colSpan={8} className="p-6 text-center text-sm text-gray-500">
                    No execution data yet — populates after trades fill.
                  </td>
                </tr>
              )}
              {executionQuality.map((eq) => (
                <tr key={eq.symbol} className="border-t border-gray-800 hover:bg-gray-800/50">
                  <td className="p-3 font-semibold text-white">{eq.symbol.split(':').pop()}</td>
                  <td className="p-3 text-right font-mono">{eq.totalTrades}</td>
                  <td className="p-3 text-right">
                    <span className={cn(
                      'font-mono',
                      eq.avgSlippagePct < 0.1 ? 'text-green-400' :
                      eq.avgSlippagePct < 0.2 ? 'text-yellow-400' : 'text-red-400'
                    )}>
                      {eq.avgSlippagePct.toFixed(2)}%
                    </span>
                  </td>
                  <td className="p-3 text-right font-mono">{eq.avgFillTimeMs}ms</td>
                  <td className="p-3 text-right">
                    <span className={cn(
                      'font-mono',
                      eq.limitFillRate > 0.9 ? 'text-green-400' :
                      eq.limitFillRate > 0.8 ? 'text-yellow-400' : 'text-red-400'
                    )}>
                      {(eq.limitFillRate * 100).toFixed(0)}%
                    </span>
                  </td>
                  <td className="p-3 text-right">
                    <span className={cn(
                      'font-mono',
                      eq.slTriggerAccuracy > 0.95 ? 'text-green-400' : 'text-yellow-400'
                    )}>
                      {(eq.slTriggerAccuracy * 100).toFixed(0)}%
                    </span>
                  </td>
                  <td className="p-3 text-sm text-gray-300">{eq.bestTimeOfDay}</td>
                  <td className="p-3 text-sm text-gray-400">{eq.recommendation}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Confidence Calibration */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
        <div className="p-4 border-b border-gray-800">
          <h3 className="font-semibold text-white flex items-center gap-2">
            <Brain className="w-5 h-5 text-purple-400" />
            LLM Confidence Calibration
          </h3>
          <p className="text-sm text-gray-400 mt-1">
            How accurate are the AI's confidence predictions?
          </p>
        </div>
        <div className="p-6 grid grid-cols-2 gap-6">
          {confidenceCalibrations.length === 0 && (
            <p className="col-span-2 text-sm text-gray-500 text-center py-6">
              No calibration data yet — populates after the weekly LLM calibration run.
            </p>
          )}
          {confidenceCalibrations.map((cal) => (
            <div key={cal.strategy} className="bg-[#0d1117] rounded-xl p-4">
              <div className="flex items-center justify-between mb-4">
                <h4 className="font-semibold text-white">{cal.strategy.replace('_', ' ')}</h4>
                <span className={cn(
                  'text-xs px-2 py-1 rounded',
                  cal.overallCalibration === 'WELL_CALIBRATED' ? 'bg-green-500/20 text-green-400' :
                  cal.overallCalibration === 'OVERCONFIDENT' ? 'bg-orange-500/20 text-orange-400' :
                  'bg-blue-500/20 text-blue-400'
                )}>
                  {cal.overallCalibration.replace('_', ' ')}
                </span>
              </div>

              <div className="space-y-2 mb-4">
                {cal.calibrationData.map((cd, i) => (
                  <div key={i} className="flex items-center gap-3">
                    <span className="text-xs text-gray-400 w-20">
                      {(cd.predictedConfidence * 100).toFixed(0)}% conf
                    </span>
                    <div className="flex-1 h-4 bg-gray-700 rounded-full overflow-hidden relative">
                      <div
                        className="absolute h-full bg-blue-500/50"
                        style={{ width: `${cd.predictedConfidence * 100}%` }}
                      />
                      <div
                        className={cn(
                          'absolute h-full',
                          cd.isCalibrated ? 'bg-green-500' : 'bg-orange-500'
                        )}
                        style={{ width: `${cd.actualWinRate * 100}%` }}
                      />
                    </div>
                    <span className={cn(
                      'text-xs font-mono w-12',
                      cd.isCalibrated ? 'text-green-400' : 'text-orange-400'
                    )}>
                      {(cd.actualWinRate * 100).toFixed(0)}%
                    </span>
                    <span className="text-xs text-gray-500">
                      n={cd.sampleSize}
                    </span>
                  </div>
                ))}
              </div>

              {cal.suggestedThresholdAdjustment !== 0 && (
                <div className="text-sm text-yellow-400 flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4" />
                  Suggest raising threshold by {(cal.suggestedThresholdAdjustment * 100).toFixed(0)}%
                </div>
              )}

              <div className="text-xs text-gray-500 mt-2">
                {cal.totalPredictions} predictions • Updated {format(cal.lastUpdated, 'dd MMM')}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* LLM Sanity Watchdog */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <h3 className="font-semibold text-white flex items-center gap-2 mb-4">
          <ShieldAlert className="w-5 h-5 text-purple-400" />
          LLM Sanity Watchdog
        </h3>
        <div className="flex items-start gap-6">
          <div className={cn(
            'px-6 py-4 rounded-xl border text-center min-w-[160px]',
            llmSanity?.bypass_llm
              ? 'text-red-400 bg-red-500/20 border-red-500/30'
              : 'text-green-400 bg-green-500/20 border-green-500/30'
          )}>
            <div className="text-lg font-bold">
              {llmSanity?.bypass_llm ? 'BYPASSED' : 'ACTIVE'}
            </div>
            <div className="text-xs mt-1 opacity-70">LLM Gate</div>
          </div>
          <div className="flex-1 space-y-2">
            <div className="flex items-center gap-3">
              {llmSanity?.bypass_llm ? (
                <AlertTriangle className="w-4 h-4 text-red-400 flex-shrink-0" />
              ) : (
                <CheckCircle2 className="w-4 h-4 text-green-400 flex-shrink-0" />
              )}
              <span className={cn('text-sm font-medium', llmSanity?.bypass_llm ? 'text-red-400' : 'text-green-400')}>
                {llmSanity?.bypass_llm
                  ? 'DSL-only mode — LLM gate disabled until 20 clean trades'
                  : 'LLM confirmation gate active — adding alpha'}
              </span>
            </div>
            <div className="text-xs text-gray-500">
              Pending signals: {llmSanity?.pending_signals ?? 0} awaiting LTP replay
            </div>
            <div className="text-xs text-gray-600">
              Gate bypasses when rejected-signal win-rate &gt; approved-trade win-rate over rolling 10 trades
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
