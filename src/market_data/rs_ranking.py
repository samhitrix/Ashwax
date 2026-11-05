"""
RS Ranking Engine â€” Mansfield Relative Strength per symbol.

Schedule: pre-market 08:30 + every 15 min intraday.

Computation per symbol (spec Â§8.2):
  Mansfield RS = (symbol 12-month return) / (NIFTY50 12-month return), normalized 0-100 percentile
  RS vs sector ETF
  RS line new highs (52-week check â†’ +0.10 LLM confidence boost signal)
  RS momentum = 4-week rate of change

Output:
  Top 20 RS leaders (priority watchlist)
  Top 20 RS improvers (biggest 4-week RS gain)
  Bottom 20% RS (blacklist for longs)

Writes atomically to logs/rs_ranking.json.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)
_IST = ZoneInfo("Asia/Kolkata")
_LOG_PATH = Path("logs/rs_ranking.json")
_TOP_N = 20
_BLACKLIST_PCT = 0.20


@dataclass
class RSRecord:
    symbol: str
    rs_score: float
    rs_percentile: float
    rs_momentum: float
    rs_new_high: bool
    llm_confidence_boost: float


@dataclass
class RSRankingResult:
    leaders: list[RSRecord]
    improvers: list[RSRecord]
    blacklist: list[RSRecord]
    computed_at: str


class RSRankingEngine:
    """
    Computes Mansfield RS rankings for a universe of symbols.

    Usage:
        engine = RSRankingEngine()
        result = engine.rank(prices_df, nifty)
        # prices_df: DataFrame, columns=symbol names, DatetimeIndex
        # nifty:     Series, NIFTY50 closing prices, same DatetimeIndex
    """

    def __init__(
        self,
        log_path: Path = _LOG_PATH,
        lookback_days: int = 252,
        top_n: int = 20,
        blacklist_bottom_pct: float = 20.0,
        min_rs_to_trade: float | None = None,
        blacklist_below: float | None = None,
    ) -> None:
        self._log_path = log_path
        self._lookback_days = lookback_days
        self._top_n = top_n
        self._blacklist_bottom_pct = blacklist_bottom_pct
        # universe.json rs_thresholds — percentile gates (fail-safe, narrowing only):
        # min_rs_to_trade drops weak symbols from the leaders set; blacklist_below
        # adds anything under the percentile to the blacklist (union, never un-blacklists).
        self._min_rs_to_trade = min_rs_to_trade
        self._blacklist_below = blacklist_below

    def rank(self, prices_df: pd.DataFrame, nifty: pd.Series) -> RSRankingResult:
        records = _compute_records(prices_df, nifty)
        if not records:
            logger.warning("rs_ranking: no symbols to rank")
            return RSRankingResult(
                leaders=[],
                improvers=[],
                blacklist=[],
                computed_at=datetime.now(_IST).isoformat(),
            )

        by_percentile = sorted(records, key=lambda r: r.rs_percentile, reverse=True)
        # self._top_n, NOT the module constant: both of these read the constant before,
        # so config/intelligence.json rs_ranking.top_n and blacklist_bottom_pct were
        # accepted by __init__ and then silently ignored -- the universe offered to the
        # swing LLM was pinned at 20 names out of ~500 and could not be widened by config.
        leaders = by_percentile[: (self._top_n or _TOP_N)]
        if self._min_rs_to_trade is not None:
            # Narrowing only: a leader must clear the tradable RS percentile.
            leaders = [r for r in leaders if r.rs_percentile >= self._min_rs_to_trade]
        # NOTE the units: self._blacklist_bottom_pct is a PERCENT (config passes 20),
        # _BLACKLIST_PCT is a FRACTION (0.20). Using the former raw would blacklist the
        # entire universe and silently stop all trading.
        _bl_frac = (
            (self._blacklist_bottom_pct / 100.0) if self._blacklist_bottom_pct else _BLACKLIST_PCT
        )
        blacklist_cutoff = max(1, int(len(by_percentile) * _bl_frac))
        blacklist = by_percentile[-blacklist_cutoff:]
        if self._blacklist_below is not None:
            # Union: never un-blacklists a symbol the bottom-pct rule already caught.
            below = [r for r in by_percentile if r.rs_percentile < self._blacklist_below]
            _seen = {r.symbol for r in blacklist}
            blacklist = blacklist + [r for r in below if r.symbol not in _seen]

        by_momentum = sorted(records, key=lambda r: r.rs_momentum, reverse=True)
        improvers = by_momentum[:_TOP_N]

        result = RSRankingResult(
            leaders=leaders,
            improvers=improvers,
            blacklist=blacklist,
            computed_at=datetime.now(_IST).isoformat(),
        )
        self._write(result)
        logger.info(
            "rs_ranking computed symbols=%d leaders=%d improvers=%d blacklist=%d",
            len(records),
            len(leaders),
            len(improvers),
            len(blacklist),
        )
        return result

    def _write(self, result: RSRankingResult) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "leaders": [asdict(r) for r in result.leaders],
            "improvers": [asdict(r) for r in result.improvers],
            "blacklist": [asdict(r) for r in result.blacklist],
            "computed_at": result.computed_at,
        }
        tmp = self._log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._log_path)


def _compute_records(prices_df: pd.DataFrame, nifty: pd.Series) -> list[RSRecord]:
    if nifty is None or len(nifty.dropna()) < 2:
        return []
    nifty_12m = _return_12m(nifty)

    rs_scores: dict[str, float] = {}
    for symbol in prices_df.columns:
        prices = prices_df[symbol].dropna()
        if len(prices) < 20:
            continue
        # Excess return (stock 12m return minus benchmark 12m return), not a
        # ratio. The prior ratio form (stock/nifty) blows up — and can even
        # flip SIGN relative to peers — whenever nifty_12m is small, scrambling
        # the whole day's cross-sectional ranking (observed: a +44% 12m winner
        # landing in the bottom 11th percentile because nifty_12m happened to
        # sit near zero that day). Excess return is monotonic and immune to
        # the zero-crossing; percentile rank only depends on relative order.
        rs_scores[symbol] = _return_12m(prices) - nifty_12m

    if not rs_scores:
        return []

    sorted_vals = sorted(rs_scores.values())
    records: list[RSRecord] = []
    for symbol, rs in rs_scores.items():
        prices = prices_df[symbol].dropna()
        percentile = _percentile_rank(rs, sorted_vals) * 100.0
        momentum = _rs_momentum(prices, nifty)
        rs_new_high = _is_rs_new_high(prices, nifty)
        records.append(
            RSRecord(
                symbol=symbol,
                rs_score=round(rs, 4),
                rs_percentile=round(percentile, 2),
                rs_momentum=round(momentum, 4),
                rs_new_high=rs_new_high,
                llm_confidence_boost=0.10 if rs_new_high else 0.0,
            )
        )

    return records


def _return_12m(prices: pd.Series) -> float:
    if len(prices) < 2:
        return 0.0
    start = float(prices.iloc[max(0, len(prices) - 252)])
    end = float(prices.iloc[-1])
    return (end - start) / start if start != 0 else 0.0


def _rs_momentum(prices: pd.Series, nifty: pd.Series) -> float:
    lookback = 20
    aligned, nifty_aligned = prices.align(nifty, join="inner")
    if len(aligned) < lookback + 1:
        return 0.0
    rs_now = _safe_ratio(float(aligned.iloc[-1]), float(nifty_aligned.iloc[-1]))
    rs_4w = _safe_ratio(float(aligned.iloc[-lookback]), float(nifty_aligned.iloc[-lookback]))
    return (rs_now - rs_4w) / rs_4w if rs_4w != 0 else 0.0


def _is_rs_new_high(prices: pd.Series, nifty: pd.Series) -> bool:
    aligned, nifty_aligned = prices.align(nifty, join="inner")
    if len(aligned) < 2:
        return False
    window = min(252, len(aligned))
    p_win = aligned.iloc[-window:]
    n_win = nifty_aligned.iloc[-window:]
    rs_series = (p_win / n_win.replace(0, float("nan"))).dropna()
    if rs_series.empty:
        return False
    return float(rs_series.iloc[-1]) >= float(rs_series.max())


def _safe_ratio(a: float, b: float) -> float:
    return a / b if b != 0 else 0.0


def _percentile_rank(value: float, sorted_vals: list[float]) -> float:
    count = sum(1 for v in sorted_vals if v <= value)
    return count / len(sorted_vals) if sorted_vals else 0.0
