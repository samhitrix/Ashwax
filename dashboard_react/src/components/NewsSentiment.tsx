import { useState } from 'react';
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Bot,
  Clock,
  ExternalLink,
  Filter,
  Globe,
  Newspaper,
  RefreshCw,
  Shield,
  TrendingDown,
  TrendingUp,
  Zap,
} from 'lucide-react';
import { useTradingStore } from '../store/tradingStore';
import { cn } from '../utils/cn';
import type { NewsItem } from '../types/trading';
import { format, formatDistanceToNow } from 'date-fns';

const categoryColors: Record<string, string> = {
  MARKET: 'bg-blue-500/20 text-blue-400',
  SECTOR: 'bg-purple-500/20 text-purple-400',
  STOCK: 'bg-green-500/20 text-green-400',
  MACRO: 'bg-yellow-500/20 text-yellow-400',
  GLOBAL: 'bg-cyan-500/20 text-cyan-400',
  REGULATORY: 'bg-red-500/20 text-red-400',
};

const impactColors: Record<string, string> = {
  LOW: 'bg-gray-500/20 text-gray-400',
  MEDIUM: 'bg-yellow-500/20 text-yellow-400',
  HIGH: 'bg-orange-500/20 text-orange-400',
  CRITICAL: 'bg-red-500/20 text-red-400',
};

const sentimentConfig = {
  POSITIVE: { icon: TrendingUp, color: 'text-green-400', bg: 'bg-green-500/20' },
  NEGATIVE: { icon: TrendingDown, color: 'text-red-400', bg: 'bg-red-500/20' },
  NEUTRAL: { icon: Shield, color: 'text-gray-400', bg: 'bg-gray-500/20' },
};

const actionColors: Record<string, string> = {
  BUY: 'bg-green-500 text-white',
  SELL: 'bg-red-500 text-white',
  HOLD: 'bg-blue-500/20 text-blue-400',
  EXIT: 'bg-orange-500 text-white',
  AVOID: 'bg-gray-500/20 text-gray-400',
};

interface NewsCardProps {
  news: NewsItem;
  positions: string[];
}

function NewsCard({ news, positions }: NewsCardProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const SentimentIcon = sentimentConfig[news.sentiment].icon;

  // Check if news affects any current positions
  const affectedPositions = news.relevantSymbols.filter((s) => positions.includes(s));
  const hasPositionImpact = affectedPositions.length > 0;

  return (
    <div className={cn(
      'bg-[#161b22] rounded-xl border overflow-hidden transition-all',
      hasPositionImpact && news.impactLevel !== 'LOW'
        ? 'border-yellow-500/50 ring-1 ring-yellow-500/20'
        : 'border-gray-800'
    )}>
      <div
        className="p-4 cursor-pointer hover:bg-gray-800/50 transition-colors"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <div className="flex items-start gap-4">
          {/* Sentiment Indicator */}
          <div className={cn(
            'w-12 h-12 rounded-lg flex items-center justify-center flex-shrink-0',
            sentimentConfig[news.sentiment].bg
          )}>
            <SentimentIcon className={cn('w-6 h-6', sentimentConfig[news.sentiment].color)} />
          </div>

          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap mb-1">
              <span className={cn('text-xs px-2 py-0.5 rounded', categoryColors[news.category])}>
                {news.category}
              </span>
              <span className={cn('text-xs px-2 py-0.5 rounded', impactColors[news.impactLevel])}>
                {news.impactLevel} IMPACT
              </span>
              {hasPositionImpact && (
                <span className="text-xs px-2 py-0.5 rounded bg-yellow-500/20 text-yellow-400 flex items-center gap-1">
                  <AlertTriangle className="w-3 h-3" />
                  Affects Position
                </span>
              )}
              {news.tradingAction && (
                <span className={cn(
                  'text-xs px-2 py-0.5 rounded font-semibold',
                  actionColors[news.tradingAction]
                )}>
                  {news.tradingAction}
                </span>
              )}
            </div>

            <h3 className="font-semibold text-white mb-2 line-clamp-2">{news.headline}</h3>

            <div className="flex items-center gap-4 text-xs text-gray-400">
              <span className="flex items-center gap-1">
                <Newspaper className="w-3 h-3" />
                {news.source}
              </span>
              <span className="flex items-center gap-1">
                <Clock className="w-3 h-3" />
                {formatDistanceToNow(news.publishedAt)} ago
              </span>
              <span className={cn(
                'flex items-center gap-1',
                news.sentimentScore > 0 ? 'text-green-400' : news.sentimentScore < 0 ? 'text-red-400' : 'text-gray-400'
              )}>
                {news.sentimentScore > 0 ? <ArrowUp className="w-3 h-3" /> : news.sentimentScore < 0 ? <ArrowDown className="w-3 h-3" /> : null}
                Score: {(news.sentimentScore * 100).toFixed(0)}%
              </span>
            </div>
          </div>
        </div>
      </div>

      {isExpanded && (
        <div className="border-t border-gray-800 p-4 space-y-4">
          {/* Summary */}
          <div>
            <h4 className="text-sm font-semibold text-gray-400 mb-2">Summary</h4>
            <p className="text-sm text-gray-300">{news.summary}</p>
          </div>

          {/* Relevant Symbols & Sectors */}
          <div className="grid grid-cols-2 gap-4">
            {news.relevantSymbols.length > 0 && (
              <div>
                <h4 className="text-sm font-semibold text-gray-400 mb-2">Affected Stocks</h4>
                <div className="flex flex-wrap gap-2">
                  {news.relevantSymbols.map((symbol) => (
                    <span
                      key={symbol}
                      className={cn(
                        'text-xs px-2 py-1 rounded font-mono',
                        positions.includes(symbol)
                          ? 'bg-yellow-500/20 text-yellow-400 ring-1 ring-yellow-500/30'
                          : 'bg-gray-700 text-gray-300'
                      )}
                    >
                      {symbol}
                      {positions.includes(symbol) && ' ★'}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {news.relevantSectors.length > 0 && (
              <div>
                <h4 className="text-sm font-semibold text-gray-400 mb-2">Sectors</h4>
                <div className="flex flex-wrap gap-2">
                  {news.relevantSectors.map((sector) => (
                    <span key={sector} className="text-xs px-2 py-1 rounded bg-purple-500/20 text-purple-400">
                      {sector}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Agent Analysis */}
          {news.agentAnalysis && (
            <div className="bg-[#0d1117] rounded-lg p-4">
              <div className="flex items-center gap-2 mb-2">
                <Bot className="w-5 h-5 text-blue-400" />
                <h4 className="font-semibold text-blue-400">Agent Analysis</h4>
              </div>
              <p className="text-sm text-gray-300">{news.agentAnalysis}</p>
              {news.tradingAction && (
                <div className="mt-3 flex items-center gap-2">
                  <span className="text-sm text-gray-400">Recommended Action:</span>
                  <span className={cn(
                    'px-3 py-1 rounded text-sm font-semibold',
                    actionColors[news.tradingAction]
                  )}>
                    {news.tradingAction}
                  </span>
                </div>
              )}
            </div>
          )}

          {/* Position-Specific Actions */}
          {hasPositionImpact && (
            <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-lg p-4">
              <div className="flex items-center gap-2 mb-2">
                <AlertTriangle className="w-5 h-5 text-yellow-400" />
                <h4 className="font-semibold text-yellow-400">Position Impact</h4>
              </div>
              <p className="text-sm text-gray-300 mb-3">
                This news affects your open position(s) in: {affectedPositions.join(', ')}
              </p>
              <div className="flex gap-2">
                <button className="flex items-center gap-2 px-3 py-2 bg-yellow-500/20 text-yellow-400 rounded-lg text-sm hover:bg-yellow-500/30 transition-colors">
                  <Shield className="w-4 h-4" />
                  Tighten SL
                </button>
                <button className="flex items-center gap-2 px-3 py-2 bg-blue-500/20 text-blue-400 rounded-lg text-sm hover:bg-blue-500/30 transition-colors">
                  <Zap className="w-4 h-4" />
                  Quick Analysis
                </button>
                <button className="flex items-center gap-2 px-3 py-2 bg-gray-700 text-gray-300 rounded-lg text-sm hover:bg-gray-600 transition-colors">
                  <ExternalLink className="w-4 h-4" />
                  Full Article
                </button>
              </div>
            </div>
          )}

          {/* Timestamps */}
          <div className="flex justify-between text-xs text-gray-500">
            <span>Published: {format(news.publishedAt, 'dd MMM yyyy HH:mm')}</span>
            <span>Processed: {format(news.processedAt, 'HH:mm:ss')} ({formatDistanceToNow(news.processedAt)} ago)</span>
          </div>
        </div>
      )}
    </div>
  );
}

export function NewsSentiment() {
  const { news, positions, newsFilter, updateNewsFilter } = useTradingStore();
  const [showFilters, setShowFilters] = useState(false);

  const positionSymbols = positions.map((p) => p.tradingSymbol);

  // Filter news
  let filteredNews = news;
  if (newsFilter.categories.length > 0) {
    filteredNews = filteredNews.filter((n) => newsFilter.categories.includes(n.category));
  }
  if (newsFilter.minImpact !== 'LOW') {
    const impactOrder = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'];
    const minIdx = impactOrder.indexOf(newsFilter.minImpact);
    filteredNews = filteredNews.filter((n) => impactOrder.indexOf(n.impactLevel) >= minIdx);
  }
  if (newsFilter.sentimentFilter !== 'ALL') {
    filteredNews = filteredNews.filter((n) => n.sentiment === newsFilter.sentimentFilter);
  }

  // Sort by impact and recency
  filteredNews.sort((a, b) => {
    // Prioritize news affecting positions
    const aAffects = a.relevantSymbols.some((s) => positionSymbols.includes(s));
    const bAffects = b.relevantSymbols.some((s) => positionSymbols.includes(s));
    if (aAffects && !bAffects) return -1;
    if (!aAffects && bAffects) return 1;

    // Then by impact
    const impactOrder = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];
    const impactDiff = impactOrder.indexOf(a.impactLevel) - impactOrder.indexOf(b.impactLevel);
    if (impactDiff !== 0) return impactDiff;

    // Then by recency
    return b.publishedAt.getTime() - a.publishedAt.getTime();
  });

  // Aggregate sentiment
  const sentimentCounts = {
    positive: news.filter((n) => n.sentiment === 'POSITIVE').length,
    negative: news.filter((n) => n.sentiment === 'NEGATIVE').length,
    neutral: news.filter((n) => n.sentiment === 'NEUTRAL').length,
  };
  const avgSentiment = news.reduce((acc, n) => acc + n.sentimentScore, 0) / news.length;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">News & Sentiment</h2>
          <p className="text-gray-400">Real-time news analysis with trading recommendations</p>
        </div>
        <div className="flex items-center gap-4">
          <button
            onClick={() => setShowFilters(!showFilters)}
            className={cn(
              'flex items-center gap-2 px-4 py-2 rounded-lg transition-colors',
              showFilters
                ? 'bg-blue-500/20 text-blue-400'
                : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
            )}
          >
            <Filter className="w-4 h-4" />
            Filters
          </button>
          <button className="flex items-center gap-2 px-4 py-2 bg-blue-500/20 text-blue-400 rounded-lg hover:bg-blue-500/30 transition-colors">
            <RefreshCw className="w-4 h-4" />
            Refresh
          </button>
        </div>
      </div>

      {/* Sentiment Overview */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className={cn(
              'w-12 h-12 rounded-lg flex items-center justify-center',
              avgSentiment > 0.2 ? 'bg-green-500/20' : avgSentiment < -0.2 ? 'bg-red-500/20' : 'bg-gray-500/20'
            )}>
              <Globe className={cn(
                'w-6 h-6',
                avgSentiment > 0.2 ? 'text-green-400' : avgSentiment < -0.2 ? 'text-red-400' : 'text-gray-400'
              )} />
            </div>
            <div>
              <div className="text-xs text-gray-400">Market Sentiment</div>
              <div className={cn(
                'text-xl font-bold',
                avgSentiment > 0.2 ? 'text-green-400' : avgSentiment < -0.2 ? 'text-red-400' : 'text-gray-400'
              )}>
                {avgSentiment > 0.2 ? 'Bullish' : avgSentiment < -0.2 ? 'Bearish' : 'Neutral'}
              </div>
              <div className="text-xs text-gray-500">{(avgSentiment * 100).toFixed(0)}% score</div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xs text-gray-400">Positive News</div>
              <div className="text-2xl font-bold text-green-400">{sentimentCounts.positive}</div>
            </div>
            <TrendingUp className="w-8 h-8 text-green-400/50" />
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xs text-gray-400">Negative News</div>
              <div className="text-2xl font-bold text-red-400">{sentimentCounts.negative}</div>
            </div>
            <TrendingDown className="w-8 h-8 text-red-400/50" />
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xs text-gray-400">High Impact</div>
              <div className="text-2xl font-bold text-orange-400">
                {news.filter((n) => n.impactLevel === 'HIGH' || n.impactLevel === 'CRITICAL').length}
              </div>
            </div>
            <AlertTriangle className="w-8 h-8 text-orange-400/50" />
          </div>
        </div>
      </div>

      {/* Filters */}
      {showFilters && (
        <div className="bg-[#161b22] rounded-xl border border-gray-800 p-4">
          <div className="grid grid-cols-3 gap-6">
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-2">Categories</label>
              <div className="flex flex-wrap gap-2">
                {['MARKET', 'SECTOR', 'STOCK', 'MACRO', 'GLOBAL', 'REGULATORY'].map((cat) => (
                  <button
                    key={cat}
                    onClick={() => {
                      const current = newsFilter.categories;
                      const updated = current.includes(cat)
                        ? current.filter((c) => c !== cat)
                        : [...current, cat];
                      updateNewsFilter({ categories: updated });
                    }}
                    className={cn(
                      'px-3 py-1 rounded text-xs transition-colors',
                      newsFilter.categories.includes(cat)
                        ? categoryColors[cat]
                        : 'bg-gray-700 text-gray-400 hover:bg-gray-600'
                    )}
                  >
                    {cat}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-400 mb-2">Min Impact</label>
              <div className="flex gap-2">
                {['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].map((impact) => (
                  <button
                    key={impact}
                    onClick={() => updateNewsFilter({ minImpact: impact })}
                    className={cn(
                      'px-3 py-1 rounded text-xs transition-colors',
                      newsFilter.minImpact === impact
                        ? impactColors[impact]
                        : 'bg-gray-700 text-gray-400 hover:bg-gray-600'
                    )}
                  >
                    {impact}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-400 mb-2">Sentiment</label>
              <div className="flex gap-2">
                {['ALL', 'POSITIVE', 'NEGATIVE', 'NEUTRAL'].map((sent) => (
                  <button
                    key={sent}
                    onClick={() => updateNewsFilter({ sentimentFilter: sent })}
                    className={cn(
                      'px-3 py-1 rounded text-xs transition-colors',
                      newsFilter.sentimentFilter === sent
                        ? sent === 'POSITIVE' ? 'bg-green-500/20 text-green-400'
                          : sent === 'NEGATIVE' ? 'bg-red-500/20 text-red-400'
                          : sent === 'NEUTRAL' ? 'bg-gray-500/20 text-gray-300'
                          : 'bg-blue-500/20 text-blue-400'
                        : 'bg-gray-700 text-gray-400 hover:bg-gray-600'
                    )}
                  >
                    {sent}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Position Impact Alert */}
      {news.some((n) =>
        n.relevantSymbols.some((s) => positionSymbols.includes(s)) &&
        (n.impactLevel === 'HIGH' || n.impactLevel === 'CRITICAL')
      ) && (
        <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-xl p-4">
          <div className="flex items-center gap-3">
            <AlertTriangle className="w-6 h-6 text-yellow-400" />
            <div>
              <h3 className="font-semibold text-yellow-400">High-Impact News Affecting Your Positions</h3>
              <p className="text-sm text-gray-300">
                There are {news.filter((n) =>
                  n.relevantSymbols.some((s) => positionSymbols.includes(s)) &&
                  (n.impactLevel === 'HIGH' || n.impactLevel === 'CRITICAL')
                ).length} news items that may impact your open trades. Review them below.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* News Feed */}
      <div className="space-y-4">
        {filteredNews.map((item) => (
          <NewsCard key={item.id} news={item} positions={positionSymbols} />
        ))}

        {filteredNews.length === 0 && (
          <div className="bg-[#161b22] rounded-xl border border-gray-800 p-12 text-center">
            <Newspaper className="w-12 h-12 text-gray-600 mx-auto mb-4" />
            <h3 className="text-lg font-semibold text-gray-400">No News Matching Filters</h3>
            <p className="text-gray-500 mt-2">Adjust your filters or wait for new updates</p>
          </div>
        )}
      </div>
    </div>
  );
}
