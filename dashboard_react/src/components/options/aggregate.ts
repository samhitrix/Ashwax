import { rootOf } from './format';
import type { OptionLegRow, OptionPosition } from './types';

/** A position as the page shows it once the "Closed positions" toggle is applied. */
export interface ViewPosition extends OptionPosition {
  visibleLegs: OptionLegRow[];
  vBooked: number;
  vUnbooked: number | null;
  vTotal: number | null;
}

/** The ONE place the closed toggle is applied, so the summary, rows and cards agree.
 * Off: positions closed today are hidden, and an open position's already-closed legs
 * (and their booked P&L) are left out. On: everything the API returned is counted. */
export function viewOf(p: OptionPosition, showClosed: boolean): ViewPosition | null {
  if (p.status !== 'OPEN') {
    if (!showClosed) return null;
    const booked = p.bookedPnl ?? p.pnl ?? 0;
    return { ...p, visibleLegs: p.legs, vBooked: booked, vUnbooked: 0, vTotal: p.pnl ?? booked };
  }
  const unbooked = p.unbookedPnl ?? null;
  const booked = showClosed ? p.bookedPnl ?? 0 : 0;
  return {
    ...p,
    visibleLegs: showClosed ? p.legs : p.legs.filter((l) => l.state !== 'CLOSED'),
    vBooked: booked,
    vUnbooked: unbooked,
    vTotal: unbooked == null ? null : unbooked + booked,
  };
}

/** Sum that stays unknown (null) if any part is unknown -- a partial sum would understate P&L. */
export function sumKnown(values: (number | null)[]): number | null {
  let total = 0;
  for (const v of values) {
    if (v == null) return null;
    total += v;
  }
  return total;
}

export interface StrategyGroup {
  key: string;
  name: string;
  isPaper: boolean;
  underlyings: string;
  structures: string;
  positions: ViewPosition[];
  openLegs: number;
  total: number | null;
  unbooked: number | null;
  booked: number;
  target: number | null;
  targetPct: number | null;
  stop: number | null;
  stopPct: number | null;
}

/** One row per registry strategy AND mode -- each strategy has its own paper/live switch,
 * so one strategy can hold paper and live positions at once. */
export function groupByStrategy(views: ViewPosition[]): StrategyGroup[] {
  const byKey = new Map<string, ViewPosition[]>();
  for (const v of views) {
    const key = `${v.registryStrategy ?? v.strategy}|${v.isPaper}`;
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key)!.push(v);
  }
  const groups: StrategyGroup[] = [];
  for (const [key, positions] of byKey) {
    const first = positions[0];
    const open = positions.filter((p) => p.status === 'OPEN');
    const targets = open.map((p) => p.targetAmount ?? null).filter((t): t is number => t != null);
    const stops = open.map((p) => p.stopAmount ?? null).filter((s): s is number => s != null);
    const single = open.length === 1 ? open[0] : null;
    groups.push({
      key,
      name: first.registryStrategy ?? first.strategy,
      isPaper: first.isPaper,
      underlyings: [...new Set(positions.map((p) => rootOf(p.underlying)))].join(', '),
      structures: [...new Set(positions.map((p) => p.strategy))].join(', '),
      positions,
      openLegs: open.reduce((n, p) => n + p.legs.filter((l) => l.state !== 'CLOSED').length, 0),
      total: sumKnown(positions.map((p) => p.vTotal)),
      unbooked: sumKnown(positions.map((p) => p.vUnbooked)),
      booked: positions.reduce((n, p) => n + p.vBooked, 0),
      target: targets.length ? targets.reduce((a, b) => a + b, 0) : null,
      targetPct: single?.targetPct ?? null,
      stop: stops.length ? stops.reduce((a, b) => a + b, 0) : null,
      stopPct: single?.stopPct ?? null,
    });
  }
  return groups.sort((a, b) => a.name.localeCompare(b.name) || Number(a.isPaper) - Number(b.isPaper));
}

export interface PnlSummary {
  total: number | null;
  unbooked: number | null;
  booked: number;
}

export function summarize(groups: StrategyGroup[]): PnlSummary {
  return {
    total: sumKnown(groups.map((g) => g.total)),
    unbooked: sumKnown(groups.map((g) => g.unbooked)),
    booked: groups.reduce((n, g) => n + g.booked, 0),
  };
}
