import { create } from 'zustand';
import type {
  OrderMonitoringState,
  OrderBreachEvent,
  GTTOrder,
  PartialFillEvent,
  RealTimeNewsItem,
  ScheduledEvent,
  SectorSentiment,
  ReconciliationResult,
  WebSocketHealth,
  ProcessHealth,
  MemoryPressureEvent,
  ExecutionQualityMetrics,
  StrategyAttribution,
  MarketRegime,
  ConfidenceCalibration,
} from '../types/advanced';

// Single-flight + per-request timeout for refreshAdvancedData (same rationale as
// tradingStore: overlapping intervals were flooding the browser with concurrent
// fetches → ERR_INSUFFICIENT_RESOURCES → failed fetches → dashboard flicker).
const _rawFetch = fetch;
let _advRefreshInFlight = false;
// Collector for the refresh cycle currently being issued. refreshAdvancedData()
// fires all of its requests synchronously, so every fetchT() call made during that
// window registers here and the single-flight flag can be released on actual
// completion rather than on a fixed timer.
let _advCycle: Promise<unknown>[] | null = null;
function fetchT(url: string, ms = 8000, init?: RequestInit): Promise<Response> {
  const ctrl = new AbortController();
  const id = setTimeout(() => ctrl.abort(), ms);
  const p = _rawFetch(url, { ...init, signal: ctrl.signal }).finally(() => clearTimeout(id));
  if (_advCycle) _advCycle.push(p);
  return p;
}

// ============ MOCK DATA GENERATORS ============

const generateOrderMonitoringState = (): OrderMonitoringState => ({
  isActive: true,
  checkIntervalMs: 500,
  lastCheckTime: new Date(),
  checksPerMinute: 120,
  avgLatencyMs: 12,
  ordersMonitored: 6,
  breachesDetected: 2,
  autoActionsExecuted: 1,
});

const generateBreachEvents = (): OrderBreachEvent[] => [
  {
    id: 'breach-1',
    positionId: 'pos-1',
    symbol: 'RELIANCE',
    breachType: 'TARGET_APPROACHING',
    detectedAt: new Date(Date.now() - 5 * 60 * 1000),
    ltp: 2945.50,
    expectedPrice: 2950,
    gapPercent: 0.15,
    orderType: 'LIMIT',
    orderStatus: 'OPEN',
    autoAction: 'NONE',
  },
  {
    id: 'breach-2',
    positionId: 'pos-2',
    symbol: 'HDFCBANK',
    breachType: 'SL_APPROACHING',
    detectedAt: new Date(Date.now() - 2 * 60 * 1000),
    ltp: 1645.20,
    expectedPrice: 1640,
    gapPercent: 0.32,
    orderType: 'SL',
    orderStatus: 'TRIGGER_PENDING',
    autoAction: 'NONE',
  },
  {
    id: 'breach-3',
    positionId: 'pos-old',
    symbol: 'TATASTEEL',
    breachType: 'TARGET_BREACH',
    detectedAt: new Date(Date.now() - 30 * 60 * 1000),
    ltp: 165.80,
    expectedPrice: 162,
    gapPercent: 2.35,
    orderType: 'LIMIT',
    orderStatus: 'OPEN',
    autoAction: 'MARKET_EXIT',
    actionResult: 'SUCCESS',
    actionOrderId: 'ord-auto-001',
    resolvedAt: new Date(Date.now() - 29 * 60 * 1000),
  },
];

const generateGTTOrders = (): GTTOrder[] => [
  {
    id: 'gtt-1',
    positionId: 'pos-1',
    symbol: 'RELIANCE',
    triggerType: 'OCO',
    status: 'ACTIVE',
    slTrigger: 2820,
    slLimit: 2815,
    targetTrigger: 2950,
    targetLimit: 2945,
    quantity: 50,
    createdAt: new Date(Date.now() - 2 * 60 * 60 * 1000),
    expiresAt: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000),
    lastCheckedAt: new Date(),
  },
  {
    id: 'gtt-2',
    positionId: 'pos-2',
    symbol: 'HDFCBANK',
    triggerType: 'SINGLE',
    status: 'ACTIVE',
    slTrigger: 1640,
    slLimit: 1635,
    quantity: 30,
    createdAt: new Date(Date.now() - 45 * 60 * 1000),
    expiresAt: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000),
    lastCheckedAt: new Date(),
  },
  {
    id: 'gtt-3',
    positionId: 'pos-3',
    symbol: 'INFY',
    triggerType: 'OCO',
    status: 'ACTIVE',
    slTrigger: 1470,
    slLimit: 1465,
    targetTrigger: 1550,
    targetLimit: 1545,
    quantity: 100,
    createdAt: new Date(Date.now() - 5 * 60 * 60 * 1000),
    expiresAt: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000),
    lastCheckedAt: new Date(),
  },
];

const generatePartialFills = (): PartialFillEvent[] => [
  {
    id: 'pf-1',
    orderId: 'ord-pf-001',
    symbol: 'BAJFINANCE',
    totalQuantity: 20,
    filledQuantity: 12,
    pendingQuantity: 8,
    fillPercent: 60,
    elapsedMs: 15000,
    timeoutMs: 30000,
    status: 'WAITING',
  },
];

const generateRealTimeNews = (): RealTimeNewsItem[] => [
  {
    id: 'rt-news-1',
    headline: '🔴 BREAKING: RBI surprises with 25bps rate cut, first in 2 years',
    summary: 'The Reserve Bank of India has unexpectedly cut the repo rate by 25 basis points to 6.25%, citing easing inflation and need to support growth.',
    source: 'Reuters',
    category: 'BREAKING',
    symbols: ['HDFCBANK', 'ICICIBANK', 'SBIN', 'KOTAKBANK', 'AXISBANK', 'BAJFINANCE'],
    sectors: ['Banking', 'NBFC', 'Real Estate', 'Auto'],
    sentiment: {
      score: 0.85,
      label: 'VERY_POSITIVE',
      confidence: 0.92,
    },
    impact: {
      level: 'CRITICAL',
      priceImpactEstimate: 2.5,
      duration: 'SHORT_TERM',
    },
    agentAction: {
      recommendation: 'HOLD',
      reasoning: 'Positive for banking sector. HDFCBANK position will benefit. Rate-sensitive stocks should rally. Hold current positions and consider adding on dips.',
      confidence: 0.88,
      affectedPositions: ['pos-2'],
      suggestedActions: [
        { positionId: 'pos-2', action: 'Tighten trailing stop to 1.5%', urgency: 'MEDIUM' },
      ],
    },
    timestamps: {
      published: new Date(Date.now() - 5 * 60 * 1000),
      fetched: new Date(Date.now() - 4 * 60 * 1000),
      analyzed: new Date(Date.now() - 3 * 60 * 1000),
    },
  },
  {
    id: 'rt-news-2',
    headline: 'Reliance announces ₹75,000 Cr investment in green energy over 3 years',
    summary: 'Reliance Industries commits massive capex for renewable energy, targeting 100GW capacity by 2030.',
    source: 'Economic Times',
    category: 'STOCK',
    symbols: ['RELIANCE'],
    sectors: ['Energy', 'Green Energy'],
    sentiment: {
      score: 0.72,
      label: 'POSITIVE',
      confidence: 0.85,
    },
    impact: {
      level: 'MEDIUM',
      priceImpactEstimate: 1.2,
      duration: 'MEDIUM_TERM',
    },
    agentAction: {
      recommendation: 'HOLD',
      reasoning: 'Long-term positive catalyst. Current position aligned with thesis. Consider trailing stop to protect gains.',
      confidence: 0.82,
      affectedPositions: ['pos-1'],
      suggestedActions: [
        { positionId: 'pos-1', action: 'Move SL to breakeven', urgency: 'LOW' },
      ],
    },
    timestamps: {
      published: new Date(Date.now() - 45 * 60 * 1000),
      fetched: new Date(Date.now() - 44 * 60 * 1000),
      analyzed: new Date(Date.now() - 43 * 60 * 1000),
    },
  },
  {
    id: 'rt-news-3',
    headline: '⚠️ US imposes new tariffs on tech imports, IT stocks under pressure',
    summary: 'New 15% tariffs on technology services imports could impact Indian IT sector revenue by 3-5% according to analysts.',
    source: 'Bloomberg',
    category: 'GLOBAL',
    symbols: ['INFY', 'TCS', 'WIPRO', 'HCLTECH', 'TECHM', 'LTIM'],
    sectors: ['IT Services'],
    sentiment: {
      score: -0.65,
      label: 'NEGATIVE',
      confidence: 0.78,
    },
    impact: {
      level: 'HIGH',
      priceImpactEstimate: -3.0,
      duration: 'SHORT_TERM',
    },
    agentAction: {
      recommendation: 'EXIT',
      reasoning: 'INFY position at risk. Sector-wide selloff expected. Recommend immediate exit to protect capital. Can re-enter after dust settles.',
      confidence: 0.75,
      affectedPositions: ['pos-3'],
      suggestedActions: [
        { positionId: 'pos-3', action: 'EXIT - Market order recommended', urgency: 'IMMEDIATE' },
      ],
    },
    timestamps: {
      published: new Date(Date.now() - 15 * 60 * 1000),
      fetched: new Date(Date.now() - 14 * 60 * 1000),
      analyzed: new Date(Date.now() - 13 * 60 * 1000),
    },
  },
  {
    id: 'rt-news-4',
    headline: 'Crude oil spikes 5% on Middle East tensions',
    summary: 'Brent crude jumps to $92/barrel as geopolitical tensions escalate. OMCs and aviation stocks under pressure.',
    source: 'CNBC',
    category: 'GLOBAL',
    symbols: ['ONGC', 'BPCL', 'IOC', 'HINDPETRO', 'RELIANCE'],
    sectors: ['Oil & Gas', 'Aviation', 'Paints'],
    sentiment: {
      score: -0.45,
      label: 'NEGATIVE',
      confidence: 0.82,
    },
    impact: {
      level: 'MEDIUM',
      priceImpactEstimate: -1.5,
      duration: 'SHORT_TERM',
    },
    agentAction: {
      recommendation: 'WATCH',
      reasoning: 'Mixed impact on RELIANCE (upstream positive, downstream negative). Monitor closely. No immediate action required.',
      confidence: 0.70,
      affectedPositions: ['pos-1'],
      suggestedActions: [],
    },
    timestamps: {
      published: new Date(Date.now() - 2 * 60 * 60 * 1000),
      fetched: new Date(Date.now() - 118 * 60 * 1000),
      analyzed: new Date(Date.now() - 117 * 60 * 1000),
    },
  },
];

const generateScheduledEvents = (): ScheduledEvent[] => [
  {
    id: 'event-1',
    title: 'INFY Q3 Results',
    type: 'EARNINGS',
    date: new Date(Date.now() + 2 * 24 * 60 * 60 * 1000),
    time: '16:00',
    symbols: ['INFY'],
    sectors: ['IT Services'],
    expectedImpact: 'HIGH',
    blackoutStart: new Date(Date.now() + 1.5 * 24 * 60 * 60 * 1000),
    blackoutEnd: new Date(Date.now() + 2.5 * 24 * 60 * 60 * 1000),
    positionSizeReduction: 50,
    notes: 'Consensus EPS: ₹15.2. Watch for guidance on deal pipeline.',
  },
  {
    id: 'event-2',
    title: 'RBI MPC Meeting',
    type: 'RBI_POLICY',
    date: new Date(Date.now() + 5 * 24 * 60 * 60 * 1000),
    time: '10:00',
    symbols: [],
    sectors: ['Banking', 'NBFC', 'Real Estate'],
    expectedImpact: 'CRITICAL',
    blackoutStart: new Date(Date.now() + 4.5 * 24 * 60 * 60 * 1000),
    blackoutEnd: new Date(Date.now() + 5.5 * 24 * 60 * 60 * 1000),
    positionSizeReduction: 30,
  },
  {
    id: 'event-3',
    title: 'US Fed Meeting',
    type: 'FED_MEETING',
    date: new Date(Date.now() + 7 * 24 * 60 * 60 * 1000),
    time: '00:30',
    symbols: [],
    sectors: [],
    expectedImpact: 'HIGH',
    notes: 'Market expects 25bps cut. Dot plot will be key.',
  },
  {
    id: 'event-4',
    title: 'RELIANCE AGM',
    type: 'AGM',
    date: new Date(Date.now() + 14 * 24 * 60 * 60 * 1000),
    time: '14:00',
    symbols: ['RELIANCE'],
    sectors: ['Conglomerate'],
    expectedImpact: 'MEDIUM',
    notes: 'Jio Financial demerger updates expected.',
  },
];

const generateSectorSentiments = (): SectorSentiment[] => [
  {
    sector: 'Banking',
    currentScore: 0.72,
    score24h: 0.65,
    score7d: 0.58,
    trend: 'IMPROVING',
    newsCount24h: 23,
    topStories: ['RBI rate cut surprise', 'Credit growth at 16%', 'NPA concerns ease'],
    recommendation: 'OVERWEIGHT',
  },
  {
    sector: 'IT Services',
    currentScore: -0.35,
    score24h: -0.15,
    score7d: 0.12,
    trend: 'DECLINING',
    newsCount24h: 18,
    topStories: ['US tariff concerns', 'Deal pipeline weak', 'Attrition stabilizing'],
    recommendation: 'UNDERWEIGHT',
  },
  {
    sector: 'Auto',
    currentScore: 0.45,
    score24h: 0.42,
    score7d: 0.38,
    trend: 'STABLE',
    newsCount24h: 12,
    topStories: ['EV adoption rising', 'Rural demand recovery', 'Input costs stable'],
    recommendation: 'NEUTRAL',
  },
  {
    sector: 'Pharma',
    currentScore: 0.28,
    score24h: 0.30,
    score7d: 0.25,
    trend: 'STABLE',
    newsCount24h: 8,
    topStories: ['US FDA inspections clear', 'Domestic growth strong'],
    recommendation: 'NEUTRAL',
  },
  {
    sector: 'Real Estate',
    currentScore: 0.68,
    score24h: 0.55,
    score7d: 0.45,
    trend: 'IMPROVING',
    newsCount24h: 15,
    topStories: ['Rate cut boost', 'Housing demand strong', 'Inventory clearing'],
    recommendation: 'OVERWEIGHT',
  },
  {
    sector: 'Metals',
    currentScore: -0.22,
    score24h: -0.18,
    score7d: -0.10,
    trend: 'DECLINING',
    newsCount24h: 10,
    topStories: ['China demand weak', 'Global slowdown fears'],
    recommendation: 'UNDERWEIGHT',
  },
];

const generateReconciliationResults = (): ReconciliationResult[] => [
  {
    id: 'recon-1',
    runAt: new Date(),
    duration: 234,
    dbPositions: 3,
    brokerPositions: 3,
    matched: 3,
    mismatches: [],
    gttsChecked: 3,
    gttsRearmed: 0,
    incidentsRaised: 0,
  },
  {
    id: 'recon-2',
    runAt: new Date(Date.now() - 30 * 1000),
    duration: 287,
    dbPositions: 3,
    brokerPositions: 3,
    matched: 3,
    mismatches: [],
    gttsChecked: 3,
    gttsRearmed: 0,
    incidentsRaised: 0,
  },
  {
    id: 'recon-3',
    runAt: new Date(Date.now() - 60 * 1000),
    duration: 312,
    dbPositions: 4,
    brokerPositions: 3,
    matched: 3,
    mismatches: [
      {
        type: 'DB_ONLY',
        symbol: 'TATASTEEL',
        dbValue: { qty: 100, status: 'open' },
        brokerValue: null,
        action: 'Marked as closed - exit detected',
        resolved: true,
      },
    ],
    gttsChecked: 4,
    gttsRearmed: 1,
    incidentsRaised: 1,
  },
];

const generateWebSocketHealth = (): WebSocketHealth => ({
  status: 'CONNECTED',
  lastTickTime: new Date(),
  ticksPerSecond: 847,
  reconnectCount: 2,
  lastReconnectAt: new Date(Date.now() - 4 * 60 * 60 * 1000),
  subscribedSymbols: 600,
  activeSymbols: 598,
  staleSymbols: ['ZOMATO', 'PAYTM'],
  latencyMs: 3.2,
});

const generateProcessHealth = (): ProcessHealth[] => [
  {
    name: 'Orchestrator',
    pid: 12345,
    status: 'RUNNING',
    cpu: 2.3,
    memory: 85.2,
    memoryLimit: 200,
    uptime: 28800,
    restartCount: 0,
    lastHeartbeat: new Date(),
    errors24h: 0,
    gcPauses: 12,
    gcPauseAvgMs: 15,
  },
  {
    name: 'EquityIngester',
    pid: 12346,
    status: 'RUNNING',
    cpu: 15.7,
    memory: 142.8,
    memoryLimit: 200,
    uptime: 28800,
    restartCount: 1,
    lastRestartAt: new Date(Date.now() - 4 * 60 * 60 * 1000),
    lastHeartbeat: new Date(),
    errors24h: 2,
    gcPauses: 45,
    gcPauseAvgMs: 8,
  },
  {
    name: 'CentralRiskManager',
    pid: 12347,
    status: 'RUNNING',
    cpu: 8.4,
    memory: 67.3,
    memoryLimit: 100,
    uptime: 28800,
    restartCount: 0,
    lastHeartbeat: new Date(),
    errors24h: 0,
    gcPauses: 23,
    gcPauseAvgMs: 12,
  },
  {
    name: 'OrderMonitor',
    pid: 12355,
    status: 'RUNNING',
    cpu: 5.2,
    memory: 45.6,
    memoryLimit: 100,
    uptime: 28800,
    restartCount: 0,
    lastHeartbeat: new Date(),
    errors24h: 0,
    gcPauses: 8,
    gcPauseAvgMs: 5,
  },
  {
    name: 'NewsIngester',
    pid: 12356,
    status: 'RUNNING',
    cpu: 3.1,
    memory: 52.4,
    memoryLimit: 100,
    uptime: 28800,
    restartCount: 0,
    lastHeartbeat: new Date(),
    errors24h: 1,
    gcPauses: 15,
    gcPauseAvgMs: 10,
  },
  {
    name: 'RuntimeSupervisor',
    pid: 12352,
    status: 'RUNNING',
    cpu: 1.2,
    memory: 52.4,
    memoryLimit: 100,
    uptime: 28800,
    restartCount: 0,
    lastHeartbeat: new Date(),
    errors24h: 0,
    gcPauses: 5,
    gcPauseAvgMs: 3,
  },
];

const generateMemoryPressureEvents = (): MemoryPressureEvent[] => [
  {
    id: 'mem-1',
    process: 'EquityIngester',
    timestamp: new Date(Date.now() - 4 * 60 * 60 * 1000),
    rssBeforeMB: 195,
    rssAfterMB: 142,
    action: 'GC_TRIGGERED',
    result: 'SUCCESS',
    freedMB: 53,
  },
  {
    id: 'mem-2',
    process: 'StrategyWorker-2',
    timestamp: new Date(Date.now() - 2 * 60 * 60 * 1000),
    rssBeforeMB: 158,
    rssAfterMB: 89,
    action: 'RESTART_INITIATED',
    result: 'SUCCESS',
    freedMB: 69,
  },
];

const generateExecutionQuality = (): ExecutionQualityMetrics[] => [
  {
    symbol: 'RELIANCE',
    totalTrades: 45,
    avgSlippagePct: 0.08,
    avgFillTimeMs: 120,
    marketImpactPct: 0.02,
    bestTimeOfDay: '09:30-10:30',
    worstTimeOfDay: '14:30-15:00',
    limitFillRate: 0.92,
    slTriggerAccuracy: 0.98,
    recommendation: 'Excellent liquidity. Use limit orders.',
  },
  {
    symbol: 'HDFCBANK',
    totalTrades: 38,
    avgSlippagePct: 0.05,
    avgFillTimeMs: 85,
    marketImpactPct: 0.01,
    bestTimeOfDay: '09:15-10:00',
    worstTimeOfDay: '15:00-15:30',
    limitFillRate: 0.95,
    slTriggerAccuracy: 0.99,
    recommendation: 'Top-tier execution. Ideal for large positions.',
  },
  {
    symbol: 'INFY',
    totalTrades: 32,
    avgSlippagePct: 0.12,
    avgFillTimeMs: 180,
    marketImpactPct: 0.03,
    bestTimeOfDay: '10:00-11:00',
    worstTimeOfDay: '09:15-09:30',
    limitFillRate: 0.88,
    slTriggerAccuracy: 0.96,
    recommendation: 'Avoid first 15 mins. Good otherwise.',
  },
  {
    symbol: 'TATASTEEL',
    totalTrades: 28,
    avgSlippagePct: 0.22,
    avgFillTimeMs: 350,
    marketImpactPct: 0.08,
    bestTimeOfDay: '11:00-12:00',
    worstTimeOfDay: '09:15-09:45',
    limitFillRate: 0.78,
    slTriggerAccuracy: 0.91,
    recommendation: 'Higher slippage. Consider SL-M for stops.',
  },
];

const generateStrategyAttributions = (): StrategyAttribution[] => [
  {
    strategy: 'minervini_vcp',
    totalTrades: 24,
    winRate: 0.67,
    avgRR: 2.8,
    profitFactor: 2.4,
    maxDrawdown: 8.5,
    sharpeRatio: 1.85,
    attributions: [
      { factor: 'ENTRY_TIMING', contribution: 35, improvement: 'Wait for volume confirmation' },
      { factor: 'SL_PLACEMENT', contribution: 25, improvement: 'Use ATR-based stops' },
      { factor: 'TARGET_PLACEMENT', contribution: 20, improvement: 'Trail after 1R profit' },
      { factor: 'MARKET_REGIME', contribution: 15, improvement: 'Avoid ranging markets' },
      { factor: 'POSITION_SIZE', contribution: 5, improvement: 'Size is appropriate' },
    ],
    recommendations: [
      'Entry timing contributes most to alpha. Focus on volume confirmation.',
      'Consider ATR-based stops instead of fixed percentage.',
      'Trailing after 1R would improve avg win by 15%.',
    ],
  },
  {
    strategy: 'ema_pullback_swing',
    totalTrades: 18,
    winRate: 0.56,
    avgRR: 1.9,
    profitFactor: 1.6,
    maxDrawdown: 12.3,
    sharpeRatio: 1.2,
    attributions: [
      { factor: 'ENTRY_TIMING', contribution: 20, improvement: 'Enter on EMA touch, not cross' },
      { factor: 'SL_PLACEMENT', contribution: 30, improvement: 'SL too tight, widen by 0.5 ATR' },
      { factor: 'TARGET_PLACEMENT', contribution: 25, improvement: 'Targets realistic' },
      { factor: 'MARKET_REGIME', contribution: 20, improvement: 'Only trade in uptrends' },
      { factor: 'POSITION_SIZE', contribution: 5, improvement: 'Consider reducing size' },
    ],
    recommendations: [
      'Stop losses getting hit prematurely. Widen by 0.5 ATR.',
      'Filter for trending regimes only (ADX > 25).',
      'Entry on EMA touch rather than cross improves timing.',
    ],
  },
];

const generateMarketRegime = (): MarketRegime => ({
  current: 'TRENDING_UP',
  confidence: 0.78,
  since: new Date(Date.now() - 5 * 24 * 60 * 60 * 1000),
  indicators: {
    adx: 32,
    atrPercent: 1.2,
    breadth: 0.68,
    vix: 13.5,
    trend50: 'UP',
    trend200: 'UP',
  },
  suitableStrategies: ['minervini_vcp', 'weinstein_stage2', 'oneil_canslim_breakout'],
  avoidStrategies: ['orb_intraday', 'mean_reversion'],
  positionSizeMultiplier: 1.0,
  historicalAccuracy: 0.73,
});

const generateConfidenceCalibrations = (): ConfidenceCalibration[] => [
  {
    strategy: 'minervini_vcp',
    llmProvider: 'gemini-2.0-flash',
    totalPredictions: 156,
    calibrationData: [
      { predictedConfidence: 0.55, actualWinRate: 0.52, sampleSize: 23, isCalibrated: true },
      { predictedConfidence: 0.65, actualWinRate: 0.61, sampleSize: 45, isCalibrated: true },
      { predictedConfidence: 0.75, actualWinRate: 0.68, sampleSize: 52, isCalibrated: true },
      { predictedConfidence: 0.85, actualWinRate: 0.74, sampleSize: 28, isCalibrated: false },
      { predictedConfidence: 0.95, actualWinRate: 0.79, sampleSize: 8, isCalibrated: false },
    ],
    overallCalibration: 'OVERCONFIDENT',
    suggestedThresholdAdjustment: 0.05,
    lastUpdated: new Date(Date.now() - 24 * 60 * 60 * 1000),
  },
  {
    strategy: 'ema_pullback_swing',
    llmProvider: 'gemini-2.0-flash',
    totalPredictions: 98,
    calibrationData: [
      { predictedConfidence: 0.55, actualWinRate: 0.48, sampleSize: 18, isCalibrated: false },
      { predictedConfidence: 0.65, actualWinRate: 0.58, sampleSize: 32, isCalibrated: true },
      { predictedConfidence: 0.75, actualWinRate: 0.72, sampleSize: 35, isCalibrated: true },
      { predictedConfidence: 0.85, actualWinRate: 0.81, sampleSize: 13, isCalibrated: true },
    ],
    overallCalibration: 'WELL_CALIBRATED',
    suggestedThresholdAdjustment: 0,
    lastUpdated: new Date(Date.now() - 24 * 60 * 60 * 1000),
  },
];

// ============ STORE ============

export interface OpenPositionRow {
  symbol: string;
  direction: 'LONG' | 'SHORT';
  quantity: number;
  product: string;
  entryPrice: number;
  ltp: number;
  pnl: number;
  external: boolean;
  paper: boolean;
  entryTime: string;
  strategy: string;
  stopLoss: number | null;
  target: number | null;
}

export interface SwingPositionRow {
  tradeId: string;
  symbol: string;
  strategyName: string;
  direction: 'LONG' | 'SHORT';
  quantity: number;
  entryPrice: number;
  exitPrice: number | null;
  stopLoss: number | null;
  target: number | null;
  entryTime: string;
  exitTime: string;
  paper: boolean;
  ltp: number | null;
  dayChangePct: number | null;
  pnl: number | null;
  partialPnlBooked: number;
}

export interface ClosedPositionRow {
  symbol: string;
  direction: 'LONG' | 'SHORT';
  quantity: number;
  entryPrice: number;
  exitPrice: number;
  pnl: number;
  strategy: string;
  entryAt: string;
  closedAt: string;
  paper: boolean;
}

interface AdvancedStore {
  // Order Monitoring
  orderMonitoringState: OrderMonitoringState;
  breachEvents: OrderBreachEvent[];
  gttOrders: GTTOrder[];
  partialFills: PartialFillEvent[];

  // News & Sentiment
  realTimeNews: RealTimeNewsItem[];
  scheduledEvents: ScheduledEvent[];
  sectorSentiments: SectorSentiment[];

  // Self-Healing
  reconciliationResults: ReconciliationResult[];
  webSocketHealth: WebSocketHealth;
  processHealth: ProcessHealth[];
  memoryPressureEvents: MemoryPressureEvent[];

  // Positions (Order Monitor+ Open/Closed tabs)
  openPositions: OpenPositionRow[];
  closedPositions: ClosedPositionRow[];
  swingPositions: SwingPositionRow[];

  // Intelligence
  executionQuality: ExecutionQualityMetrics[];
  strategyAttributions: StrategyAttribution[];
  marketRegime: MarketRegime;
  confidenceCalibrations: ConfidenceCalibration[];

  // Actions
  refreshAdvancedData: () => void;
  resolveBreachEvent: (positionId: string, action: 'MARKET_EXIT') => Promise<void>;
  dismissNews: (id: string) => void;
  refreshNews: () => Promise<void>;
  refreshSectorSentiments: () => Promise<void>;
  refreshUpcomingEvents: () => Promise<void>;
  acknowledgeEvent: (id: string) => void;
}

export const useAdvancedStore = create<AdvancedStore>((set) => ({
  // Initial state — empty/zero; refreshAdvancedData() populates from real APIs
  orderMonitoringState: {
    isActive: false,
    checkIntervalMs: 2000,
    lastCheckTime: new Date(),
    checksPerMinute: 0,
    avgLatencyMs: 0,
    ordersMonitored: 0,
    breachesDetected: 0,
    autoActionsExecuted: 0,
  },
  breachEvents: [],
  gttOrders: [],
  partialFills: [],
  realTimeNews: [],
  scheduledEvents: [],
  sectorSentiments: [],
  reconciliationResults: [],
  webSocketHealth: {
    status: 'DISCONNECTED' as const,
    lastTickTime: new Date(),
    ticksPerSecond: 0,
    reconnectCount: 0,
    lastReconnectAt: new Date(),
    subscribedSymbols: 0,
    activeSymbols: 0,
    staleSymbols: [],
    latencyMs: 0,
  },
  processHealth: [],
  memoryPressureEvents: [],
  openPositions: [],
  closedPositions: [],
  swingPositions: [],
  executionQuality: [],
  strategyAttributions: [],
  marketRegime: {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    current: 'UNKNOWN' as any,
    confidence: 0,
    since: new Date(),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    indicators: { adx: 0, atrPercent: 0, breadth: 0, vix: 0, trend50: 'FLAT' as any, trend200: 'FLAT' as any },
    suitableStrategies: [],
    avoidStrategies: [],
    positionSizeMultiplier: 1.0,
    historicalAccuracy: 0,
  },
  confidenceCalibrations: [],

  // Actions
  refreshAdvancedData: () => {
    if (_advRefreshInFlight) return;
    _advRefreshInFlight = true;
    // Completion-based guard. The previous 2500ms timer released the flag while
    // requests with an 8000ms timeout were still in flight, so a slow cycle still
    // stacked on top of itself — exactly the pile-up the guard existed to prevent.
    // fetchT's abort still bounds each request, so the flag can never wedge.
    const _cycle: Promise<unknown>[] = [];
    _advCycle = _cycle;
    const API = '';

    // GTT orders from broker
    fetchT(`${API}/api/gtt`).then((r) => r.json()).then((data) => {
      if (!Array.isArray(data)) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const mapped = data.map((g: any) => ({
        id: String(g.id ?? g.trigger_id ?? ''),
        positionId: String(g.id ?? ''),
        symbol: g.condition?.tradingsymbol ?? g.symbol ?? '',
        triggerType: (g.type === 'two-leg' ? 'OCO' : 'SINGLE') as 'OCO' | 'SINGLE',
        status: (g.status?.toUpperCase() ?? 'UNKNOWN') as 'ACTIVE' | 'TRIGGERED' | 'EXPIRED' | 'CANCELLED',
        slTrigger: Number(g.condition?.trigger_values?.[0] ?? 0),
        slLimit: Number(g.orders?.[0]?.price ?? 0),
        targetTrigger: Number(g.condition?.trigger_values?.[1] ?? 0),
        targetLimit: Number(g.orders?.[1]?.price ?? 0),
        quantity: Number(g.orders?.[0]?.quantity ?? 0),
        createdAt: g.created_at ? new Date(g.created_at) : new Date(),
        expiresAt: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000),
        lastCheckedAt: new Date(),
      }));
      set({ gttOrders: mapped });
    }).catch(() => {});

    // Market regime
    fetchT(`${API}/api/regime`).then((r) => r.json()).then((data) => {
      if (!data?.regime) return;
      // DB regime vocabulary (BULL/BEAR/SIDEWAYS/...) → dashboard enum
      const _regimeMap: Record<string, string> = {
        BULL: 'TRENDING_UP', STRONG_BULL: 'TRENDING_UP',
        BEAR: 'TRENDING_DOWN', STRONG_BEAR: 'TRENDING_DOWN',
        SIDEWAYS: 'RANGING', NEUTRAL: 'RANGING', RANGE: 'RANGING',
        VOLATILE: 'VOLATILE', CRASH: 'CRASH',
      };
      const _raw = String(data.regime).toUpperCase();
      set({
        marketRegime: {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          current: (_regimeMap[_raw] ?? _raw) as any,
          confidence: Number(data.confidence ?? 0),
          since: data.since ? new Date(data.since) : new Date(),
          indicators: {
            adx: Number(data.adx ?? 0),
            atrPercent: Number(data.atr_pct ?? 0),
            breadth: Number(data.breadth ?? 0),
            vix: Number(data.vix ?? 0),
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            trend50: (data.ma50_trend ?? 'FLAT') as any,
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            trend200: (data.ma200_trend ?? 'FLAT') as any,
          },
          suitableStrategies: data.suitable_strategies ?? [],
          avoidStrategies: data.avoid_strategies ?? [],
          positionSizeMultiplier: Number(data.position_size_multiplier ?? 1.0),
          historicalAccuracy: Number(data.historical_accuracy ?? 0),
        },
      });
    }).catch(() => {});

    // Agent status → drives Monitor Active/Stopped badge
    fetchT(`${API}/api/agent_status`).then((r) => r.json()).then((data) => {
      set({
        orderMonitoringState: {
          ...useAdvancedStore.getState().orderMonitoringState,
          isActive: data?.active === true,
          lastCheckTime: new Date(),
        },
      });
    }).catch(() => {});

    // Live monitor stats (order_monitor_stats, DB truth) → top stat tiles
    fetchT(`${API}/api/monitor_stats`).then((r) => r.json()).then((d) => {
      if (!d) return;
      set({
        orderMonitoringState: {
          ...useAdvancedStore.getState().orderMonitoringState,
          checkIntervalMs: Number(d.checkIntervalMs ?? 2000),
          checksPerMinute: Number(d.checksPerMinute ?? 0),
          avgLatencyMs: Number(d.avgLatencyMs ?? 0),
          ordersMonitored: Number(d.ordersMonitored ?? 0),
          breachesDetected: Number(d.breachesDetected ?? 0),
          autoActionsExecuted: Number(d.autoActionsExecuted ?? 0),
        },
      });
    }).catch(() => {});

    // Breach events (order_monitor_breaches, DB truth) → Active + Resolved alerts
    fetchT(`${API}/api/breaches`).then((r) => r.json()).then((data) => {
      if (!Array.isArray(data)) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const mapped = data.map((b: any) => ({
        id: String(b.id),
        positionId: String(b.positionId ?? ''),
        symbol: b.symbol,
        breachType: b.breachType,
        detectedAt: b.detectedAt ? new Date(String(b.detectedAt).replace(' ', 'T')) : new Date(),
        ltp: Number(b.ltp ?? 0),
        expectedPrice: Number(b.expectedPrice ?? 0),
        gapPercent: Number(b.gapPercent ?? 0),
        orderType: b.orderType ?? 'LIMIT',
        orderStatus: b.orderStatus ?? 'OPEN',
        autoAction: b.autoAction ?? 'NONE',
        actionResult: b.actionResult ?? undefined,
        resolvedAt: b.resolvedAt ? new Date(String(b.resolvedAt).replace(' ', 'T')) : undefined,
      }));
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      set({ breachEvents: mapped as any });
    }).catch(() => {});

    // Partial fills (trades in PARTIAL state, DB truth)
    fetchT(`${API}/api/partial_fills`).then((r) => r.json()).then((data) => {
      if (!Array.isArray(data)) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      set({ partialFills: data as any });
    }).catch(() => {});

    // Open positions (Kite live truth) → Order Monitor+ Open tab
    fetchT(`${API}/api/positions`).then((r) => r.json()).then((data) => {
      const net = Array.isArray(data?.net) ? data.net : [];
      // A CNC row with quantity < 0 and overnight_quantity 0 is NOT an open
      // short — CNC/cash accounts can't hold a genuine open short. It's Kite's
      // day-position bookkeeping for shares sold TODAY out of existing
      // holdings (no matching same-day buy), which only clears from
      // positions() after T+1 settlement. From the trader's perspective that
      // trade is already exited; showing it as "open" was misleading.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const isSettledCncSale = (p: any) =>
        (p.product ?? '') === 'CNC' &&
        Number(p.quantity ?? 0) < 0 &&
        Number(p.overnight_quantity ?? 0) === 0;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const mapped = net
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        .filter((p: any) => Number(p.quantity ?? 0) !== 0 && !isSettledCncSale(p))
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        .map((p: any) => ({
        symbol: p.tradingsymbol ?? '',
        direction: (Number(p.quantity ?? 0) >= 0 ? 'LONG' : 'SHORT') as 'LONG' | 'SHORT',
        quantity: Math.abs(Number(p.quantity ?? 0)),
        product: p.product ?? '',
        entryPrice: Number(p.average_price ?? 0),
        ltp: Number(p.last_price ?? 0),
        pnl: Number(p.pnl ?? 0),
        external: p.external === true,
        paper: p.paper === true,
        entryTime: p.entry_time ?? '',
        strategy: p.strategy ?? '',
        stopLoss: p.current_sl != null ? Number(p.current_sl) : null,
        target: p.current_target != null ? Number(p.current_target) : null,
      }));
      set({ openPositions: mapped });
    }).catch(() => {});

    // Swing (CNC) positions — any entry date, not just today (see /api/positions'
    // intraday-only same-day filter, which excludes multi-day swing holds).
    fetchT(`${API}/api/swing_positions`).then((r) => r.json()).then((data) => {
      if (!Array.isArray(data)) return;
      const mappedSwing: SwingPositionRow[] = data.map((p) => ({
        tradeId: p.tradeId ?? '',
        symbol: p.symbol ?? '',
        strategyName: p.strategyName ?? '',
        direction: (p.direction === 'SHORT' ? 'SHORT' : 'LONG') as 'LONG' | 'SHORT',
        quantity: Number(p.quantity ?? 0),
        entryPrice: Number(p.entryPrice ?? 0),
        exitPrice: p.exitPrice != null ? Number(p.exitPrice) : null,
        stopLoss: p.stopLoss != null ? Number(p.stopLoss) : null,
        target: p.target != null ? Number(p.target) : null,
        entryTime: p.entryTime ?? '',
        exitTime: p.exitTime ?? '',
        paper: p.paper === true,
        ltp: p.ltp != null ? Number(p.ltp) : null,
        dayChangePct: p.dayChangePct != null ? Number(p.dayChangePct) : null,
        pnl: p.pnl != null ? Number(p.pnl) : null,
        partialPnlBooked: Number(p.partialPnlBooked ?? 0),
      }));
      set({ swingPositions: mappedSwing });
    }).catch(() => {});

    // Closed positions (trades, DB truth) → Order Monitor+ Closed tab
    fetchT(`${API}/api/closed_positions`).then((r) => r.json()).then((data) => {
      if (!Array.isArray(data)) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const mapped = data.map((c: any) => ({
        symbol: c.symbol ?? '',
        direction: (String(c.direction ?? '').toUpperCase() === 'SHORT' ? 'SHORT' : 'LONG') as 'LONG' | 'SHORT',
        quantity: Number(c.quantity ?? 0),
        entryPrice: Number(c.entryPrice ?? 0),
        exitPrice: Number(c.exitPrice ?? 0),
        pnl: Number(c.pnl ?? 0),
        strategy: c.strategy ?? '',
        entryAt: c.entryAt ?? '',
        closedAt: c.closedAt ?? '',
        paper: c.paper === true,
      }));
      set({ closedPositions: mapped });
    }).catch(() => {});

    // Calibration history — /api/calibration returns { model: [bucket rows] } (the
    // shape the Learning tab also consumes); reshape into ConfidenceCalibration[] here.
    fetchT(`${API}/api/calibration`).then((r) => r.json()).then((data) => {
      if (!data || typeof data !== 'object' || Array.isArray(data)) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const mapped = Object.entries(data).map(([model, rowsRaw]: [string, any]) => {
        const rows = Array.isArray(rowsRaw) ? rowsRaw : [];
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const calibrationData = rows.map((r: any) => {
          const pred = Number(r.predicted_wr ?? 0);
          const act = Number(r.actual_wr ?? 0);
          return {
            predictedConfidence: pred,
            actualWinRate: act,
            sampleSize: Number(r.trade_count ?? 0),
            isCalibrated: Math.abs(pred - act) <= 0.1,
          };
        }).sort((a, b) => a.predictedConfidence - b.predictedConfidence);
        const n = calibrationData.length;
        const avgGap = n
          ? calibrationData.reduce((s, c) => s + (c.predictedConfidence - c.actualWinRate), 0) / n
          : 0;
        const overall = avgGap > 0.05 ? 'OVERCONFIDENT' : avgGap < -0.05 ? 'UNDERCONFIDENT' : 'WELL_CALIBRATED';
        return {
          strategy: model,
          llmProvider: model,
          totalPredictions: calibrationData.reduce((s, c) => s + c.sampleSize, 0),
          calibrationData,
          overallCalibration: overall,
          suggestedThresholdAdjustment: avgGap > 0.05 ? Math.round(avgGap * 100) / 100 : 0,
          lastUpdated: new Date(),
        };
      });
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      if (mapped.length) set({ confidenceCalibrations: mapped as any });
    }).catch(() => {});

    // Attribution → strategyAttributions
    fetchT(`${API}/api/attribution`).then((r) => r.json()).then((data) => {
      if (Array.isArray(data) && data.length > 0) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        set({ strategyAttributions: data as any });
      }
    }).catch(() => {});

    // Per-symbol execution quality (DB truth: trades)
    fetchT(`${API}/api/execution_quality`).then((r) => r.json()).then((data) => {
      if (Array.isArray(data)) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        set({ executionQuality: data as any });
      }
    }).catch(() => {});

    // Process health (engine_heartbeats, DB truth) → Self-Healing process grid
    fetchT(`${API}/api/process_health`).then((r) => r.json()).then((data) => {
      if (!Array.isArray(data)) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const mapped: ProcessHealth[] = data.map((p: any) => ({
        name: p.name,
        pid: Number(p.pid ?? 0),
        status: (p.status ?? 'STOPPED') as ProcessHealth['status'],
        cpu: Number(p.cpu ?? 0),
        memory: Number(p.memory ?? 0),
        memoryLimit: Number(p.memoryLimit ?? 0),
        uptime: Number(p.uptime ?? 0),
        restartCount: Number(p.restartCount ?? 0),
        lastHeartbeat: p.lastHeartbeat ? new Date(p.lastHeartbeat) : new Date(),
        errors24h: Number(p.errors24h ?? 0),
        gcPauses: Number(p.gcPauses ?? 0),
        gcPauseAvgMs: Number(p.gcPauseAvgMs ?? 0),
      }));
      set({ processHealth: mapped });
    }).catch(() => {});

    // WebSocket health (ws_health, DB truth) → Self-Healing WS monitor
    fetchT(`${API}/api/ws_health`).then((r) => r.json()).then((d) => {
      if (!d || !d.status) return;
      set({
        webSocketHealth: {
          status: d.status as WebSocketHealth['status'],
          lastTickTime: new Date(),
          ticksPerSecond: Number(d.ticks_per_second ?? 0),
          reconnectCount: 0,
          subscribedSymbols: Number(d.subscribed ?? 0),
          activeSymbols: Number(d.active ?? 0),
          staleSymbols: Array.isArray(d.stale) ? d.stale : [],
          latencyMs: 0,
        },
      });
    }).catch(() => {});

    // Position reconciliation (reconciliation_log, DB truth) → Self-Healing recon panel
    fetchT(`${API}/api/reconciliation`).then((r) => r.json()).then((data) => {
      if (!Array.isArray(data)) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const mapped: ReconciliationResult[] = data.map((x: any) => ({
        id: String(x.id),
        runAt: x.runAt ? new Date(x.runAt) : new Date(),
        duration: Number(x.duration ?? 0),
        dbPositions: Number(x.dbPositions ?? 0),
        brokerPositions: Number(x.brokerPositions ?? 0),
        matched: Number(x.matched ?? 0),
        mismatches: Array.isArray(x.mismatches) ? x.mismatches : [],
        gttsChecked: Number(x.gttsChecked ?? 0),
        gttsRearmed: Number(x.gttsRearmed ?? 0),
        incidentsRaised: Number(x.incidentsRaised ?? 0),
      }));
      set({ reconciliationResults: mapped });
    }).catch(() => {});

    // Every request for this cycle has now been issued, so stop collecting and
    // release the single-flight flag only once they have all settled.
    _advCycle = null;
    Promise.allSettled(_cycle).finally(() => { _advRefreshInFlight = false; });
  },

  resolveBreachEvent: async (positionId, action) => {
    // Root cause fixed 2026-08-17: this used to only flip local state
    // (fake 'SUCCESS') with no backend call at all — the position was never
    // actually touched, so the very next breach poll showed the same live
    // breach again. Now queues a real force-exit; the breach naturally drops
    // off the active list once order_monitor.cycle() confirms the position
    // is closed (see /api/breaches).
    set((state) => ({
      breachEvents: state.breachEvents.map((e) =>
        e.positionId === positionId
          ? { ...e, autoAction: action, actionResult: 'PENDING' }
          : e
      ),
    }));
    try {
      const res = await fetchT(
        `/api/orders/${encodeURIComponent(positionId)}/force-exit`,
        8000,
        { method: 'POST' }
      );
      if (!res.ok) throw new Error(String(res.status));
    } catch {
      set((state) => ({
        breachEvents: state.breachEvents.map((e) =>
          e.positionId === positionId ? { ...e, actionResult: 'FAILED' } : e
        ),
      }));
    }
  },

  dismissNews: (id) => {
    set((state) => ({
      realTimeNews: state.realTimeNews.filter((n) => n.id !== id),
    }));
  },

  refreshNews: async () => {
    try {
      const res = await fetchT('/api/news');
      if (!res.ok) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const items: any[] = await res.json();
      if (!items.length) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const sentLabel = (s: string): any =>
        s === 'POSITIVE' ? 'POSITIVE' : s === 'NEGATIVE' ? 'NEGATIVE' : 'NEUTRAL';
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const impactLvl = (l: string): any =>
        ['HIGH', 'MEDIUM', 'LOW'].includes(l) ? l : 'LOW';
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const validCat = (c: string): any =>
        ['BREAKING','EARNINGS','MACRO','SECTOR','STOCK','GLOBAL','REGULATORY'].includes(c) ? c : 'MACRO';
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const validAction = (a: string): any =>
        ['BUY','SELL','HOLD','EXIT','AVOID','WATCH'].includes(a) ? a : 'HOLD';
      const safeDate = (s: string | undefined | null): Date => {
        if (!s) return new Date();
        // Python datetime: "2026-06-10 14:30:00.123456+05:30" — fix space→T and truncate microseconds to ms
        const norm = s.replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1');
        const d = new Date(norm);
        return isNaN(d.getTime()) ? new Date() : d;
      };
      // DB may return JSON/text columns as strings (e.g. '["RELIANCE"]') — coerce to string[]
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const toArr = (v: any): string[] => {
        if (Array.isArray(v)) return v.map(String);
        if (typeof v === 'string') {
          const t = v.trim();
          if (t.startsWith('[')) {
            try { const p = JSON.parse(t); return Array.isArray(p) ? p.map(String) : []; } catch { /* fall through */ }
          }
          return t ? [t] : [];
        }
        return [];
      };
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const mapped = items.map((n: any) => {
        const ts = safeDate(n.publishedAt);
        return {
          id: n.id,
          headline: n.headline,
          summary: n.summary,
          source: n.source,
          category: validCat(n.category),
          symbols: toArr(n.relevantSymbols),
          sectors: toArr(n.relevantSectors),
          sentiment: { score: n.sentimentScore ?? 0, label: sentLabel(n.sentiment), confidence: n.confidence ?? Math.abs(n.sentimentScore ?? 0) },
          impact: { level: impactLvl(n.impactLevel), priceImpactEstimate: 0, duration: 'SHORT_TERM' as const },
          agentAction: { recommendation: validAction(n.tradingAction), reasoning: n.agentAnalysis ?? '', confidence: n.confidence ?? Math.abs(n.sentimentScore ?? 0), affectedPositions: [], suggestedActions: [] },
          timestamps: { published: ts, fetched: ts, analyzed: ts },
        };
      });
      set({ realTimeNews: mapped });
    } catch { /* API not running — keep existing data */ }
  },

  refreshSectorSentiments: async () => {
    try {
      const res = await fetchT('/api/sector_rotation');
      if (!res.ok) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await res.json();
      const sectors: any[] = data?.sectors ?? [];
      if (!sectors.length) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const trend = (status: string): any =>
        ['LEADING', 'EMERGING'].includes(status) ? 'IMPROVING' : status === 'DECLINING' ? 'DECLINING' : 'STABLE';
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const rec = (status: string): any =>
        status === 'LEADING' ? 'OVERWEIGHT' : status === 'DECLINING' ? 'UNDERWEIGHT' : 'NEUTRAL';
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const statusScore = (status: string): number =>
        status === 'LEADING' ? 0.75 : status === 'EMERGING' ? 0.4
        : status === 'DECLINING' ? -0.75 : -0.2;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const mapped = sectors.map((s: any) => ({
        sector: s.name,
        currentScore: statusScore(s.status),
        score24h: statusScore(s.status),
        score7d: statusScore(s.status),
        trend: trend(s.status),
        newsCount24h: 0,
        topStories: [],
        recommendation: rec(s.status),
      }));
      set({ sectorSentiments: mapped });
    } catch { /* keep existing */ }
  },

  refreshUpcomingEvents: async () => {
    try {
      const res = await fetchT('/api/earnings_calendar');
      if (!res.ok) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await res.json();
      const events: any[] = data?.events ?? [];
      const safeDateE = (s: string | undefined | null): Date => {
        if (!s) return new Date();
        const norm = s.replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1');
        const d = new Date(norm);
        return isNaN(d.getTime()) ? new Date() : d;
      };
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const mapped = events.map((e: any) => ({
        id: e.symbol ?? e.id ?? String(Math.random()),
        title: `${e.symbol ?? 'Unknown'} Earnings`,
        type: 'EARNINGS' as const,
        date: safeDateE(e.date ?? e.expected_date),
        symbols: e.symbol ? [e.symbol] : [],
        sectors: [],
        expectedImpact: 'HIGH' as const,
      }));
      set({ scheduledEvents: mapped });
    } catch { /* keep existing */ }
  },

  acknowledgeEvent: (id) => {
    set((state) => ({
      scheduledEvents: state.scheduledEvents.filter((e) => e.id !== id),
    }));
  },
}));

const _initRealData = async () => {
  for (let attempt = 0; attempt < 10; attempt++) {
    await new Promise(r => setTimeout(r, 2000 * (attempt + 1)));
    try {
      const res = await fetchT('/api/sector_rotation');
      if (res.ok) {
        const store = useAdvancedStore.getState();
        await store.refreshNews();
        await store.refreshSectorSentiments();
        await store.refreshUpcomingEvents();
        break;
      }
    } catch { continue; }
  }
};
_initRealData();
