import { useState } from 'react';
import { FileText, TrendingUp, AlertTriangle, DollarSign } from 'lucide-react';
import { useApi } from '../hooks/useApi';
import { cn } from '../utils/cn';
import { OptionsHistoryTab, PnlTab, TradebookTab, type ReportMode } from './ReportsHistory';

interface TaxLine { symbol: string; category: string; net_pnl: number; tax_liability: number; holding_days: number }
interface TaxReport {
  fy_label: string;
  stcg_gains: number; ltcg_gains: number; ltcg_gains_taxable: number;
  speculative_income: number;
  stcg_tax: number; ltcg_tax: number;
  total_tax_liability: number;
  trades: TaxLine[];
}

const CAT_COLOR: Record<string, string> = {
  STCG: 'text-orange-400 bg-orange-500/20',
  LTCG: 'text-green-400 bg-green-500/20',
  SPECULATIVE: 'text-blue-400 bg-blue-500/20',
};

function fmt(v: number) {
  return `₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}

// The pre-existing tax report stays first and is the default view; the history
// tabs are additions next to it.
const REPORT_TABS = [
  { key: 'tax', label: 'Tax (STCG / LTCG)' },
  { key: 'tradebook', label: 'Tradebook' },
  { key: 'pnl', label: 'P&L' },
  { key: 'options', label: 'Options' },
] as const;

export function Reports() {
  const [mode, setMode] = useState<ReportMode>('live');
  const [tab, setTab] = useState<(typeof REPORT_TABS)[number]['key']>('tax');
  const activeTab = tab;
  const tabs = REPORT_TABS;
  const pill = (on: boolean) =>
    cn(
      'px-4 py-1.5 rounded-lg text-sm font-medium border transition-colors',
      on ? 'bg-blue-500/20 border-blue-500/40 text-blue-300' : 'bg-[#161b22] border-gray-800 text-gray-400 hover:text-gray-200'
    );

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-white">Reports</h2>
        <p className="text-gray-400">Closed-trade history up to yesterday — Paper and Live are kept separate</p>
      </div>
      <div className="flex flex-wrap items-center gap-6">
        <div className="flex gap-2">
          {(['live', 'paper'] as const).map((m) => (
            <button key={m} onClick={() => setMode(m)} className={pill(mode === m)}>{m === 'live' ? 'Live' : 'Paper'}</button>
          ))}
        </div>
        <div className="flex gap-2 border-l border-gray-800 pl-6">
          {tabs.map((t) => (
            <button key={t.key} onClick={() => setTab(t.key)} className={pill(activeTab === t.key)}>{t.label}</button>
          ))}
        </div>
      </div>
      {activeTab === 'tradebook' && <TradebookTab mode={mode} />}
      {activeTab === 'pnl' && <PnlTab mode={mode} />}
      {activeTab === 'options' && <OptionsHistoryTab mode={mode} />}
      {activeTab === 'tax' && <TaxReportView mode={mode} />}
    </div>
  );
}

function TaxReportView({ mode }: { mode: ReportMode }) {
  const { data: tax } = useApi<TaxReport>(`/api/tax_report?mode=${mode}`, 60000);

  return (
    <div className="space-y-6">
      <p className="text-gray-400">Tax report — STCG / LTCG / Speculative · FY2025-26</p>

      {/* Tax summary cards */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg flex items-center justify-center bg-orange-500/20">
              <TrendingUp className="w-5 h-5 text-orange-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">STCG Gains</div>
              <div className="text-xl font-bold font-mono text-orange-400">{fmt(tax?.stcg_gains ?? 0)}</div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg flex items-center justify-center bg-green-500/20">
              <TrendingUp className="w-5 h-5 text-green-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">LTCG Gains</div>
              <div className="text-xl font-bold font-mono text-green-400">{fmt(tax?.ltcg_gains ?? 0)}</div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg flex items-center justify-center bg-blue-500/20">
              <DollarSign className="w-5 h-5 text-blue-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Speculative Income</div>
              <div className={cn('text-xl font-bold font-mono', (tax?.speculative_income ?? 0) >= 0 ? 'text-blue-400' : 'text-red-400')}>
                {fmt(tax?.speculative_income ?? 0)}
              </div>
            </div>
          </div>
        </div>

        <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg flex items-center justify-center bg-red-500/20">
              <AlertTriangle className="w-5 h-5 text-red-400" />
            </div>
            <div>
              <div className="text-xs text-gray-400">Total Tax Liability</div>
              <div className="text-xl font-bold font-mono text-red-400">{fmt(tax?.total_tax_liability ?? 0)}</div>
            </div>
          </div>
        </div>
      </div>

      {/* Category breakdown */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <h3 className="font-semibold text-white flex items-center gap-2 mb-4">
          <FileText className="w-5 h-5 text-blue-400" />
          Tax Category Breakdown
        </h3>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-400 border-b border-gray-800">
              <th className="pb-3 pr-4">Category</th>
              <th className="pb-3 pr-4 text-right">Gains / Income</th>
              <th className="pb-3 pr-4 text-right">Taxable Amount</th>
              <th className="pb-3 text-right">Tax @ Rate</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            <tr>
              <td className="py-3 pr-4">
                <span className="px-2 py-1 rounded text-xs font-semibold text-orange-400 bg-orange-500/20">STCG</span>
                <span className="text-gray-400 text-xs ml-2">20% flat · held &lt;12 months</span>
              </td>
              <td className="py-3 pr-4 text-right font-mono text-white">{fmt(tax?.stcg_gains ?? 0)}</td>
              <td className="py-3 pr-4 text-right font-mono text-white">{fmt(tax?.stcg_gains ?? 0)}</td>
              <td className="py-3 text-right font-mono text-orange-400">{fmt(tax?.stcg_tax ?? 0)}</td>
            </tr>
            <tr>
              <td className="py-3 pr-4">
                <span className="px-2 py-1 rounded text-xs font-semibold text-green-400 bg-green-500/20">LTCG</span>
                <span className="text-gray-400 text-xs ml-2">12.5% · above ₹1.25L exemption</span>
              </td>
              <td className="py-3 pr-4 text-right font-mono text-white">{fmt(tax?.ltcg_gains ?? 0)}</td>
              <td className="py-3 pr-4 text-right font-mono text-white">{fmt(tax?.ltcg_gains_taxable ?? 0)}</td>
              <td className="py-3 text-right font-mono text-green-400">{fmt(tax?.ltcg_tax ?? 0)}</td>
            </tr>
            <tr>
              <td className="py-3 pr-4">
                <span className="px-2 py-1 rounded text-xs font-semibold text-blue-400 bg-blue-500/20">SPECULATIVE</span>
                <span className="text-gray-400 text-xs ml-2">MIS intraday · added to total income</span>
              </td>
              <td className="py-3 pr-4 text-right font-mono text-white">{fmt(tax?.speculative_income ?? 0)}</td>
              <td className="py-3 pr-4 text-right font-mono text-gray-400">per slab</td>
              <td className="py-3 text-right font-mono text-gray-400">per slab</td>
            </tr>
          </tbody>
          <tfoot>
            <tr className="border-t border-gray-700">
              <td className="pt-3 pr-4 font-semibold text-white">Total</td>
              <td className="pt-3 pr-4" />
              <td className="pt-3 pr-4" />
              <td className="pt-3 text-right font-bold font-mono text-red-400">{fmt(tax?.total_tax_liability ?? 0)}</td>
            </tr>
          </tfoot>
        </table>
      </div>

      {/* Trade-level detail */}
      {(tax?.trades?.length ?? 0) > 0 && (
        <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
          <h3 className="font-semibold text-white flex items-center gap-2 mb-4">
            <FileText className="w-5 h-5 text-gray-400" />
            Trade Detail
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-400 border-b border-gray-800">
                  <th className="pb-3 pr-4">Symbol</th>
                  <th className="pb-3 pr-4">Category</th>
                  <th className="pb-3 pr-4 text-right">Holding Days</th>
                  <th className="pb-3 pr-4 text-right">Net P&L</th>
                  <th className="pb-3 text-right">Tax</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800">
                {tax!.trades.map((t, i) => (
                  <tr key={i}>
                    <td className="py-2 pr-4 font-mono text-white">{t.symbol.split(':').pop()}</td>
                    <td className="py-2 pr-4">
                      <span className={cn('px-2 py-0.5 rounded text-xs font-semibold', CAT_COLOR[t.category] ?? 'text-gray-400 bg-gray-500/20')}>
                        {t.category}
                      </span>
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-gray-300">{t.holding_days}d</td>
                    <td className={cn('py-2 pr-4 text-right font-mono', t.net_pnl >= 0 ? 'text-green-400' : 'text-red-400')}>
                      {t.net_pnl >= 0 ? '+' : ''}{fmt(t.net_pnl)}
                    </td>
                    <td className="py-2 text-right font-mono text-orange-400">{fmt(t.tax_liability)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <p className="text-xs text-gray-600 text-center">
        {tax?.fy_label ?? 'FY2025-26'} · TaxRuleFY2026v1 · All figures after transaction costs · Not financial advice
      </p>
    </div>
  );
}
