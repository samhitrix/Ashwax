// ASHWAX Trading Types

export type OrderType = 'LIMIT' | 'MARKET' | 'SL' | 'SL-M';
export type OrderStatus = 'PENDING' | 'OPEN' | 'COMPLETE' | 'CANCELLED' | 'REJECTED' | 'TRIGGER_PENDING';
export type TradeDirection = 'LONG' | 'SHORT';
export type ProductType = 'MIS' | 'CNC' | 'NRML';
export type ExchangeSegment = 'NSE' | 'BSE' | 'NFO';

export interface Order {
  id: string;
  orderId: string;
  tradingSymbol: string;
  exchange: ExchangeSegment;
  orderType: OrderType;
  transactionType: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  triggerPrice?: number;
  status: OrderStatus;
  filledQuantity: number;
  averagePrice: number;
  product: ProductType;
  placedAt: Date;
  updatedAt: Date;
  parentOrderId?: string;
  tag?: string;
}

export interface Position {
  id: string;
  tradingSymbol: string;
  exchange: ExchangeSegment;
  direction: TradeDirection;
  quantity: number;
  averagePrice: number;
  ltp: number;
  pnl: number;
  pnlPercent: number;
  product: ProductType;
  external?: boolean;
  paper?: boolean;
  // Order Management
  entryOrderId: string;
  slOrderId?: string;
  targetOrderId?: string;
  trailingSL?: number;
  initialSL: number;
  currentSL: number;
  initialTarget: number;
  currentTarget: number;
  // Monitoring State
  slStatus: 'ACTIVE' | 'TRIGGERED' | 'MODIFIED' | 'MISSED';
  targetStatus: 'ACTIVE' | 'TRIGGERED' | 'MODIFIED' | 'MISSED' | 'PARTIAL';
  lastCheckedAt: Date;
  checksCount: number;
  missedTargetCount: number;
  missedSLCount: number;
  // Strategy Info
  strategy: string;
  confidence: number;
  entryTimestamp: Date;
}

export interface OrderMonitoringAlert {
  id: string;
  positionId: string;
  symbol: string;
  alertType: 'SL_BREACH' | 'TARGET_BREACH' | 'ORDER_REJECTED' | 'EXECUTION_DELAY' | 'SLIPPAGE' | 'PARTIAL_FILL';
  severity: 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
  message: string;
  ltp: number;
  expectedPrice: number;
  action?: 'MARKET_EXIT' | 'MODIFY_ORDER' | 'CANCEL_AND_RETRY' | 'MANUAL_REVIEW';
  actionTaken?: boolean;
  timestamp: Date;
  resolvedAt?: Date;
}

export interface NewsItem {
  id: string;
  headline: string;
  summary: string;
  source: string;
  category: 'MARKET' | 'SECTOR' | 'STOCK' | 'MACRO' | 'GLOBAL' | 'REGULATORY';
  sentiment: 'POSITIVE' | 'NEGATIVE' | 'NEUTRAL';
  sentimentScore: number; // -1 to 1
  relevantSymbols: string[];
  relevantSectors: string[];
  impactLevel: 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
  publishedAt: Date;
  processedAt: Date;
  tradingAction?: 'BUY' | 'SELL' | 'HOLD' | 'EXIT' | 'AVOID';
  agentAnalysis?: string;
}

export interface MarketContext {
  niftyLevel: number;
  niftyChange: number;
  niftyChangePercent: number;
  vixLevel: number;
  vixChange: number;
  marketTrend: 'BULLISH' | 'BEARISH' | 'SIDEWAYS' | 'VOLATILE';
  // Day-scoped regime from the live trading loop (NIFTY day-move + breadth,
  // refreshed every ~5 min). UNKNOWN = off-hours or index feed unavailable.
  intradayTrend: 'BULLISH' | 'BEARISH' | 'SIDEWAYS' | 'UNKNOWN';
  breadth: {
    advances: number;
    declines: number;
    unchanged: number;
  };
  sectorRotation: {
    sector: string;
    performance: number;
    trend: 'LEADING' | 'LAGGING' | 'NEUTRAL';
  }[];
  fiiActivity: {
    cashBuy: number;
    cashSell: number;
    netCash: number;
    futuresOI: number;
    futuresOIChange: number;
  };
  diiActivity: {
    cashBuy: number;
    cashSell: number;
    netCash: number;
  };
}

export interface AgentState {
  isActive: boolean;
  mode: 'PAPER' | 'LIVE' | 'MIXED';
  segments: { intraday: 'PAPER' | 'LIVE'; swing: 'PAPER' | 'LIVE'; options: 'PAPER' | 'LIVE' };
  autonomyLevel: 'MANUAL' | 'ASSISTED' | 'AUTONOMOUS';
  currentTask: string;
  lastHeartbeat: Date;
  circuitBreakers: {
    dailyLoss: { triggered: boolean; current: number; limit: number };
    consecutiveLosses: { triggered: boolean; count: number; limit: number };
    connectivity: { triggered: boolean; lastCheck: Date };
    newsBlackout: { active: boolean; reason?: string; expiresAt?: Date };
  };
  openPositions: number;
  maxPositions: number;
  totalExposure: number;
  maxExposure: number;
  dailyPnL: number;
  realizedPnL: number;
  unrealizedPnL: number;
}

export interface OrderMonitorConfig {
  checkIntervalMs: number;
  slBreachTolerancePercent: number;
  targetBreachTolerancePercent: number;
  maxSlippagePercent: number;
  autoMarketExitOnMissedSL: boolean;
  autoMarketExitOnMissedTarget: boolean;
  partialFillTimeoutMs: number;
  trailingStopEnabled: boolean;
  trailingStopType: 'FIXED' | 'ATR' | 'PERCENTAGE';
  trailingStopValue: number;
}

export interface SelfHealingAction {
  id: string;
  type: 'ORDER_RETRY' | 'SL_REPLACEMENT' | 'MARKET_EXIT' | 'POSITION_RECONCILE' | 'CONNECTION_RESET';
  trigger: string;
  positionId?: string;
  status: 'PENDING' | 'EXECUTING' | 'SUCCESS' | 'FAILED';
  attempts: number;
  maxAttempts: number;
  createdAt: Date;
  executedAt?: Date;
  result?: string;
}

export interface TradingInsight {
  id: string;
  type: 'PATTERN' | 'ANOMALY' | 'OPPORTUNITY' | 'RISK' | 'IMPROVEMENT';
  title: string;
  description: string;
  confidence: number;
  actionable: boolean;
  suggestedAction?: string;
  relatedData: Record<string, any>;
  createdAt: Date;
}
