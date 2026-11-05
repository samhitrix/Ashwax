// Advanced Features Types for ASHWAX

// ============ ORDER MONITORING ============
export interface OrderMonitoringState {
  isActive: boolean;
  checkIntervalMs: number;
  lastCheckTime: Date;
  checksPerMinute: number;
  avgLatencyMs: number;
  ordersMonitored: number;
  breachesDetected: number;
  autoActionsExecuted: number;
}

export interface OrderBreachEvent {
  id: string;
  positionId: string;
  symbol: string;
  breachType: 'SL_BREACH' | 'TARGET_BREACH' | 'SL_APPROACHING' | 'TARGET_APPROACHING';
  detectedAt: Date;
  ltp: number;
  expectedPrice: number;
  gapPercent: number;
  orderType: 'LIMIT' | 'SL' | 'SL-M';
  orderStatus: 'OPEN' | 'TRIGGER_PENDING' | 'COMPLETE' | 'CANCELLED';
  autoAction?: 'MARKET_EXIT' | 'MODIFY_ORDER' | 'NONE';
  actionResult?: 'SUCCESS' | 'FAILED' | 'PENDING';
  actionOrderId?: string;
  resolvedAt?: Date;
}

export interface GTTOrder {
  id: string;
  positionId: string;
  symbol: string;
  triggerType: 'SINGLE' | 'OCO';
  status: 'ACTIVE' | 'TRIGGERED' | 'CANCELLED' | 'EXPIRED';
  slTrigger: number;
  slLimit: number;
  targetTrigger?: number;
  targetLimit?: number;
  quantity: number;
  createdAt: Date;
  expiresAt: Date;
  lastCheckedAt: Date;
}

export interface PartialFillEvent {
  id: string;
  orderId: string;
  symbol: string;
  totalQuantity: number;
  filledQuantity: number;
  pendingQuantity: number;
  fillPercent: number;
  elapsedMs: number;
  timeoutMs: number;
  status: 'WAITING' | 'TIMEOUT' | 'COMPLETED' | 'CANCELLED';
  action?: 'CONVERT_TO_MARKET' | 'CANCEL' | 'WAIT';
}

// ============ NEWS & SENTIMENT ============
export interface RealTimeNewsItem {
  id: string;
  headline: string;
  summary: string;
  fullText?: string;
  source: string;
  sourceUrl?: string;
  category: 'BREAKING' | 'EARNINGS' | 'MACRO' | 'SECTOR' | 'STOCK' | 'GLOBAL' | 'REGULATORY';
  symbols: string[];
  sectors: string[];
  sentiment: {
    score: number; // -1 to 1
    label: 'VERY_NEGATIVE' | 'NEGATIVE' | 'NEUTRAL' | 'POSITIVE' | 'VERY_POSITIVE';
    confidence: number;
  };
  impact: {
    level: 'MINIMAL' | 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
    priceImpactEstimate: number; // percentage
    duration: 'IMMEDIATE' | 'SHORT_TERM' | 'MEDIUM_TERM' | 'LONG_TERM';
  };
  agentAction: {
    recommendation: 'BUY' | 'SELL' | 'HOLD' | 'EXIT' | 'AVOID' | 'WATCH';
    reasoning: string;
    confidence: number;
    affectedPositions: string[];
    suggestedActions: {
      positionId: string;
      action: string;
      urgency: 'LOW' | 'MEDIUM' | 'HIGH' | 'IMMEDIATE';
    }[];
  };
  timestamps: {
    published: Date;
    fetched: Date;
    analyzed: Date;
    actioned?: Date;
  };
}

export interface ScheduledEvent {
  id: string;
  title: string;
  type: 'EARNINGS' | 'DIVIDEND' | 'SPLIT' | 'AGM' | 'RBI_POLICY' | 'FED_MEETING' | 'GDP' | 'INFLATION' | 'BUDGET' | 'OTHER';
  date: Date;
  time?: string;
  symbols: string[];
  sectors: string[];
  expectedImpact: 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
  blackoutStart?: Date;
  blackoutEnd?: Date;
  positionSizeReduction?: number; // percentage
  notes?: string;
}

export interface SectorSentiment {
  sector: string;
  currentScore: number;
  score24h: number;
  score7d: number;
  trend: 'IMPROVING' | 'STABLE' | 'DECLINING';
  newsCount24h: number;
  topStories: string[];
  recommendation: 'OVERWEIGHT' | 'NEUTRAL' | 'UNDERWEIGHT';
}

// ============ SELF-HEALING ============
export interface ReconciliationResult {
  id: string;
  runAt: Date;
  duration: number;
  dbPositions: number;
  brokerPositions: number;
  matched: number;
  mismatches: {
    type: 'DB_ONLY' | 'BROKER_ONLY' | 'QUANTITY_MISMATCH' | 'PRICE_MISMATCH';
    symbol: string;
    dbValue?: any;
    brokerValue?: any;
    action: string;
    resolved: boolean;
  }[];
  gttsChecked: number;
  gttsRearmed: number;
  incidentsRaised: number;
}

export interface WebSocketHealth {
  status: 'CONNECTED' | 'RECONNECTING' | 'DISCONNECTED' | 'ERROR';
  lastTickTime: Date;
  ticksPerSecond: number;
  reconnectCount: number;
  lastReconnectAt?: Date;
  subscribedSymbols: number;
  activeSymbols: number;
  staleSymbols: string[];
  latencyMs: number;
}

export interface ProcessHealth {
  name: string;
  pid: number;
  status: 'RUNNING' | 'IDLE' | 'RESTARTING' | 'CRASHED' | 'STOPPED';
  cpu: number;
  memory: number;
  memoryLimit: number;
  uptime: number;
  restartCount: number;
  lastRestartAt?: Date;
  lastHeartbeat: Date;
  errors24h: number;
  gcPauses: number;
  gcPauseAvgMs: number;
}

export interface MemoryPressureEvent {
  id: string;
  process: string;
  timestamp: Date;
  rssBeforeMB: number;
  rssAfterMB: number;
  action: 'GC_TRIGGERED' | 'RESTART_INITIATED' | 'ALERT_RAISED';
  result: 'SUCCESS' | 'PARTIAL' | 'FAILED';
  freedMB: number;
}

// ============ INTELLIGENCE ============
export interface ExecutionQualityMetrics {
  symbol: string;
  totalTrades: number;
  avgSlippagePct: number;
  avgFillTimeMs: number;
  marketImpactPct: number;
  bestTimeOfDay: string;
  worstTimeOfDay: string;
  limitFillRate: number;
  slTriggerAccuracy: number;
  recommendation: string;
}

export interface StrategyAttribution {
  strategy: string;
  totalTrades: number;
  winRate: number;
  avgRR: number;
  profitFactor: number;
  maxDrawdown: number;
  sharpeRatio: number;
  attributions: {
    factor: 'ENTRY_TIMING' | 'EXIT_TIMING' | 'POSITION_SIZE' | 'SL_PLACEMENT' | 'TARGET_PLACEMENT' | 'MARKET_REGIME';
    contribution: number; // percentage of P&L attributed to this factor
    improvement: string;
  }[];
  recommendations: string[];
}

export interface MarketRegime {
  current: 'TRENDING_UP' | 'TRENDING_DOWN' | 'RANGING' | 'VOLATILE' | 'CRASH';
  confidence: number;
  since: Date;
  indicators: {
    adx: number;
    atrPercent: number;
    breadth: number;
    vix: number;
    trend50: 'UP' | 'DOWN' | 'FLAT';
    trend200: 'UP' | 'DOWN' | 'FLAT';
  };
  suitableStrategies: string[];
  avoidStrategies: string[];
  positionSizeMultiplier: number;
  historicalAccuracy: number;
}

export interface ConfidenceCalibration {
  strategy: string;
  llmProvider: string;
  totalPredictions: number;
  calibrationData: {
    predictedConfidence: number; // bucket: 0.5-0.6, 0.6-0.7, etc.
    actualWinRate: number;
    sampleSize: number;
    isCalibrated: boolean; // within 10% of predicted
  }[];
  overallCalibration: 'WELL_CALIBRATED' | 'OVERCONFIDENT' | 'UNDERCONFIDENT';
  suggestedThresholdAdjustment: number;
  lastUpdated: Date;
}
