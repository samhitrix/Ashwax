import { useEffect, useState } from 'react';
import {
  AlertTriangle,
  ArrowRight,
  Bell,
  Calendar,
  ChevronDown,
  ChevronUp,
  Globe,
  Newspaper,
  TrendingDown,
  TrendingUp,
} from 'lucide-react';
import { useAdvancedStore } from '../store/advancedStore';
import { cn } from '../utils/cn';
import { format, formatDistanceToNow } from 'date-fns';

export function RealTimeNews() {
  const {
    realTimeNews,
    scheduledEvents,
    sectorSentiments,
    dismissNews,
    refreshNews,
    refreshSectorSentiments,
    refreshUpcomingEvents,
  } = useAdvancedStore();

  const [expandedNews, setExpandedNews] = useState<string | null>(null);

  useEffect(() => {
    refreshNews();
    refreshSectorSentiments();
    refreshUpcomingEvents();
    const id = setInterval(() => {
      refreshNews();
      refreshSectorSentiments();
      refreshUpcomingEvents();
    }, 30_000);
    return () => clearInterval(id);
  }, []);

  const sentimentColors = {
    VERY_POSITIVE: 'text-green-400 bg-green-500/20',
    POSITIVE: 'text-green-300 bg-green-500/15',
    NEUTRAL: 'text-gray-400 bg-gray-500/20',
    NEGATIVE: 'text-red-300 bg-red-500/15',
    VERY_NEGATIVE: 'text-red-400 bg-red-500/20',
  };

  const impactColors = {
    MINIMAL: 'text-gray-400',
    LOW: 'text-blue-400',
    MEDIUM: 'text-yellow-400',
    HIGH: 'text-orange-400',
    CRITICAL: 'text-red-400',
  };

  const actionColors = {
    BUY: 'bg-green-500 text-white',
    SELL: 'bg-red-500 text-white',
    HOLD: 'bg-blue-500/20 text-blue-400',
    EXIT: 'bg-red-500 text-white',
    AVOID: 'bg-gray-500/20 text-gray-400',
    WATCH: 'bg-yellow-500/20 text-yellow-400',
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Real-Time News Intelligence</h2>
          <p className="text-gray-400">AI-analyzed news with instant trading recommendations</p>
        </div>
        <div className="flex items-center gap-2">
          <span className="flex items-center gap-2 px-3 py-1.5 bg-green-500/20 text-green-400 rounded-full text-sm">
            <div className="w-2 h-2 rounded-full bg-green-400 animate-pulse" />
            Live Feed Active
          </span>
        </div>
      </div>

      {/* Breaking News Alert */}
      {realTimeNews.filter((n) => n.category === 'BREAKING').map((news) => (
        <div
          key={news.id}
          className="bg-red-500/10 border border-red-500/30 rounded-xl p-4 animate-pulse"
          style={{ animationDuration: '2s' }}
        >
          <div className="flex items-start justify-between">
            <div className="flex items-start gap-4">
              <div className="w-12 h-12 rounded-lg bg-red-500/20 flex items-center justify-center">
                <Bell className="w-6 h-6 text-red-400" />
              </div>
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs px-2 py-0.5 rounded bg-red-500 text-white font-bold">
                    BREAKING
                  </span>
                  <span className="text-xs text-gray-400">
                    {formatDistanceToNow(news.timestamps.published)} ago
                  </span>
                </div>
                <h3 className="font-bold text-white text-lg">{news.headline}</h3>
                <p className="text-gray-300 mt-2">{news.summary}</p>

                {news.agentAction.affectedPositions.length > 0 && (
                  <div className="mt-3 p-3 bg-yellow-500/10 border border-yellow-500/30 rounded-lg">
                    <div className="flex items-center gap-2 text-yellow-400 text-sm font-medium">
                      <AlertTriangle className="w-4 h-4" />
                      Affects your positions: {news.symbols.filter((s) =>
                        news.agentAction.affectedPositions.some((p) => p.includes(s))
                      ).join(', ')}
                    </div>
                  </div>
                )}
              </div>
            </div>
            <div className="flex flex-col items-end gap-2">
              <span className={cn(
                'px-3 py-1 rounded text-sm font-bold',
                actionColors[news.agentAction.recommendation]
              )}>
                {news.agentAction.recommendation}
              </span>
              <span className="text-xs text-gray-400">
                Confidence: {(news.agentAction.confidence * 100).toFixed(0)}%
              </span>
            </div>
          </div>
        </div>
      ))}

      <div className="grid grid-cols-3 gap-6">
        {/* News Feed */}
        <div className="col-span-2 space-y-4">
          <h3 className="font-semibold text-white flex items-center gap-2">
            <Newspaper className="w-5 h-5 text-blue-400" />
            News Feed
          </h3>

          {realTimeNews.filter((n) => n.category !== 'BREAKING').length === 0 && (
            <div className="bg-[#161b22] rounded-xl border border-gray-800 p-12 text-center">
              <Newspaper className="w-12 h-12 text-gray-600 mx-auto mb-4" />
              <h3 className="text-lg font-semibold text-gray-400">No News Yet</h3>
              <p className="text-gray-500 mt-2 text-sm">Start <code className="text-blue-400 font-mono">python launcher.py</code> to begin receiving live news from the intelligence engine.</p>
              <p className="text-gray-600 mt-1 text-xs">News will appear here automatically once the system is running.</p>
            </div>
          )}
          {realTimeNews.filter((n) => n.category !== 'BREAKING').map((news) => (
            <div
              key={news.id}
              className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden"
            >
              <div
                className="p-4 cursor-pointer hover:bg-gray-800/50 transition-colors"
                onClick={() => setExpandedNews(expandedNews === news.id ? null : news.id)}
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-2">
                      <span className={cn(
                        'text-xs px-2 py-0.5 rounded',
                        sentimentColors[news.sentiment.label]
                      )}>
                        {news.sentiment.label.replace('_', ' ')}
                      </span>
                      <span className={cn(
                        'text-xs',
                        impactColors[news.impact.level]
                      )}>
                        {news.impact.level} IMPACT
                      </span>
                      <span className="text-xs text-gray-500">
                        {news.source} • {formatDistanceToNow(news.timestamps.published)} ago
                      </span>
                    </div>
                    <h4 className="font-semibold text-white">{news.headline}</h4>
                    <p className="text-sm text-gray-400 mt-1 line-clamp-2">{news.summary}</p>

                    {Array.isArray(news.symbols) && news.symbols.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-2">
                        {news.symbols.slice(0, 5).map((symbol) => (
                          <span key={symbol} className="text-xs px-2 py-0.5 rounded bg-gray-700 text-gray-300 font-mono">
                            {symbol}
                          </span>
                        ))}
                        {news.symbols.length > 5 && (
                          <span className="text-xs text-gray-500">+{news.symbols.length - 5} more</span>
                        )}
                      </div>
                    )}
                  </div>

                  <div className="flex flex-col items-end gap-2 ml-4">
                    <span className={cn(
                      'px-2 py-1 rounded text-xs font-semibold',
                      actionColors[news.agentAction.recommendation]
                    )}>
                      {news.agentAction.recommendation}
                    </span>
                    {expandedNews === news.id ? (
                      <ChevronUp className="w-5 h-5 text-gray-400" />
                    ) : (
                      <ChevronDown className="w-5 h-5 text-gray-400" />
                    )}
                  </div>
                </div>
              </div>

              {expandedNews === news.id && (
                <div className="border-t border-gray-800 p-4 space-y-4">
                  <div className="bg-[#0d1117] rounded-lg p-4">
                    <h5 className="text-sm font-medium text-blue-400 mb-2">🤖 Agent Analysis</h5>
                    <p className="text-sm text-gray-300">{news.agentAction.reasoning}</p>
                    <div className="flex items-center gap-4 mt-3">
                      <span className="text-xs text-gray-400">
                        Confidence: <span className="text-white font-mono">{(news.agentAction.confidence * 100).toFixed(0)}%</span>
                      </span>
                    </div>
                  </div>

                  {news.agentAction.suggestedActions.length > 0 && (
                    <div className="space-y-2">
                      <h5 className="text-sm font-medium text-yellow-400">Suggested Actions</h5>
                      {news.agentAction.suggestedActions.map((action, i) => (
                        <div key={i} className="flex items-center justify-between bg-yellow-500/10 rounded-lg p-3">
                          <span className="text-sm text-gray-300">{action.action}</span>
                          <span className={cn(
                            'text-xs px-2 py-0.5 rounded',
                            action.urgency === 'IMMEDIATE' ? 'bg-red-500 text-white' :
                            action.urgency === 'HIGH' ? 'bg-orange-500/20 text-orange-400' :
                            'bg-gray-700 text-gray-300'
                          )}>
                            {action.urgency}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  <div className="flex justify-between items-center pt-2">
                    <div className="text-xs text-gray-500">
                      Published: {format(news.timestamps.published, 'HH:mm:ss')}
                    </div>
                    <button
                      onClick={() => dismissNews(news.id)}
                      className="text-xs text-gray-400 hover:text-white transition-colors"
                    >
                      Dismiss
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>

        {/* Sidebar */}
        <div className="space-y-6">
          {/* Upcoming Events */}
          <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
            <div className="p-4 border-b border-gray-800">
              <h3 className="font-semibold text-white flex items-center gap-2">
                <Calendar className="w-5 h-5 text-purple-400" />
                Upcoming Events
              </h3>
            </div>
            <div className="p-4 space-y-3">
              {scheduledEvents.length === 0 && (
                <p className="text-xs text-gray-500 text-center py-4">No upcoming events — system offline</p>
              )}
              {scheduledEvents.slice(0, 4).map((event) => (
                <div key={event.id} className="bg-[#0d1117] rounded-lg p-3">
                  <div className="flex items-start justify-between">
                    <div>
                      <div className={cn(
                        'text-xs px-2 py-0.5 rounded inline-block mb-1',
                        event.expectedImpact === 'CRITICAL' ? 'bg-red-500/20 text-red-400' :
                        event.expectedImpact === 'HIGH' ? 'bg-orange-500/20 text-orange-400' :
                        'bg-blue-500/20 text-blue-400'
                      )}>
                        {event.type.replace('_', ' ')}
                      </div>
                      <h4 className="font-medium text-white text-sm">{event.title}</h4>
                      <div className="text-xs text-gray-400 mt-1">
                        {format(event.date, 'dd MMM')} {event.time && `• ${event.time}`}
                      </div>
                    </div>
                    {event.blackoutStart && (
                      <span className="text-xs px-2 py-0.5 rounded bg-yellow-500/20 text-yellow-400">
                        Blackout
                      </span>
                    )}
                  </div>
                  {event.positionSizeReduction && (
                    <div className="text-xs text-orange-400 mt-2">
                      Position size -{event.positionSizeReduction}%
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>

          {/* Sector Sentiment */}
          <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
            <div className="p-4 border-b border-gray-800">
              <h3 className="font-semibold text-white flex items-center gap-2">
                <Globe className="w-5 h-5 text-cyan-400" />
                Sector Sentiment
              </h3>
            </div>
            <div className="p-4 space-y-3">
              {sectorSentiments.length === 0 && (
                <p className="text-xs text-gray-500 text-center py-4">No sector data — system offline</p>
              )}
              {sectorSentiments.map((sector) => (
                <div key={sector.sector} className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    {sector.trend === 'IMPROVING' ? (
                      <TrendingUp className="w-4 h-4 text-green-400" />
                    ) : sector.trend === 'DECLINING' ? (
                      <TrendingDown className="w-4 h-4 text-red-400" />
                    ) : (
                      <ArrowRight className="w-4 h-4 text-gray-400" />
                    )}
                    <span className="text-sm text-white">{sector.sector}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="w-16 bg-gray-700 rounded-full h-2">
                      <div
                        className={cn(
                          'h-2 rounded-full',
                          sector.currentScore > 0.3 ? 'bg-green-500' :
                          sector.currentScore < -0.3 ? 'bg-red-500' : 'bg-gray-500'
                        )}
                        style={{ width: `${Math.abs(sector.currentScore) * 100}%` }}
                      />
                    </div>
                    <span className={cn(
                      'text-xs px-2 py-0.5 rounded',
                      sector.recommendation === 'OVERWEIGHT' ? 'bg-green-500/20 text-green-400' :
                      sector.recommendation === 'UNDERWEIGHT' ? 'bg-red-500/20 text-red-400' :
                      'bg-gray-500/20 text-gray-400'
                    )}>
                      {sector.recommendation}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
