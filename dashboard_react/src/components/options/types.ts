export interface OptionLegRow {
  tradingsymbol: string;
  instrumentType: string;
  strike: number;
  direction: string;
  lots: number;
  premium: number;
  ltp: number | null;
  exitPremium: number | null;
  pnl: number | null;
  // 'CLOSED' for a leg already stopped out/closed while its position is still OPEN (per-leg stops).
  state?: string;
  exitReason?: string;
  exitAt?: string;
}

export interface Breakeven {
  strike: number;
  pctFromSpot: number | null;
}

export interface OptionPosition {
  positionId: string;
  underlying: string;
  strategy: string;
  registryStrategy?: string;
  status: string;
  legs: OptionLegRow[];
  expiry: string;
  dte: number | null;
  netCredit: number;
  maxLoss: number;
  maxProfit: number;
  entryMargin: number | null;
  pnl: number | null;
  pctOfMaxProfit: number | null;
  breakeven: Breakeven[];
  pop: number | null;
  targetAmount?: number | null;
  targetPct?: number | null;
  stopAmount?: number | null;
  stopPct?: number | null;
  todayPnl: number | null;
  bookedPnl: number | null;
  unbookedPnl: number | null;
  spot: number | null;
  exitReason: string;
  openedAt: string;
  closedAt: string;
  isPaper: boolean;
  product?: 'MIS' | 'NRML';
}

export interface FundsMarginsSide {
  fundAvailable: number;
  marginAvailable: number;
  sharedRiskCeiling: number;
}

export interface FundsMarginsResponse {
  paper: FundsMarginsSide;
  live: FundsMarginsSide;
}
