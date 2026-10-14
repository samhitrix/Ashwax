import { useState } from 'react';
import { useApi } from '../hooks/useApi';
import { cn } from '../utils/cn';

interface OptionLeg {
  symbol: string;
  ltp: number;
  bid: number;
  ask: number;
  oi: number;
  volume: number;
  iv: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
}

interface StrikeRow {
  strike: number;
  call: OptionLeg | null;
  put: OptionLeg | null;
}

interface OptionChainResponse {
  symbol: string;
  spot: number | null;
  expiry: string | null;
  days_to_expiry: number | null;
  lot_size: number | null;
  strikes: StrikeRow[];
  error?: string;
}

const UNDERLYINGS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY'];

function fmtNum(v: number | null | undefined, digits = 2): string {
  return v == null ? '—' : v.toLocaleString('en-IN', { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

function fmtInt(v: number | null | undefined): string {
  return v == null ? '—' : v.toLocaleString('en-IN');
}

export function OptionChain() {
  const [underlying, setUnderlying] = useState('NIFTY');
  const [expiry, setExpiry] = useState('');

  // Re-queries on every filter change rather than filtering client-side —
  // the chain is live/expensive data (fresh quotes per strike), not a
  // single feed like PaperTrades' one-shot fetch.
  const url = `/api/option_chain?symbol=${underlying}${expiry ? `&expiry=${expiry}` : ''}`;
  const { data, loading, error } = useApi<OptionChainResponse>(url, 5000);

  const spot = data?.spot ?? null;
  const strikes = data?.strikes ?? [];
  const atmStrike =
    spot != null && strikes.length > 0
      ? strikes.reduce((best, s) => (Math.abs(s.strike - spot) < Math.abs(best.strike - spot) ? s : best)).strike
      : null;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-white">⛓️ Option Chain</h2>
        <p className="text-gray-400">Live strikes, OI/volume and Black-Scholes Greeks</p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="flex gap-2">
          {UNDERLYINGS.map((u) => (
            <button
              key={u}
              onClick={() => setUnderlying(u)}
              className={cn(
                'px-4 py-1.5 rounded-lg text-sm font-medium border transition-colors',
                underlying === u
                  ? 'bg-blue-500/20 border-blue-500/40 text-blue-300'
                  : 'bg-[#161b22] border-gray-800 text-gray-400 hover:text-gray-200'
              )}
            >
              {u}
            </button>
          ))}
        </div>
        <input
          type="date"
          value={expiry}
          onChange={(e) => setExpiry(e.target.value)}
          className="px-3 py-1.5 rounded-lg text-sm bg-[#161b22] border border-gray-800 text-gray-200"
        />
        {expiry && (
          <button onClick={() => setExpiry('')} className="text-xs text-gray-500 hover:text-gray-300">
            clear
          </button>
        )}
        {data?.expiry && (
          <span className="text-sm text-gray-400">
            Expiry: <span className="text-gray-200 font-mono">{data.expiry}</span>
            {data.days_to_expiry != null && ` (${data.days_to_expiry}d)`}
            {' · '}Spot: <span className="text-gray-200 font-mono">{fmtNum(spot)}</span>
            {data.lot_size != null && ` · Lot: ${data.lot_size}`}
          </span>
        )}
      </div>

      {loading && strikes.length === 0 && <p className="text-gray-400 text-sm py-8 text-center">Loading chain…</p>}
      {!loading && (error || data?.error) && <p className="text-red-400 text-sm py-8 text-center">{error || data?.error}</p>}
      {!loading && !error && !data?.error && strikes.length === 0 && (
        <p className="text-gray-400 text-sm py-8 text-center">No chain data for {underlying}</p>
      )}

      {strikes.length > 0 && (
        <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-400 text-left border-b border-gray-800">
                <th className="py-2 px-3 text-center" colSpan={7}>
                  Call
                </th>
                <th className="py-2 px-3 text-center">Strike</th>
                <th className="py-2 px-3 text-center" colSpan={7}>
                  Put
                </th>
              </tr>
              <tr className="text-gray-500 text-left border-b border-gray-800">
                <th className="py-2 px-3">OI</th>
                <th className="py-2 px-3">Vol</th>
                <th className="py-2 px-3">LTP</th>
                <th className="py-2 px-3">IV</th>
                <th className="py-2 px-3">Delta</th>
                <th className="py-2 px-3">Theta</th>
                <th className="py-2 px-3">Vega</th>
                <th className="py-2 px-3 text-center">—</th>
                <th className="py-2 px-3">LTP</th>
                <th className="py-2 px-3">IV</th>
                <th className="py-2 px-3">Delta</th>
                <th className="py-2 px-3">Theta</th>
                <th className="py-2 px-3">Vega</th>
                <th className="py-2 px-3">Vol</th>
                <th className="py-2 px-3">OI</th>
              </tr>
            </thead>
            <tbody>
              {strikes.map((row) => (
                <tr
                  key={row.strike}
                  className={cn('border-b border-gray-900 text-gray-200', row.strike === atmStrike && 'bg-blue-500/10')}
                >
                  <td className="py-2 px-3 font-mono">{fmtInt(row.call?.oi)}</td>
                  <td className="py-2 px-3 font-mono text-gray-400">{fmtInt(row.call?.volume)}</td>
                  <td className="py-2 px-3 font-mono">{fmtNum(row.call?.ltp)}</td>
                  <td className="py-2 px-3 font-mono text-gray-400">
                    {row.call?.iv != null ? `${(row.call.iv * 100).toFixed(1)}%` : '—'}
                  </td>
                  <td className="py-2 px-3 font-mono text-gray-400">{fmtNum(row.call?.delta, 3)}</td>
                  <td className="py-2 px-3 font-mono text-gray-400">{fmtNum(row.call?.theta, 2)}</td>
                  <td className="py-2 px-3 font-mono text-gray-400">{fmtNum(row.call?.vega, 2)}</td>
                  <td
                    className={cn(
                      'py-2 px-3 text-center font-bold',
                      row.strike === atmStrike ? 'text-blue-300' : 'text-white'
                    )}
                  >
                    {row.strike}
                  </td>
                  <td className="py-2 px-3 font-mono">{fmtNum(row.put?.ltp)}</td>
                  <td className="py-2 px-3 font-mono text-gray-400">
                    {row.put?.iv != null ? `${(row.put.iv * 100).toFixed(1)}%` : '—'}
                  </td>
                  <td className="py-2 px-3 font-mono text-gray-400">{fmtNum(row.put?.delta, 3)}</td>
                  <td className="py-2 px-3 font-mono text-gray-400">{fmtNum(row.put?.theta, 2)}</td>
                  <td className="py-2 px-3 font-mono text-gray-400">{fmtNum(row.put?.vega, 2)}</td>
                  <td className="py-2 px-3 font-mono text-gray-400">{fmtInt(row.put?.volume)}</td>
                  <td className="py-2 px-3 font-mono">{fmtInt(row.put?.oi)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
