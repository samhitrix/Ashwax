import { useApi } from "../hooks/useApi";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import { Brain, TrendingUp, AlertTriangle, CheckCircle, XCircle, Zap } from "lucide-react";

// ── Types ────────────────────────────────────────────────────────────────────

interface Pattern {
  pattern_name: string;
  confidence: "WEAK" | "MEDIUM" | "STRONG";
  win_rate: number;
  sample_size: number;
  description?: string;
}

interface LearningSummary {
  trade_count: number;
  computed_at: string;
  patterns: Pattern[];
  summaries: Record<string, {
    total_trades: number;
    wins: number;
    losses: number;
    top_failure_reason?: string;
    top_win_reason?: string;
  }>;
}

interface CalibrationBucket {
  confidence_bucket: string;
  predicted_wr: number | null;
  actual_wr: number | null;
  trade_count: number | null;
  brier_score: number | null;
  period_start: string;
  period_end: string;
}

type CalibrationData = Record<string, CalibrationBucket[]>;

interface StrategyExpectancy {
  expectancy: number;
  avg_win: number;
  avg_loss: number;
  win_rate: number;
  total_trades: number;
}

interface ActiveIncident {
  invariantId: string;
  severity: string;
  message: string;
  openedAt: string;
}

interface HealthOverviewRow {
  strategy: string;
  tier: string;
  sizeMultiplier: number;
  activeIncident: ActiveIncident | null;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function cn(...classes: (string | false | undefined)[]) {
  return classes.filter(Boolean).join(" ");
}

function pct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function fmt(v: number | null | undefined, decimals = 2): string {
  if (v == null) return "—";
  return v.toFixed(decimals);
}

const CONFIDENCE_COLOR: Record<string, string> = {
  STRONG: "text-green-400 bg-green-400/10 border-green-400/30",
  MEDIUM: "text-yellow-400 bg-yellow-400/10 border-yellow-400/30",
  WEAK:   "text-red-400 bg-red-400/10 border-red-400/30",
};

const CONFIDENCE_DOT: Record<string, string> = {
  STRONG: "bg-green-400",
  MEDIUM: "bg-yellow-400",
  WEAK:   "bg-red-400",
};

const TIER_COLOR: Record<string, string> = {
  HEALTHY:   "text-green-400 bg-green-400/10 border-green-400/30",
  CAUTIOUS:  "text-yellow-400 bg-yellow-400/10 border-yellow-400/30",
  DEGRADED:  "text-orange-400 bg-orange-400/10 border-orange-400/30",
  STRESSED:  "text-orange-400 bg-orange-400/10 border-orange-400/30",
  SUSPENDED: "text-red-400 bg-red-400/10 border-red-400/30",
};

// ── Sub-components ───────────────────────────────────────────────────────────

function StatBox({ label, value, sub, color = "text-white" }: {
  label: string; value: string | number; sub?: string; color?: string;
}) {
  return (
    <div className="bg-[#0d1117] rounded-lg p-3">
      <p className="text-xs text-gray-400 mb-1">{label}</p>
      <p className={cn("text-xl font-mono font-bold", color)}>{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-0.5">{sub}</p>}
    </div>
  );
}

function PatternCard({ p }: { p: Pattern }) {
  const colorClass = CONFIDENCE_COLOR[p.confidence] ?? CONFIDENCE_COLOR.WEAK;
  const dotClass = CONFIDENCE_DOT[p.confidence] ?? "bg-gray-400";
  return (
    <div className="bg-[#0d1117] rounded-lg p-4 flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-white truncate">{p.pattern_name}</span>
        <span className={cn("text-xs px-2 py-0.5 rounded border font-mono shrink-0", colorClass)}>
          {p.confidence}
        </span>
      </div>
      <div className="flex items-center gap-3 text-xs text-gray-400">
        <span className="flex items-center gap-1">
          <span className={cn("inline-block w-2 h-2 rounded-full", dotClass)} />
          WR {pct(p.win_rate)}
        </span>
        <span>n={p.sample_size}</span>
      </div>
      {p.description && (
        <p className="text-xs text-gray-500 leading-relaxed">{p.description}</p>
      )}
    </div>
  );
}

function CalibrationChart({ data }: { data: CalibrationData }) {
  const entries = Object.entries(data);
  if (entries.length === 0) {
    return <p className="text-xs text-gray-500 text-center py-8">No calibration data</p>;
  }

  const [modelName, buckets] = entries[0];
  const chartData = buckets
    .filter((b) => b.actual_wr != null && b.predicted_wr != null)
    .map((b) => ({
      bucket:    b.confidence_bucket,
      predicted: b.predicted_wr != null ? +(b.predicted_wr * 100).toFixed(1) : null,
      actual:    b.actual_wr    != null ? +(b.actual_wr    * 100).toFixed(1) : null,
      count:     b.trade_count,
    }));

  return (
    <div>
      <p className="text-xs text-gray-500 mb-3">
        Model: <span className="text-gray-300 font-mono">{modelName}</span>
      </p>
      <ResponsiveContainer width="100%" height={180}>
        <BarChart data={chartData} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
          <XAxis dataKey="bucket" tick={{ fill: "#9ca3af", fontSize: 10 }} axisLine={false} tickLine={false} />
          <YAxis tick={{ fill: "#9ca3af", fontSize: 10 }} axisLine={false} tickLine={false} unit="%" />
          <Tooltip
            contentStyle={{ background: "#0d1117", border: "1px solid #374151", borderRadius: 6, fontSize: 11 }}
            formatter={(v, name) => [
              `${Number(v)}%`, name === "predicted" ? "Predicted" : "Actual",
            ]}
          />
          <ReferenceLine y={50} stroke="#374151" strokeDasharray="3 3" />
          <Bar dataKey="predicted" fill="#4b5563" radius={[3, 3, 0, 0]} name="predicted" />
          <Bar dataKey="actual"    fill="#22c55e" radius={[3, 3, 0, 0]} name="actual" />
        </BarChart>
      </ResponsiveContainer>
      <div className="flex gap-4 mt-2 justify-end text-xs text-gray-500">
        <span className="flex items-center gap-1">
          <span className="inline-block w-3 h-3 rounded-sm bg-[#4b5563]" />Predicted
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-3 h-3 rounded-sm bg-green-500" />Actual
        </span>
      </div>
    </div>
  );
}

function AttributionChart({ summaries }: { summaries: LearningSummary["summaries"] }) {
  const rows = Object.entries(summaries).map(([strat, s]) => ({
    strategy: strat.length > 12 ? strat.slice(0, 12) + "…" : strat,
    wins:   s.wins,
    losses: -(s.losses),
    winRate: s.total_trades > 0 ? +(s.wins / s.total_trades * 100).toFixed(1) : 0,
  }));

  if (rows.length === 0) {
    return <p className="text-xs text-gray-500 text-center py-8">No attribution data</p>;
  }

  return (
    <ResponsiveContainer width="100%" height={Math.max(140, rows.length * 36)}>
      <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 16, left: 8, bottom: 0 }}>
        <XAxis type="number" tick={{ fill: "#9ca3af", fontSize: 10 }} axisLine={false} tickLine={false} />
        <YAxis
          dataKey="strategy" type="category"
          tick={{ fill: "#9ca3af", fontSize: 10 }} axisLine={false} tickLine={false} width={72}
        />
        <Tooltip
          contentStyle={{ background: "#0d1117", border: "1px solid #374151", borderRadius: 6, fontSize: 11 }}
          formatter={(v, name) => [
            name === "wins" ? `+${Number(v)} wins` : `${Math.abs(Number(v))} losses`,
            name === "wins" ? "Wins" : "Losses",
          ]}
        />
        <ReferenceLine x={0} stroke="#374151" />
        <Bar dataKey="wins"   fill="#22c55e" radius={[0, 3, 3, 0]} name="wins" />
        <Bar dataKey="losses" fill="#ef4444" radius={[3, 0, 0, 3]} name="losses" />
      </BarChart>
    </ResponsiveContainer>
  );
}

// ── Main Component ───────────────────────────────────────────────────────────

export default function Learning() {
  const { data: summary, loading: lSum } = useApi<LearningSummary>("/api/learning_summary", 60_000);
  const { data: calib,   loading: lCal } = useApi<CalibrationData>("/api/calibration",      60_000);
  const { data: expRaw,  loading: lExp } = useApi<Record<string, StrategyExpectancy>>("/api/expectancy", 60_000);
  const { data: healthOverview } = useApi<HealthOverviewRow[]>("/api/health_overview", 15_000);

  const loading = lSum || lCal || lExp;
  const throttled = (healthOverview ?? []).filter((r) => r.sizeMultiplier < 1.0);

  const patterns    = summary?.patterns  ?? [];
  const summaries   = summary?.summaries ?? {};
  const tradeCount  = summary?.trade_count ?? 0;
  const computedAt  = summary?.computed_at ?? "";

  const stratCount  = Object.keys(summaries).length;
  const strongCount = patterns.filter((p) => p.confidence === "STRONG").length;

  const expectancies = expRaw ? Object.entries(expRaw) : [];

  return (
    <div className="space-y-6 p-6">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Brain className="w-6 h-6 text-purple-400" />
          <h1 className="text-xl font-semibold text-white">Learning Engine</h1>
        </div>
        {computedAt && (
          <p className="text-xs text-gray-500 font-mono">
            last run: {new Date(computedAt).toLocaleTimeString()}
          </p>
        )}
      </div>

      {/* Top stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatBox label="Trades Analysed"    value={tradeCount}        color="text-blue-400" />
        <StatBox label="Strategies Tracked" value={stratCount}        color="text-purple-400" />
        <StatBox label="Patterns Found"     value={patterns.length}   sub={`${strongCount} strong`} color="text-yellow-400" />
        <StatBox
          label="Calibration"
          value={calib && Object.keys(calib).length > 0 ? "Active" : loading ? "…" : "No data"}
          color={calib && Object.keys(calib).length > 0 ? "text-green-400" : "text-gray-500"}
        />
      </div>

      {/* Live effect — what the health/expectancy loop is CURRENTLY doing to
          trading, not just what it computed. Shows every tracked strategy;
          "throttled" ones (multiplier < 1.0) are called out up top since
          those are the ones actually being acted on right now. */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <div className="flex items-center gap-2 mb-1">
          <Zap className="w-4 h-4 text-blue-400" />
          <h2 className="text-sm font-semibold text-gray-200">Live Effect on Trading</h2>
          <span className="text-xs text-gray-500 ml-auto">
            {throttled.length > 0 ? `${throttled.length} strategy${throttled.length === 1 ? '' : 'ies'} throttled right now` : 'nothing throttled right now'}
          </span>
        </div>
        <p className="text-xs text-gray-500 mb-4">
          Position-size multiplier the CRM G_HEALTH gate is applying to new entries, per strategy — this is the loop's actual output, not its inputs.
        </p>
        {healthOverview && healthOverview.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left py-2 pr-4 font-medium">Strategy</th>
                  <th className="text-left py-2 px-3 font-medium">Tier</th>
                  <th className="text-right py-2 px-3 font-medium">Size Multiplier</th>
                  <th className="text-left py-2 pl-3 font-medium">Why</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/60">
                {healthOverview.map((r) => (
                  <tr key={r.strategy} className="hover:bg-[#0d1117]/60 transition-colors">
                    <td className="py-2 pr-4 font-mono text-gray-200">{r.strategy}</td>
                    <td className="py-2 px-3">
                      <span className={cn("text-xs px-2 py-0.5 rounded border font-mono", TIER_COLOR[r.tier] ?? TIER_COLOR.HEALTHY)}>
                        {r.tier}
                      </span>
                    </td>
                    <td className={cn(
                      "py-2 px-3 text-right font-mono font-bold",
                      r.sizeMultiplier >= 1.0 ? "text-green-400" : r.sizeMultiplier > 0 ? "text-orange-400" : "text-red-400"
                    )}>
                      {r.sizeMultiplier === 0 ? "BLOCKED" : `${(r.sizeMultiplier * 100).toFixed(0)}%`}
                    </td>
                    <td className="py-2 pl-3 text-gray-500">
                      {r.activeIncident ? r.activeIncident.message : r.sizeMultiplier >= 1.0 ? "—" : "recent losing trades (rolling window)"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-xs text-gray-500 text-center py-6">No strategy health data yet</p>
        )}
      </div>

      {/* Attribution + Calibration */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
          <div className="flex items-center gap-2 mb-4">
            <TrendingUp className="w-4 h-4 text-gray-400" />
            <h2 className="text-sm font-semibold text-gray-200">Win / Loss Attribution</h2>
          </div>
          <AttributionChart summaries={summaries} />
        </div>

        <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
          <div className="flex items-center gap-2 mb-4">
            <Zap className="w-4 h-4 text-gray-400" />
            <h2 className="text-sm font-semibold text-gray-200">LLM Calibration</h2>
            <span className="text-xs text-gray-500 ml-auto">predicted vs actual WR</span>
          </div>
          {calib
            ? <CalibrationChart data={calib} />
            : <p className="text-xs text-gray-500 text-center py-8">{loading ? "Loading…" : "No data"}</p>
          }
        </div>
      </div>

      {/* Pattern cards */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <div className="flex items-center gap-2 mb-4">
          <AlertTriangle className="w-4 h-4 text-yellow-400" />
          <h2 className="text-sm font-semibold text-gray-200">Detected Patterns</h2>
          <span className="text-xs text-gray-500 ml-auto">{patterns.length} total</span>
        </div>
        {patterns.length > 0 ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
            {patterns.map((p, i) => <PatternCard key={i} p={p} />)}
          </div>
        ) : (
          <p className="text-xs text-gray-500 text-center py-6">
            {loading ? "Loading patterns…" : "No patterns detected yet"}
          </p>
        )}
      </div>

      {/* Expectancy table */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <div className="flex items-center gap-2 mb-4">
          <CheckCircle className="w-4 h-4 text-green-400" />
          <h2 className="text-sm font-semibold text-gray-200">Strategy Expectancy</h2>
        </div>
        {expectancies.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left py-2 pr-4 font-medium">Strategy</th>
                  <th className="text-right py-2 px-3 font-medium">Expectancy</th>
                  <th className="text-right py-2 px-3 font-medium">Win Rate</th>
                  <th className="text-right py-2 px-3 font-medium">Avg Win</th>
                  <th className="text-right py-2 px-3 font-medium">Avg Loss</th>
                  <th className="text-right py-2 pl-3 font-medium">Trades</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/60">
                {expectancies.map(([strat, e]) => (
                  <tr key={strat} className="hover:bg-[#0d1117]/60 transition-colors">
                    <td className="py-2 pr-4 font-mono text-gray-200">{strat}</td>
                    <td className={cn(
                      "py-2 px-3 text-right font-mono font-bold",
                      e.expectancy >= 0 ? "text-green-400" : "text-red-400"
                    )}>
                      {e.expectancy >= 0 ? "+" : ""}{fmt(e.expectancy)}
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-gray-300">{pct(e.win_rate)}</td>
                    <td className="py-2 px-3 text-right font-mono text-green-400">+{fmt(e.avg_win)}</td>
                    <td className="py-2 px-3 text-right font-mono text-red-400">−{fmt(Math.abs(e.avg_loss))}</td>
                    <td className="py-2 pl-3 text-right font-mono text-gray-400">{e.total_trades}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-xs text-gray-500 text-center py-6">
            {loading ? "Loading expectancy…" : "No strategy data yet"}
          </p>
        )}
      </div>

      {/* Per-strategy attribution detail */}
      {Object.entries(summaries).length > 0 && (
        <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
          <div className="flex items-center gap-2 mb-4">
            <XCircle className="w-4 h-4 text-red-400" />
            <h2 className="text-sm font-semibold text-gray-200">Attribution Detail</h2>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
            {Object.entries(summaries).map(([strat, s]) => (
              <div key={strat} className="bg-[#0d1117] rounded-lg p-4 space-y-2">
                <p className="text-xs font-mono text-gray-200 truncate">{strat}</p>
                <div className="flex gap-3 text-xs">
                  <span className="text-green-400 font-mono">{s.wins}W</span>
                  <span className="text-red-400 font-mono">{s.losses}L</span>
                  <span className="text-gray-500">/ {s.total_trades} total</span>
                </div>
                {s.top_win_reason && (
                  <p className="text-xs text-gray-500">
                    <span className="text-green-400/70">↑ </span>{s.top_win_reason}
                  </p>
                )}
                {s.top_failure_reason && (
                  <p className="text-xs text-gray-500">
                    <span className="text-red-400/70">↓ </span>{s.top_failure_reason}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
