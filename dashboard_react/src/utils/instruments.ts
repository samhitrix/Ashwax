/** True for an index/stock OPTION contract in a broker position row (NFO/BFO, ...CE/...PE).
 * Options live only in the Options tab (fed by /api/options_positions); equity views must
 * drop them -- otherwise live legs show twice and their P&L is double-counted on Overview. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function isOptionPosition(p: any): boolean {
  const exchange = String(p?.exchange ?? '').toUpperCase();
  const symbol = String(p?.tradingsymbol ?? '').toUpperCase();
  return (exchange === 'NFO' || exchange === 'BFO') && /(CE|PE)$/.test(symbol);
}
