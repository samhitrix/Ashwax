import { create } from 'zustand';
import { playAlertSound } from '../utils/alertSound';
import type {
  Position,
  Order,
  OrderMonitoringAlert,
  NewsItem,
  MarketContext,
  AgentState,
  SelfHealingAction,
  TradingInsight,
  OrderMonitorConfig,
} from '../types/trading';

const API_BASE = '';

// ── Kite API → store type mappers ───────────────────────────────────────────

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let _connFailStreak = 0; // debounce so a transient fetch blip doesn't flap the connection badge
let _agentFailStreak = 0; // same debounce for the Active/Stopped badge (agent_status)
// Toast diffing needs one real poll to establish "what already existed" before it
// can tell "new" apart from "was already there when the page loaded" — without
// these, opening the dashboard with 5 open orders/alerts already active would
// fire 5 toasts (and an alarm) for things that aren't new at all.
let _alertsBaselineSet = false;
let _ordersBaselineSet = false;

// Single-flight guard: refreshData is called from several overlapping intervals
// (App global 2s loop + per-tab timers). Without this, each call fires ~16 fetches
// that pile up faster than they drain, the browser hits ERR_INSUFFICIENT_RESOURCES,
// the rejected fetches read as failures, and the badges flicker. Skip a new cycle
// while one is still in flight.
let _refreshInFlight = false;

// Per-request timeout/abort so a single hung endpoint can neither hold a browser
// socket open indefinitely nor wedge the single-flight flag. An abort rejects the
// promise, which the existing .catch debounce treats as one transient miss.
function fetchT(url: string, ms = 8000): Promise<Response> {
  const ctrl = new AbortController();
  const id = setTimeout(() => ctrl.abort(), ms);
  return fetch(url, { signal: ctrl.signal }).finally(() => clearTimeout(id));
}

function mapPosition(raw: any): Position {
  const qty = Number(raw.quantity ?? 0);
  const avg = Number(raw.average_price ?? 0);
  const ltp = Number(raw.last_price ?? 0);
  const pnl = Number(raw.pnl ?? 0);
  return {
    id: `${raw.tradingsymbol}-${raw.exchange}`,
    tradingSymbol: String(raw.tradingsymbol ?? ''),
    exchange: (raw.exchange ?? 'NSE') as Position['exchange'],
    direction: qty >= 0 ? 'LONG' : 'SHORT',
    quantity: Math.abs(qty),
    averagePrice: avg,
    ltp,
    pnl,
    pnlPercent: avg > 0 ? (pnl / (avg * Math.abs(qty))) * 100 : 0,
    product: (raw.product ?? 'CNC') as Position['product'],
    external: Boolean(raw.external),
    paper: Boolean(raw.paper),
    entryOrderId: '',
    initialSL: Number(raw.initial_sl ?? 0),
    currentSL: Number(raw.current_sl ?? 0),
    initialTarget: Number(raw.initial_target ?? 0),
    currentTarget: Number(raw.current_target ?? 0),
    slStatus: 'ACTIVE',
    targetStatus: 'ACTIVE',
    lastCheckedAt: new Date(),
    checksCount: 0,
    missedTargetCount: 0,
    missedSLCount: 0,
    strategy: String(raw.strategy ?? raw.tag ?? ''),
    confidence: 0,
    entryTimestamp: raw.entry_time ? _sd(String(raw.entry_time)) : new Date(),
  };
}

export interface PnLPoint { time: string; pnl: number }

// Append the current total open-position P&L to the intraday curve. Sampled at
// most once per minute (one point per "HH:MM"); within the same minute the last
// point is updated live. The curve resets when the day rolls over (label wraps
// past 00:00) so it always represents the current session.
function appendPnLPoint(history: PnLPoint[], totalPnl: number): PnLPoint[] {
  const now = new Date();
  const label = now.toLocaleTimeString('en-IN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
  const pnl = Math.round(totalPnl);
  const last = history[history.length - 1];
  if (last && last.time === label) {
    const next = history.slice(0, -1);
    next.push({ time: label, pnl });
    return next;
  }
  // New-day reset: a label earlier than the previous one means the clock wrapped.
  const reset = last && label < last.time ? [] : history;
  const next = [...reset, { time: label, pnl }];
  return next.length > 480 ? next.slice(next.length - 480) : next;
}

const _sd = (s: string): Date => {
  const norm = s.replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1');
  const d = new Date(norm);
  return isNaN(d.getTime()) ? new Date() : d;
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function mapOrder(raw: any): Order {
  return {
    id: String(raw.order_id ?? ''),
    orderId: String(raw.order_id ?? ''),
    tradingSymbol: String(raw.tradingsymbol ?? ''),
    exchange: (raw.exchange ?? 'NSE') as Order['exchange'],
    orderType: (raw.order_type ?? 'MARKET') as Order['orderType'],
    transactionType: (raw.transaction_type ?? 'BUY') as Order['transactionType'],
    quantity: Number(raw.quantity ?? 0),
    price: Number(raw.price ?? 0),
    triggerPrice: raw.trigger_price ? Number(raw.trigger_price) : undefined,
    status: (raw.status ?? 'OPEN') as Order['status'],
    filledQuantity: Number(raw.filled_quantity ?? 0),
    averagePrice: Number(raw.average_price ?? 0),
    product: (raw.product ?? 'CNC') as Order['product'],
    placedAt: raw.order_timestamp ? _sd(raw.order_timestamp) : new Date(),
    updatedAt: raw.exchange_update_timestamp ? _sd(raw.exchange_update_timestamp) : new Date(),
    tag: String(raw.tag ?? ''),
  };
}

// Broker-style order lifecycle toasts (placed/modified/executed/rejected/
// cancelled) — Kite's own web terminal pops one for each of these; this diffs
// two successive /api/orders snapshots by order id to reconstruct the same
// events from polling, since the dashboard has no order-event push channel.
function _detectOrderToasts(prevOrders: Order[], nextOrders: Order[]): ToastItem[] {
  const prevById = new Map(prevOrders.map((o) => [o.id, o]));
  const toasts: ToastItem[] = [];
  for (const o of nextOrders) {
    const label = `${o.transactionType} ${o.quantity} ${o.tradingSymbol}`;
    const prev = prevById.get(o.id);
    if (!prev) {
      if (o.status === 'REJECTED') {
        toasts.push(_newToast('CRITICAL', `Order rejected: ${label}`));
      } else if (o.status !== 'CANCELLED') {
        const px = o.price || o.triggerPrice || 0;
        toasts.push(_newToast('INFO', `Order placed: ${label}${px ? ` @ ₹${px.toFixed(2)}` : ''}`));
      }
      continue;
    }
    if (prev.status !== o.status) {
      if (o.status === 'COMPLETE') {
        toasts.push(_newToast('INFO', `Order executed: ${label} @ ₹${o.averagePrice.toFixed(2)}`));
      } else if (o.status === 'REJECTED') {
        toasts.push(_newToast('CRITICAL', `Order rejected: ${label}`));
      } else if (o.status === 'CANCELLED') {
        toasts.push(_newToast('WARNING', `Order cancelled: ${label}`));
      }
      continue;
    }
    // Same status, still resting — a repeg/manual modify changed price/qty/trigger.
    if (prev.price !== o.price || prev.quantity !== o.quantity || prev.triggerPrice !== o.triggerPrice) {
      toasts.push(_newToast('INFO', `Order modified: ${label} → ₹${(o.price || o.triggerPrice || 0).toFixed(2)}`));
    }
  }
  return toasts;
}

const EMPTY_MARKET_CONTEXT: MarketContext = {
  niftyLevel: 0,
  niftyChange: 0,
  niftyChangePercent: 0,
  vixLevel: 0,
  vixChange: 0,
  marketTrend: 'SIDEWAYS',
  intradayTrend: 'UNKNOWN',
  breadth: { advances: 0, declines: 0, unchanged: 0 },
  sectorRotation: [],
  fiiActivity: { cashBuy: 0, cashSell: 0, netCash: 0, futuresOI: 0, futuresOIChange: 0 },
  diiActivity: { cashBuy: 0, cashSell: 0, netCash: 0 },
};

const EMPTY_AGENT_STATE: AgentState = {
  isActive: false,
  mode: 'PAPER',
  segments: { intraday: 'PAPER', swing: 'PAPER', options: 'PAPER' },
  autonomyLevel: 'ASSISTED',
  currentTask: 'Idle — trading engine not running',
  lastHeartbeat: new Date(),
  circuitBreakers: {
    dailyLoss: { triggered: false, current: 0, limit: 10000 },
    consecutiveLosses: { triggered: false, count: 0, limit: 4 },
    connectivity: { triggered: false, lastCheck: new Date() },
    newsBlackout: { active: false },
  },
  openPositions: 0,
  maxPositions: 5,
  totalExposure: 0,
  maxExposure: 0,
  dailyPnL: 0,
  realizedPnL: 0,
  unrealizedPnL: 0,
};

// ── Store ────────────────────────────────────────────────────────────────────

interface Funds {
  availableCash: number;
  usedMargin: number;
  net: number;
}

export interface AlertItem {
  id: string;
  ts: string;
  tier: 'INFO' | 'WARNING' | 'ALERT' | 'CRITICAL' | 'EMERGENCY';
  message: string;
  acknowledged: boolean;
  source?: string;
}

/** A transient bottom-left notification — a new system alert, or a broker-style
 * order lifecycle event (placed/modified/executed/rejected/cancelled). Distinct
 * from AlertItem: it has no acknowledged/source state, it's fire-and-forget. */
export interface ToastItem {
  id: string;
  ts: string;
  tier: AlertItem['tier'];
  message: string;
}

const MAX_TOASTS = 4;
let _toastSeq = 0;
function _newToast(tier: ToastItem['tier'], message: string): ToastItem {
  _toastSeq += 1;
  return { id: `t${Date.now()}-${_toastSeq}`, ts: new Date().toLocaleTimeString('en-IN'), tier, message };
}

interface TradingStore {
  positions: Position[];
  orders: Order[];
  pnlHistory: PnLPoint[];
  funds: Funds | null;
  kiteConnected: boolean;
  alerts: OrderMonitoringAlert[];
  systemAlerts: AlertItem[];
  toasts: ToastItem[];
  hasUnseenCriticalAlert: boolean;
  dismissToast: (id: string) => void;
  news: NewsItem[];
  marketContext: MarketContext;
  agentState: AgentState;
  selfHealingActions: SelfHealingAction[];
  insights: TradingInsight[];
  orderMonitorConfig: OrderMonitorConfig;
  activeTab: string;
  selectedPosition: Position | null;
  isNewsFilterActive: boolean;
  newsFilter: { categories: string[]; minImpact: string; sentimentFilter: string };
  setActiveTab: (tab: string) => void;
  selectPosition: (position: Position | null) => void;
  resolveAlert: (alertId: string) => void;
  executeHealingAction: (actionId: string) => void;
  updateOrderMonitorConfig: (config: Partial<OrderMonitorConfig>) => void;
  updateNewsFilter: (filter: Partial<TradingStore['newsFilter']>) => void;
  refreshData: () => void;
  acknowledgeInsight: (insightId: string) => void;
}

export const useTradingStore = create<TradingStore>((set) => ({
  positions: [],
  orders: [],
  pnlHistory: [],
  funds: null,
  kiteConnected: false,
  alerts: [],
  systemAlerts: [],
  toasts: [],
  hasUnseenCriticalAlert: false,
  news: [],
  marketContext: EMPTY_MARKET_CONTEXT,
  agentState: EMPTY_AGENT_STATE,
  selfHealingActions: [],
  insights: [],

  orderMonitorConfig: {
    checkIntervalMs: 500,
    slBreachTolerancePercent: 0.5,
    targetBreachTolerancePercent: 1.0,
    maxSlippagePercent: 0.3,
    autoMarketExitOnMissedSL: true,
    autoMarketExitOnMissedTarget: false,
    partialFillTimeoutMs: 30000,
    trailingStopEnabled: true,
    trailingStopType: 'ATR',
    trailingStopValue: 2,
  },

  activeTab: 'overview',
  selectedPosition: null,
  isNewsFilterActive: false,
  newsFilter: { categories: [], minImpact: 'LOW', sentimentFilter: 'ALL' },

  setActiveTab: (tab) => set((state) => ({
    activeTab: tab,
    // Visiting the Alerts tab is what "seeing" a critical alert means — stop the
    // nav item blinking once the user has actually looked at it.
    hasUnseenCriticalAlert: tab === 'alerts' ? false : state.hasUnseenCriticalAlert,
  })),
  selectPosition: (position) => set({ selectedPosition: position }),
  dismissToast: (id) => set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),

  resolveAlert: (alertId) => set((state) => ({
    alerts: state.alerts.map((a) =>
      a.id === alertId ? { ...a, actionTaken: true, resolvedAt: new Date() } : a
    ),
  })),

  executeHealingAction: (actionId) => set((state) => ({
    selfHealingActions: state.selfHealingActions.map((a) =>
      a.id === actionId ? { ...a, status: 'EXECUTING' as const } : a
    ),
  })),

  updateOrderMonitorConfig: (config) => set((state) => ({
    orderMonitorConfig: { ...state.orderMonitorConfig, ...config },
  })),

  updateNewsFilter: (filter) => set((state) => ({
    newsFilter: { ...state.newsFilter, ...filter },
    isNewsFilterActive: true,
  })),

  refreshData: () => {
    if (_refreshInFlight) return;
    _refreshInFlight = true;
    const _p1 = Promise.all([
      fetchT(`${API_BASE}/api/positions`).then((r) => r.json()),
      fetchT(`${API_BASE}/api/orders`).then((r) => r.json()),
      fetchT(`${API_BASE}/api/margins`).then((r) => r.json()),
      fetchT(`${API_BASE}/api/session`).then((r) => r.json()),
      fetchT(`${API_BASE}/api/closed_positions`).then((r) => r.json()).catch(() => []),
      fetchT(`${API_BASE}/api/options_positions`).then((r) => r.json()).catch(() => []),
    ])
      .then(([posRes, ordRes, marginRes, sessionRes, closedRes, optionsRes]) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const raw: any[] = Array.isArray(posRes?.net) ? posRes.net : [];
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const rawOrders: any[] = Array.isArray(ordRes) ? ordRes : [];
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const rawClosed: any[] = Array.isArray(closedRes) ? closedRes : [];
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const rawOptions: any[] = Array.isArray(optionsRes) ? optionsRes : [];

        const positions = raw
          .filter((p) => Number(p.quantity) !== 0)
          .map(mapPosition);

        // Options never appeared in Today's P&L / the Overview curve -- /api/positions
        // and /api/closed_positions only ever query the equity `trades`/`paper_trades`
        // tables, options_positions was never in the mix. Same open+today-closed
        // convention as equity: every currently OPEN structure's live P&L, plus any
        // structure closed TODAY (IST) -- not every closed row in the last 30 days.
        const todayIST = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
        const optionsUnrealizedPnL = rawOptions
          .filter((o) => o.status === 'OPEN')
          .reduce((sum, o) => sum + Number(o.pnl ?? 0), 0);
        const optionsRealizedPnL = rawOptions
          .filter((o) => o.status === 'CLOSED' && String(o.closedAt ?? '').slice(0, 10) === todayIST)
          .reduce((sum, o) => sum + Number(o.pnl ?? 0), 0);

        const unrealizedPnL = positions.reduce((sum, p) => sum + p.pnl, 0) + optionsUnrealizedPnL;
        // Today's already-closed trades (live + paper) — the Overview P&L card
        // and Intraday P&L curve must reflect the whole day, not just positions
        // still open right now, else a position that closed profitably drops
        // out of "today's P&L" the instant it's squared off.
        const realizedPnL =
          rawClosed.reduce((sum, c) => sum + Number(c.pnl ?? 0), 0) + optionsRealizedPnL;
        const dailyPnL = unrealizedPnL + realizedPnL;

        // Funds — only when the broker returned real equity data, else null (empty state).
        // Read the server-normalized `ashwax` block, never the raw Kite fields:
        // Zerodha reports equity.available.cash = 0 when the free cash sits in
        // available.live_balance, which made this card read ₹0 all day.
        const norm = marginRes?.ashwax;
        const funds: Funds | null = norm
          ? {
              availableCash: Number(norm.available_cash ?? 0),
              usedMargin: Number(norm.used_margin ?? 0),
              net: Number(norm.net ?? 0),
            }
          : null;

        const kiteConnected = sessionRes?.ok === true;
        _connFailStreak = 0; // all four endpoints resolved → healthy
        const nextOrders = rawOrders.map(mapOrder);

        set((state) => {
          // Same first-poll-is-just-a-baseline rule as alerts (see above) — a
          // dashboard opened with orders already resting must not toast for them.
          const orderToasts = _ordersBaselineSet
            ? _detectOrderToasts(state.orders, nextOrders)
            : [];
          _ordersBaselineSet = true;
          return {
            positions,
            orders: nextOrders,
            funds,
            kiteConnected,
            pnlHistory: appendPnLPoint(state.pnlHistory, dailyPnL),
            toasts: orderToasts.length
              ? [...state.toasts, ...orderToasts].slice(-MAX_TOASTS)
              : state.toasts,
            agentState: {
              ...state.agentState,
              openPositions: positions.length,
              unrealizedPnL,
              realizedPnL,
              dailyPnL,
              lastHeartbeat: new Date(),
            },
          };
        });
      })
      .catch((err) => {
        // A single transient API/network blip must NOT flap the connection badge.
        // Only mark disconnected after 2 consecutive failures; keep last-good funds
        // until then so the dashboard stops connecting/disconnecting every few seconds.
        console.error('[ashwax] refreshData failed:', err);
        _connFailStreak += 1;
        set((state) => ({
          agentState: { ...state.agentState, lastHeartbeat: new Date() },
          ...(_connFailStreak >= 2 ? { funds: null, kiteConnected: false } : {}),
        }));
      });

    // Market context — NIFTY LTP + VIX from regime (independent, never blocks)
    const _pMkt = Promise.all([
      fetchT(`${API_BASE}/api/ltp?symbols=NSE:NIFTY 50,NSE:INDIA VIX`).then((r) => r.json()).catch(() => null),
      fetchT(`${API_BASE}/api/regime`).then((r) => r.json()).catch(() => null),
      fetchT(`${API_BASE}/api/sector_performance`).then((r) => r.json()).catch(() => null),
      fetchT(`${API_BASE}/api/market_breadth`).then((r) => r.json()).catch(() => null),
      fetchT(`${API_BASE}/api/fii_dii`).then((r) => r.json()).catch(() => null),
      fetchT(`${API_BASE}/api/agent_status`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ]).then(([ltpData, regimeData, sectorData, breadthData, fiiDiiData, agentStatusData]: any[]) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const niftyEntry: any = ltpData?.['NSE:NIFTY 50'] ?? ltpData?.['NSE:Nifty 50'] ?? null;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const vixEntry: any = ltpData?.['NSE:INDIA VIX'] ?? null;
      const niftyLevel = niftyEntry ? Number(niftyEntry.last_price ?? niftyEntry.ltp ?? 0) : 0;
      const niftyClose = niftyEntry ? Number(niftyEntry.ohlc?.close ?? 0) : 0;
      const niftyChange = niftyClose > 0 ? niftyLevel - niftyClose : 0;
      const niftyChangePercent = niftyClose > 0 ? (niftyChange / niftyClose) * 100 : 0;
      const vixLevel = vixEntry
        ? Number(vixEntry.last_price ?? vixEntry.ltp ?? 0)
        : Number(regimeData?.vix ?? regimeData?.indicators?.vix ?? 0);

      // Regime → trend
      const _trendMap: Record<string, 'BULLISH' | 'BEARISH' | 'SIDEWAYS' | 'VOLATILE'> = {
        STRONG_BULL: 'BULLISH', BULL: 'BULLISH',
        NEUTRAL: 'SIDEWAYS', UNKNOWN: 'SIDEWAYS',
        BEAR: 'BEARISH', STRONG_BEAR: 'BEARISH',
      };
      const rawRegime = String(regimeData?.regime ?? '').toUpperCase();
      const marketTrend = _trendMap[rawRegime] ?? 'SIDEWAYS';
      // Day-scoped regime (live loop, 5-min refresh) — distinct from the
      // structural trend above; UNKNOWN when off-hours / feed unavailable.
      const _intraMap: Record<string, 'BULLISH' | 'BEARISH' | 'SIDEWAYS'> = {
        BULL: 'BULLISH', BEAR: 'BEARISH', NEUTRAL: 'SIDEWAYS',
      };
      const rawIntra = String(regimeData?.intraday_regime ?? '').toUpperCase();
      const intradayTrend = _intraMap[rawIntra] ?? 'UNKNOWN';

      // Sector rotation
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const sectorRotation = (Array.isArray(sectorData) ? sectorData : []).map((s: any) => ({
        sector: String(s.sector ?? s.name ?? ''),
        performance: Number(s.change_pct ?? s.performance ?? 0),
        trend: (['LEADING', 'LAGGING', 'NEUTRAL'].includes(String(s.trend ?? '').toUpperCase())
          ? String(s.trend).toUpperCase()
          : 'NEUTRAL') as 'LEADING' | 'LAGGING' | 'NEUTRAL',
      }));

      // Market breadth
      const breadth = {
        advances: Number(breadthData?.advances ?? 0),
        declines: Number(breadthData?.declines ?? 0),
        unchanged: Number(breadthData?.unchanged ?? 0),
      };

      // FII / DII
      const fiiRaw = fiiDiiData?.fii ?? {};
      const diiRaw = fiiDiiData?.dii ?? {};
      const fiiActivity = {
        cashBuy: Number(fiiRaw.buy ?? 0),
        cashSell: Number(fiiRaw.sell ?? 0),
        netCash: Number(fiiRaw.net ?? 0),
        futuresOI: 0,
        futuresOIChange: 0,
      };
      const diiActivity = {
        cashBuy: Number(diiRaw.buy ?? 0),
        cashSell: Number(diiRaw.sell ?? 0),
        netCash: Number(diiRaw.net ?? 0),
      };

      // agentStatusData === null => couldn't determine (transient fetch blip or 503
      // from a momentarily-busy DB pool). Keep last-good until 2 consecutive misses so
      // a single bad sample doesn't flap the Active/Stopped badge.
      set((state) => {
        let isActive: boolean;
        if (agentStatusData == null) {
          _agentFailStreak += 1;
          isActive = _agentFailStreak >= 2 ? false : state.agentState.isActive;
        } else {
          _agentFailStreak = 0;
          isActive = agentStatusData.active === true;
        }
        return {
          marketContext: {
            ...state.marketContext,
            niftyLevel,
            niftyChange,
            niftyChangePercent,
            vixLevel,
            marketTrend,
            intradayTrend,
            sectorRotation,
            breadth,
            fiiActivity,
            diiActivity,
          },
          agentState: {
            ...state.agentState,
            isActive,
            mode: (agentStatusData?.mode ?? state.agentState.mode) as 'PAPER' | 'LIVE' | 'MIXED',
            segments: agentStatusData?.segments ?? state.agentState.segments,
            currentTask: isActive
              ? `Engine running — ${agentStatusData?.mode ?? state.agentState.mode} mode`
              : 'Idle — trading engine not running',
          },
        };
      });
    });

    // System alerts — independent fetch; never blocks positions/orders/funds
    const _pAlerts = fetchT(`${API_BASE}/api/alerts`)
      .then((r) => r.json())
      .then((data: AlertItem[]) => {
        if (!Array.isArray(data)) return;
        set((state) => {
          // First poll this session just establishes the baseline — without this,
          // every pre-existing alert from before the page loaded would look "new"
          // and flood toasts/sound the instant you open the dashboard.
          if (!_alertsBaselineSet) {
            _alertsBaselineSet = true;
            return { systemAlerts: data };
          }
          // Sound only for alerts not seen in a previous poll — an alert that's
          // still active on the next 2s cycle must not re-alarm every tick.
          const prevIds = new Set(state.systemAlerts.map((a) => a.id));
          const newOnes = data.filter((a) => !prevIds.has(a.id));
          const newCritical = newOnes.filter((a) => a.tier === 'CRITICAL' || a.tier === 'EMERGENCY');
          if (newCritical.length === 0) {
            return { systemAlerts: data };
          }
          const tier = newCritical.some((a) => a.tier === 'EMERGENCY') ? 'EMERGENCY' : 'CRITICAL';
          playAlertSound(tier);
          const newToasts = newCritical.map((a) => _newToast(a.tier, a.message));
          return {
            systemAlerts: data,
            // The nav item blinks until the user actually opens the Alerts tab
            // (cleared in setActiveTab).
            toasts: [...state.toasts, ...newToasts].slice(-MAX_TOASTS),
            hasUnseenCriticalAlert: true,
          };
        });
      })
      .catch(() => {/* supervisor not running — silent */});

    // Release the single-flight guard only after every fetch in this cycle settles.
    Promise.allSettled([_p1, _pMkt, _pAlerts]).finally(() => {
      _refreshInFlight = false;
    });
  },

  acknowledgeInsight: (insightId) => set((state) => ({
    insights: state.insights.filter((i) => i.id !== insightId),
  })),
}));
