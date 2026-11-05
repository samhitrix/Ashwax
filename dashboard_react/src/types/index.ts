// ── Domain types shared across all tabs ───────────────────────────────────────

export type AlertTier = "INFO" | "WARNING" | "ALERT" | "CRITICAL" | "EMERGENCY";
export type OrderStatus = "PENDING" | "OPEN" | "PARTIAL" | "FILLED" | "CANCELLED" | "REJECTED";
export type SystemLevel = "FULL_OPS" | "ELEVATED" | "REDUCED" | "MONITOR_ONLY" | "EMERGENCY";
export type InvariantSeverity = "OK" | "WARNING" | "CRITICAL";

export interface EquityPoint {
  ts: string;
  equity: number;
  pnl: number;
}

export interface Position {
  symbol: string;
  direction: "long" | "short";
  quantity: number;
  avgPrice: number;
  ltp: number;
  pnl: number;
  product: string;
}

export interface Order {
  orderId: string;
  symbol: string;
  direction: "BUY" | "SELL";
  quantity: number;
  filled: number;
  status: OrderStatus;
  orderType: string;
  price: number | null;
  avgPrice: number;
  placedAt: string;
  tag: string;
}

export interface TradeActivity {
  id: string;
  ts: string;
  symbol: string;
  direction: "BUY" | "SELL";
  quantity: number;
  fillPrice: number;
  pnl: number | null;
  strategy: string;
}

export interface AlertItem {
  id: string;
  ts: string;
  tier: AlertTier;
  message: string;
  acknowledged: boolean;
}

export interface InvariantResult {
  id: string;
  severity: InvariantSeverity;
  message: string;
}

export interface LoopStatus {
  name: string;
  alive: boolean;
  lastTick: string;
}

export interface DashboardState {
  equityCurve: EquityPoint[];
  currentEquity: number;
  startingCapital: number;
  dailyPnl: number;
  dailyLossSoFar: number;
  positions: Position[];
  orders: Order[];
  activity: TradeActivity[];
  alerts: AlertItem[];
  systemLevel: SystemLevel;
  invariants: InvariantResult[];
  loops: LoopStatus[];
  paperMode: boolean;
  paperPositions: Position[];
  paperOrders: Order[];
  masterSwitch: boolean;
  autonomyMode: "manual" | "assisted" | "autonomous";
  lastUpdated: string;
}
