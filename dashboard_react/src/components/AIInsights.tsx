import {
  AlertTriangle,
  ArrowRight,
  Brain,
  CheckCircle2,
  Lightbulb,
  Shield,
  Target,
  TrendingUp,
  XCircle,
  Zap,
} from 'lucide-react';
import { useTradingStore } from '../store/tradingStore';
import { cn } from '../utils/cn';
import type { TradingInsight } from '../types/trading';
import { formatDistanceToNow } from 'date-fns';

const insightTypeConfig: Record<string, { icon: typeof Brain; color: string; bg: string; label: string }> = {
  PATTERN: { icon: TrendingUp, color: 'text-blue-400', bg: 'bg-blue-500/20', label: 'Pattern Detected' },
  ANOMALY: { icon: AlertTriangle, color: 'text-orange-400', bg: 'bg-orange-500/20', label: 'Anomaly' },
  OPPORTUNITY: { icon: Target, color: 'text-green-400', bg: 'bg-green-500/20', label: 'Opportunity' },
  RISK: { icon: Shield, color: 'text-red-400', bg: 'bg-red-500/20', label: 'Risk Warning' },
  IMPROVEMENT: { icon: Lightbulb, color: 'text-yellow-400', bg: 'bg-yellow-500/20', label: 'Improvement' },
};

interface InsightCardProps {
  insight: TradingInsight;
}

function InsightCard({ insight }: InsightCardProps) {
  const { acknowledgeInsight } = useTradingStore();
  const config = insightTypeConfig[insight.type];
  const TypeIcon = config.icon;

  return (
    <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
      <div className="p-4">
        <div className="flex items-start gap-4">
          <div className={cn('w-12 h-12 rounded-lg flex items-center justify-center', config.bg)}>
            <TypeIcon className={cn('w-6 h-6', config.color)} />
          </div>

          <div className="flex-1">
            <div className="flex items-center gap-2 mb-1">
              <span className={cn('text-xs px-2 py-0.5 rounded', config.bg, config.color)}>
                {config.label}
              </span>
              <span className="text-xs text-gray-500">
                {formatDistanceToNow(insight.createdAt)} ago
              </span>
            </div>

            <h3 className="font-semibold text-white mb-2">{insight.title}</h3>
            <p className="text-sm text-gray-300 mb-3">{insight.description}</p>

            {/* Confidence Bar */}
            <div className="mb-3">
              <div className="flex justify-between text-xs mb-1">
                <span className="text-gray-400">Confidence</span>
                <span className={cn(
                  'font-mono',
                  insight.confidence >= 0.8 ? 'text-green-400' :
                  insight.confidence >= 0.6 ? 'text-yellow-400' : 'text-orange-400'
                )}>
                  {(insight.confidence * 100).toFixed(0)}%
                </span>
              </div>
              <div className="w-full bg-gray-700 rounded-full h-2">
                <div
                  className={cn(
                    'h-2 rounded-full transition-all',
                    insight.confidence >= 0.8 ? 'bg-green-500' :
                    insight.confidence >= 0.6 ? 'bg-yellow-500' : 'bg-orange-500'
                  )}
                  style={{ width: `${insight.confidence * 100}%` }}
                />
              </div>
            </div>

            {/* Related Data */}
            {Object.keys(insight.relatedData).length > 0 && (
              <div className="bg-[#0d1117] rounded-lg p-3 mb-3">
                <div className="grid grid-cols-3 gap-3">
                  {Object.entries(insight.relatedData).map(([key, value]) => (
                    <div key={key}>
                      <div className="text-xs text-gray-400">{key.replace(/([A-Z])/g, ' $1').trim()}</div>
                      <div className="font-mono text-sm text-white">
                        {typeof value === 'number' ? value.toFixed(2) : String(value)}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Suggested Action */}
            {insight.suggestedAction && (
              <div className={cn(
                'rounded-lg p-3 border',
                insight.actionable
                  ? 'bg-blue-500/10 border-blue-500/30'
                  : 'bg-gray-800 border-gray-700'
              )}>
                <div className="flex items-center gap-2 mb-1">
                  <Zap className={cn(
                    'w-4 h-4',
                    insight.actionable ? 'text-blue-400' : 'text-gray-400'
                  )} />
                  <span className="text-xs font-medium text-gray-400">Suggested Action</span>
                </div>
                <p className="text-sm text-gray-300">{insight.suggestedAction}</p>
              </div>
            )}
          </div>

          <div className="flex flex-col gap-2">
            {insight.actionable && (
              <button className="flex items-center gap-2 px-4 py-2 bg-blue-500 text-white rounded-lg text-sm font-medium hover:bg-blue-600 transition-colors">
                <ArrowRight className="w-4 h-4" />
                Take Action
              </button>
            )}
            <button
              onClick={() => acknowledgeInsight(insight.id)}
              className="flex items-center gap-2 px-4 py-2 bg-gray-700 text-gray-300 rounded-lg text-sm hover:bg-gray-600 transition-colors"
            >
              <CheckCircle2 className="w-4 h-4" />
              Acknowledge
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function AIInsights() {
  const { insights } = useTradingStore();

  const actionableInsights = insights.filter((i) => i.actionable);
  const informationalInsights = insights.filter((i) => !i.actionable);

  const byType = {
    PATTERN: insights.filter((i) => i.type === 'PATTERN').length,
    OPPORTUNITY: insights.filter((i) => i.type === 'OPPORTUNITY').length,
    RISK: insights.filter((i) => i.type === 'RISK').length,
    IMPROVEMENT: insights.filter((i) => i.type === 'IMPROVEMENT').length,
    ANOMALY: insights.filter((i) => i.type === 'ANOMALY').length,
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">AI Insights</h2>
          <p className="text-gray-400">Intelligent analysis and recommendations from the trading agent</p>
        </div>
        <div className="flex items-center gap-2">
          <Brain className="w-6 h-6 text-purple-400" />
          <span className="text-sm text-gray-400">Powered by Gemini 2.0</span>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-5 gap-4">
        {Object.entries(byType).map(([type, count]) => {
          const config = insightTypeConfig[type];
          const TypeIcon = config.icon;
          return (
            <div key={type} className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
              <div className="flex items-center gap-3">
                <div className={cn('w-10 h-10 rounded-lg flex items-center justify-center', config.bg)}>
                  <TypeIcon className={cn('w-5 h-5', config.color)} />
                </div>
                <div>
                  <div className="text-2xl font-bold">{count}</div>
                  <div className="text-xs text-gray-400">{config.label}</div>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Actionable Insights */}
      {actionableInsights.length > 0 && (
        <div>
          <h3 className="font-semibold text-white mb-3 flex items-center gap-2">
            <Zap className="w-5 h-5 text-yellow-400" />
            Actionable Insights ({actionableInsights.length})
          </h3>
          <div className="space-y-4">
            {actionableInsights.map((insight) => (
              <InsightCard key={insight.id} insight={insight} />
            ))}
          </div>
        </div>
      )}

      {/* Informational Insights */}
      {informationalInsights.length > 0 && (
        <div>
          <h3 className="font-semibold text-gray-400 mb-3 flex items-center gap-2">
            <Lightbulb className="w-5 h-5" />
            Informational ({informationalInsights.length})
          </h3>
          <div className="space-y-4">
            {informationalInsights.map((insight) => (
              <InsightCard key={insight.id} insight={insight} />
            ))}
          </div>
        </div>
      )}

      {insights.length === 0 && (
        <div className="bg-[#161b22] rounded-xl border border-gray-800 p-12 text-center">
          <Brain className="w-12 h-12 text-gray-600 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-gray-400">No Active Insights</h3>
          <p className="text-gray-500 mt-2">The AI agent will surface insights as patterns emerge</p>
        </div>
      )}

      {/* AI Capabilities Section */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <h3 className="font-semibold text-white mb-4 flex items-center gap-2">
          <Brain className="w-5 h-5 text-purple-400" />
          Agent Capabilities
        </h3>
        <div className="grid grid-cols-3 gap-6">
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="w-5 h-5 text-green-400" />
              <span className="font-medium">Pattern Recognition</span>
            </div>
            <p className="text-sm text-gray-400 ml-7">
              Identifies VCP, Cup-and-Handle, Stage 2 breakouts, and other technical patterns
            </p>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="w-5 h-5 text-green-400" />
              <span className="font-medium">Risk Analysis</span>
            </div>
            <p className="text-sm text-gray-400 ml-7">
              Monitors sector concentration, position sizing, and portfolio heat
            </p>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="w-5 h-5 text-green-400" />
              <span className="font-medium">Strategy Performance</span>
            </div>
            <p className="text-sm text-gray-400 ml-7">
              Tracks win rates, R:R ratios, and suggests parameter adjustments
            </p>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="w-5 h-5 text-green-400" />
              <span className="font-medium">News Sentiment</span>
            </div>
            <p className="text-sm text-gray-400 ml-7">
              Analyzes news impact on positions and suggests protective actions
            </p>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="w-5 h-5 text-green-400" />
              <span className="font-medium">Anomaly Detection</span>
            </div>
            <p className="text-sm text-gray-400 ml-7">
              Flags unusual volume, price gaps, and execution irregularities
            </p>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <XCircle className="w-5 h-5 text-gray-500" />
              <span className="font-medium text-gray-500">Options Greeks (Coming)</span>
            </div>
            <p className="text-sm text-gray-500 ml-7">
              Phase 3: Real-time Greeks analysis for options positions
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
