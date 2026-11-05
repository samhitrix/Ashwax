"""
USSCompiler — compiles USSSpec into a CompiledStrategy.

CompiledStrategy exposes:
  .evaluate_candle(df, idx)  → Signal | None  (live execution)
  .compute_sl(entry, df, idx) → float
  .compute_target(entry, sl)  → float
  .compute_trail(current_sl, entry, df, idx) → float
  .generate_backtest_signals(df) → list[BacktestSignal]

ConditionCompiler translates each USS Condition into a vectorized boolean
Series (backtest) and a scalar bool (live, at a specific row index).

Direction handling: a spec is LONG, SHORT, or BOTH. For BOTH (e.g. ORB) the
engine fires long breakouts AND mirrored short breakdowns from the same spec —
the conditions are authored long-side and the compiler mirrors the directional
operators for the short leg. SL / target / trail / exit math all flip per side.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import time as dtime
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..indicators.library import compute, range_pct
from .schema import (
    ConditionType,
    Direction,
    LogicOperator,
    SLMethod,
    TargetMethod,
    TrailingStop,
    TrailMethod,
    USSSpec,
)

_log = logging.getLogger(__name__)  # "src.strategies.uss.compiler" -> strategies.log

# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass
class Signal:
    strategy_name: str
    direction: Direction
    entry_price: float
    sl: float
    target: float
    confidence: float = 1.0


@dataclass
class BacktestSignal:
    bar_index: int
    date: Any  # pd.Timestamp
    entry_price: float
    sl: float
    target: float
    exit_price: float | None = None
    exit_bar: int | None = None
    pnl_r: float | None = None  # profit in R units
    direction: Direction = Direction.LONG
    exit_reason: str = ""  # Target / SL / Trail SL / Reversal / EOD / Time Stop / MA Close
    partial_r: float | None = None  # +R locked by the 50%-at-1R partial book (None if never booked)
    # Size-weighted R of each closed leg. A partial-booked position closes in TWO
    # legs (50% @ partial_r, then the remaining 50%) — counted as separate trades
    # by TradingView/broker statements. No partial → a single full-size leg.
    leg_pnls_r: list[float] | None = None


# ---------------------------------------------------------------------------
# Condition compiler
# ---------------------------------------------------------------------------


class ConditionCompiler:
    """Translates a USS Condition into a vectorized boolean Series.

    Pass short=True to mirror directional operators (price_above↔price_below,
    consolidation_high↔consolidation_low, crossover↔crossunder, …) so a
    long-authored condition produces the equivalent short-side signal.

    orb_high / orb_low (optional, per-row Series) supply the day's opening-range
    levels so PRICE conditions can reference `opening_range_high/low`.
    """

    def __init__(
        self,
        orb_high: pd.Series | None = None,
        orb_low: pd.Series | None = None,
        benchmark: pd.Series | None = None,
        rs_override: float | None = None,
        regime_long_ok: pd.Series | None = None,
    ) -> None:
        self._orb_high = orb_high
        self._orb_low = orb_low
        # Benchmark (index) close series — feeds the relative-strength RS rating.
        self._benchmark = benchmark
        # Self-regime gate (RegimeFilter), precomputed by CompiledStrategy —
        # consumed by the `pullback_trigger` PATTERN below.
        self._regime_long_ok = regime_long_ok
        # Output side-channel: the `pullback_trigger` pattern also tracks the
        # armed setup's low, which SLMethod.PULLBACK_LOW needs later. Threaded
        # the same way `_orb_range` is threaded into SL calc — computed once,
        # cached, read after compilation (see CompiledStrategy._ensure_compiled).
        self.last_pullback_low: pd.Series | None = None
        # Cross-sectional RS percentile (this symbol ranked against its peers,
        # e.g. from RSRankingEngine/rs_rankings DB table) for the CURRENT bar
        # only. When set, overrides RS_PERCENTILE's default self-referential
        # calc (rank within the symbol's OWN trailing history), which requires
        # a stock to already be near its own 1yr high before firing — well
        # after a base breakout has run, since a base is a pause below the
        # stock's own recent RS peak. See rs_percentile() docstring.
        self._rs_override = rs_override

    def compile_series(self, cond: Any, df: pd.DataFrame, short: bool = False) -> pd.Series:
        t = cond.type
        p = cond.params.get("params", cond.params)  # unwrap JSON-parser double-wrap

        if t == ConditionType.INDICATOR:
            return self._indicator_series(p, df, short)
        if t == ConditionType.PRICE:
            return self._price_series(p, df, short)
        if t == ConditionType.VOLUME:
            return self._volume_series(p, df)  # volume surge is direction-symmetric
        if t == ConditionType.PATTERN:
            return self._pattern_series(p, df)

        return pd.Series(True, index=df.index)

    def _indicator_series(
        self, p: dict[str, Any], df: pd.DataFrame, short: bool = False
    ) -> pd.Series:
        ind = (p.get("indicator") or p.get("name", "")).upper()
        period = int(p.get("period", 20))
        # JSON uses "operator"; "condition" is the canonical name — accept both
        condition = p.get("condition") or p.get("operator", "price_above")
        if short:
            condition = _MIRROR_OP.get(condition, condition)
        if ind == "RS_PERCENTILE":
            if self._rs_override is not None:
                values = pd.Series(self._rs_override, index=df.index)
            else:
                values = compute(df, ind, period=period, benchmark=self._benchmark)
        else:
            values = compute(df, ind, period=period)

        if condition == "price_above":
            return df["close"] > values
        if condition == "price_below":
            return df["close"] < values
        if condition == "near":
            pct = float(p.get("pct", 2.0))
            return (df["close"] >= values * (1 - pct / 100)) & (
                df["close"] <= values * (1 + pct / 100)
            )
        if condition == "value_above":
            return values > float(p.get("threshold", 50))
        if condition == "value_below":
            return values < float(p.get("threshold", 50))
        if condition == "value_between":
            return (values >= float(p.get("min", 30))) & (values <= float(p.get("max", 70)))
        if condition in ("indicator_above", "indicator_below"):
            # State (not event) comparison of two indicators, e.g. SMA(50) > SMA(200).
            other = compute(
                df,
                (p.get("other_indicator") or ind).upper(),
                period=int(p.get("other_period", period * 2)),
            )
            return values > other if condition == "indicator_above" else values < other
        if condition == "crossover":
            slow = compute(df, ind, period=int(p.get("slow_period", period * 2)))
            return (values > slow) & (values.shift(1) <= slow.shift(1))
        if condition == "crossunder":
            slow = compute(df, ind, period=int(p.get("slow_period", period * 2)))
            return (values < slow) & (values.shift(1) >= slow.shift(1))

        return pd.Series(False, index=df.index)

    def _price_series(self, p: dict[str, Any], df: pd.DataFrame, short: bool = False) -> pd.Series:
        condition = p.get("condition") or p.get("operator", "price_above")
        ref = p.get("reference", "")
        lookback = int(p.get("lookback_days", 21))

        # Opening-Range-Breakout: break of the day's session range (levels
        # supplied by CompiledStrategy). Long breaks above OR high; short mirror
        # breaks below OR low.
        if ref in ("opening_range_high", "opening_range_low") and self._orb_high is not None:
            orb_h, orb_l = self._orb_high, self._orb_low
            # FRESH breakout only: fire on the bar that FIRST closes outside the
            # opening range (prev close inside, this close outside). A plain level
            # check (close > orb_high) stays True for EVERY bar after the breakout,
            # so the live scan entered on whatever LATER bar it happened to look at
            # — price already ran, entry chased near the top ("late trade"). The
            # cross means we take the breakout bar itself or nothing (never chase).
            prev_close = df["close"].shift(1)
            up = (df["close"] > orb_h) & (prev_close <= orb_h)
            dn = (df["close"] < orb_l) & (prev_close >= orb_l)
            if ref == "opening_range_high":
                return dn if short else up
            return up if short else dn

        # Opening-range WIDTH gate (direction-neutral): the day's OR height as a
        # percent of price must sit inside [min_pct, max_pct]. Filters dead
        # (no-momentum) ranges and too-wide (uncontrollable-risk) ranges.
        if ref == "opening_range_width":
            if self._orb_high is None or self._orb_low is None:
                return pd.Series(False, index=df.index)
            width_pct = (self._orb_high - self._orb_low) / df["close"] * 100.0
            mask = pd.Series(True, index=df.index)
            if "min_pct" in p:
                mask &= width_pct >= float(p["min_pct"])
            if "max_pct" in p:
                mask &= width_pct <= float(p["max_pct"])
            return mask & width_pct.notna()

        # Close above the highest CLOSE of the prior `lookback_days` bars (long);
        # short mirror: close below the lowest prior close.
        if ref in ("closing_high", "closing_low"):
            hi = df["close"] > df["close"].shift(1).rolling(lookback).max()
            lo = df["close"] < df["close"].shift(1).rolling(lookback).min()
            return (lo if short else hi) if ref == "closing_high" else (hi if short else lo)

        breakout_up = df["close"] > df["high"].shift(1).rolling(lookback).max()
        breakdown_dn = df["close"] < df["low"].shift(1).rolling(lookback).min()

        if ref in ("consolidation_high",) or "consolidation_high" in condition:
            # long: break above range high; short mirror: break below range low
            return breakdown_dn if short else breakout_up
        if ref in ("consolidation_low",) or "consolidation_low" in condition:
            return breakout_up if short else breakdown_dn
        if "threshold" in p:
            val = float(p["threshold"])
            op = condition
            if short:
                op = _MIRROR_OP.get(op, op)
            return df["close"] > val if op == "price_above" else df["close"] < val

        return pd.Series(False, index=df.index)

    def _volume_series(self, p: dict[str, Any], df: pd.DataFrame) -> pd.Series:
        condition = p.get("condition") or p.get("operator", "volume_ratio_above")
        avg_days = int(p.get("average_days", p.get("avg_days", 20)))

        if condition == "volume_ratio_above":
            ratio = float(p.get("ratio", 1.5))
            avg_vol = df["volume"].rolling(avg_days).mean()
            return df["volume"] > avg_vol * ratio
        if condition == "volume_ratio_below":
            ratio = float(p.get("ratio", 0.8))
            avg_vol = df["volume"].rolling(avg_days).mean()
            return df["volume"] < avg_vol * ratio
        if condition == "volume_above":
            return df["volume"] > float(p.get("threshold", 0))
        if condition == "turnover_above":
            # Average daily traded value (close x volume) in rupee crores.
            turnover = (df["close"] * df["volume"]).rolling(avg_days).mean()
            return turnover >= float(p.get("threshold_crores", 0)) * 1e7

        return pd.Series(False, index=df.index)

    def _pattern_series(self, p: dict[str, Any], df: pd.DataFrame) -> pd.Series:
        import numpy as _np

        pattern = p.get("pattern", "")

        if pattern == "tight_range":
            max_range = float(p.get("max_range_pct", 8.0))
            lookback = int(p.get("lookback_days", 21))
            return range_pct(df, lookback) < max_range

        if pattern == "vcp_contraction":
            # Minervini VCP is SEQUENTIAL: a tight base on drying volume, THEN a
            # breakout. Detecting the contraction on the *breakout* bar is
            # impossible — the breakout expands the range and volume, ending the
            # contraction — so `vcp_contraction` and `breakout_above_pivot` never
            # co-occurred on the same bar and the AND was always empty (zero
            # signals). We instead detect a contraction in the recent base
            # *leading into* the current bar, which co-exists with a same-bar
            # breakout.
            p1 = int(p.get("period1", 60))
            p2 = int(p.get("period2", 30))
            p3 = int(p.get("period3", 15))
            base_lookback = int(p.get("base_lookback", 10))

            def _rng(n: int) -> pd.Series:
                hh = df["high"].rolling(n).max()
                ll = df["low"].rolling(n).min()
                return (hh - ll) / hh.replace(0, _np.nan) * 100

            c1, c2, c3 = _rng(p1), _rng(p2), _rng(p3)
            range_contracting = (c3 < c2) & (c2 < c1)
            # Volume dries up across the base: the recent (p3) average sits below
            # the full-base (p1) average. Strict monotone v3<v2<v1 was too brittle
            # and seldom co-occurred with the tightening range.
            v1 = df["volume"].rolling(p1).mean()
            v3 = df["volume"].rolling(p3).mean()
            volume_contracting = v3 < v1
            contracted = range_contracting & volume_contracting
            # True when a VCP base contracted within the prior `base_lookback`
            # bars; shift(1) excludes the current (breakout) bar.
            return contracted.shift(1).rolling(base_lookback).max().fillna(0).astype(bool)

        if pattern == "wyckoff_spring":
            # Wyckoff spring: price undercuts the established range low on
            # BELOW-average volume (a shakeout, not real supply), then reclaims
            # that low. True from the reclaim bar through the rest of the
            # spring_window so it co-exists with a same-bar wyckoff_lps trigger
            # under AND, the same trailing-window shape vcp_contraction uses.
            range_lookback = int(p.get("range_lookback", 40))
            spring_window = int(p.get("spring_window", 10))
            undercut_pct = float(p.get("undercut_pct", 1.0))
            max_volume_ratio = float(p.get("max_volume_ratio", 0.8))
            avg_days = int(p.get("avg_days", 20))

            range_low = df["low"].rolling(range_lookback).min().shift(spring_window)
            avg_vol = df["volume"].rolling(avg_days).mean()
            undercut_level = range_low * (1 - undercut_pct / 100)
            is_undercut_bar = (df["low"] < undercut_level) & (
                df["volume"] < avg_vol * max_volume_ratio
            )
            spring_occurred = is_undercut_bar.rolling(spring_window).max().fillna(0).astype(bool)
            reclaimed = df["close"] > range_low
            return spring_occurred & reclaimed

        if pattern == "wyckoff_lps":
            # Last Point of Support: after a confirmed SOS breakout (close cleared
            # the pre-pullback range high by breakout_min_pct within the recent
            # window), the CURRENT bar is a quiet pullback — close sits between
            # the old resistance and max_pullback_pct above it, on below-average
            # volume. This is the entry trigger bar itself (unlike wyckoff_spring's
            # wider trailing-window shape).
            range_lookback = int(p.get("range_lookback", 40))
            breakout_lookback = int(p.get("breakout_lookback", 15))
            pullback_window = int(p.get("pullback_window", 8))
            breakout_min_pct = float(p.get("breakout_min_pct", 2.0))
            max_pullback_pct = float(p.get("max_pullback_pct", 5.0))
            max_volume_ratio_lps = float(p.get("max_volume_ratio", 0.9))
            avg_days_lps = int(p.get("avg_days", 20))

            resistance = (
                df["high"].rolling(range_lookback).max().shift(pullback_window + breakout_lookback)
            )
            recent_close_max = df["close"].rolling(breakout_lookback).max().shift(pullback_window)
            had_sos = recent_close_max > resistance * (1 + breakout_min_pct / 100)

            avg_vol_lps = df["volume"].rolling(avg_days_lps).mean()
            close = df["close"]
            near_resistance = (close >= resistance) & (
                close <= resistance * (1 + max_pullback_pct / 100)
            )
            quiet = df["volume"] < avg_vol_lps * max_volume_ratio_lps
            return had_sos.fillna(False) & near_resistance & quiet

        if pattern == "tight_days_pullback":
            # Buy the CONFIRMATION out of a post-breakout pause, not the raw
            # breakout candle (Minervini's "3 Tight Days" secondary entry): after
            # price first closes above the pivot, wait for `tight_days`
            # consecutive low-range/low-volume days, then fire on the bar that
            # closes above THAT pause's own high. Anchors the eventual stop to
            # the pause's low (a few % away) instead of the whole base's low
            # (often 10-20% away) — pair with stop_loss.method=recent_low using
            # a matching lookback_days.
            pivot_lookback = int(p.get("pivot_lookback_days", 15))
            post_breakout_days = int(p.get("post_breakout_days", 10))
            tight_days = int(p.get("tight_days", 3))
            max_daily_range_pct = float(p.get("max_daily_range_pct", 3.5))
            volume_avg_days = int(p.get("volume_avg_days", 20))
            volume_dryup_ratio = float(p.get("volume_dryup_ratio", 0.8))

            breakout_up = df["close"] > df["high"].shift(1).rolling(pivot_lookback).max()
            had_breakout_recently = (
                breakout_up.shift(1).rolling(post_breakout_days).max().fillna(0).astype(bool)
            )

            daily_range_pct = (df["high"] - df["low"]) / df["low"].replace(0, _np.nan) * 100
            vol_avg = df["volume"].rolling(volume_avg_days).mean()
            pause_day = (daily_range_pct <= max_daily_range_pct) & (
                df["volume"] < vol_avg * volume_dryup_ratio
            )
            # All of the tight_days bars immediately before the current one (not
            # including it) must be pause days — sum of a boolean rolling window
            # equalling tight_days means every bar in it was True.
            pause_cluster = pause_day.shift(1).rolling(tight_days).sum().fillna(0) == tight_days
            cluster_high = df["high"].shift(1).rolling(tight_days).max()

            return (df["close"] > cluster_high) & pause_cluster & had_breakout_recently

        if pattern == "breakout_or_pullback":
            # Union of a raw pivot breakout (fires often, catches every fresh
            # high) and tight_days_pullback's stricter post-breakout "3 Tight
            # Days" continuation (fires rarely, catches higher-quality re-entries
            # a raw breakout alone would miss whenever price pauses below its
            # prior pivot before resuming). Same params as tight_days_pullback;
            # trades under whichever leg fires on a given bar.
            pivot_lookback = int(p.get("pivot_lookback_days", 15))
            post_breakout_days = int(p.get("post_breakout_days", 10))
            tight_days = int(p.get("tight_days", 3))
            max_daily_range_pct = float(p.get("max_daily_range_pct", 3.5))
            volume_avg_days = int(p.get("volume_avg_days", 20))
            volume_dryup_ratio = float(p.get("volume_dryup_ratio", 0.8))

            breakout_up = df["close"] > df["high"].shift(1).rolling(pivot_lookback).max()
            had_breakout_recently = (
                breakout_up.shift(1).rolling(post_breakout_days).max().fillna(0).astype(bool)
            )

            daily_range_pct = (df["high"] - df["low"]) / df["low"].replace(0, _np.nan) * 100
            vol_avg = df["volume"].rolling(volume_avg_days).mean()
            pause_day = (daily_range_pct <= max_daily_range_pct) & (
                df["volume"] < vol_avg * volume_dryup_ratio
            )
            pause_cluster = pause_day.shift(1).rolling(tight_days).sum().fillna(0) == tight_days
            cluster_high = df["high"].shift(1).rolling(tight_days).max()
            pullback_confirmed = (
                (df["close"] > cluster_high) & pause_cluster & had_breakout_recently
            )

            return breakout_up | pullback_confirmed

        if pattern == "sma_high_cross":
            # FRESH cross only (see the ORB fresh-cross comment above): fires on
            # the bar that first closes above the SMA-of-highs band, not on
            # every later bar that stays above it.
            period = int(p.get("period", 445))
            sma_high = df["high"].rolling(period).mean()
            return (df["close"] > sma_high) & (df["close"].shift(1) <= sma_high.shift(1))

        if pattern == "pullback_trigger":
            # Regime Pullback Swing (spec §3-4): arm on the first close back
            # under a short SMA after a qualifying streak above it; track the
            # pullback's low while armed; cancel on regime loss / excess depth
            # / max duration; fire on a CLOSE-confirmed break of the trailing
            # trigger-window high. True bar-by-bar state (unlike the bounded
            # rolling-window patterns above) because arm/cancel/trigger genuinely
            # depend on an evolving in-flight setup, not a fixed lookback window.
            #
            # Deliberate adaptations from the Pine source:
            #  - Pine's `flat`/`attemptsL` arm-gates are enforced outside this
            #    function (one-trade-at-a-time is already the engine's rule; the
            #    per-regime-phase attempt limiter is a CRM gate, not a compiler
            #    concern) — matches how every entry condition in this engine is
            #    position-agnostic.
            #  - Pine's trigger is a resting intrabar buy-stop (`ta.highest`
            #    includes the current bar; fills whenever a later bar's HIGH
            #    crosses it). This engine only ever confirms entries on CLOSE
            #    (see the ORB fresh-cross comment above) to avoid chasing an
            #    intrabar level — so the trigger here is close > (trailing
            #    trigger_window bars' high, EXCLUDING the current bar) × buffer.
            pull_period = int(p.get("pullback_sma_period", 20))
            min_bars_above = int(p.get("min_bars_above", 5))
            atr_period = int(p.get("atr_period", 20))
            depth_mult = float(p.get("depth_atr_mult", 2.5))
            min_dur = int(p.get("min_duration", 3))
            max_dur = int(p.get("max_duration", 15))
            trig_win = int(p.get("trigger_window", 3))
            trig_buf_pct = float(p.get("trigger_buffer_pct", 0.05))

            sma_pul = compute(df, "SMA", period=pull_period).to_numpy(dtype=float)
            atr_arr = compute(df, "ATR", period=atr_period).to_numpy(dtype=float)
            close = df["close"].to_numpy(dtype=float)
            high = df["high"].to_numpy(dtype=float)
            low = df["low"].to_numpy(dtype=float)
            n = len(df)
            if self._regime_long_ok is not None:
                regime_ok = (
                    self._regime_long_ok.reindex(df.index).fillna(False).to_numpy(dtype=bool)
                )
            else:
                regime_ok = _np.ones(n, dtype=bool)

            trigger = _np.zeros(n, dtype=bool)
            pb_low_out = _np.full(n, _np.nan)

            above_cnt = 0
            prev_above_cnt = 0
            run_high = _np.nan
            armed = False
            pb_low = swing_hi = atr_arm = _np.nan
            pb_start = 0

            for i in range(n):
                smap, atrv = sma_pul[i], atr_arr[i]
                if _np.isnan(smap) or _np.isnan(atrv):
                    prev_above_cnt = above_cnt
                    continue
                c, h, lo = close[i], high[i], low[i]
                reg_ok_i = bool(regime_ok[i])

                if c > smap:
                    above_cnt += 1
                    run_high = h if above_cnt == 1 else max(run_high, h)
                else:
                    above_cnt = 0

                if reg_ok_i and not armed and c < smap and prev_above_cnt >= min_bars_above:
                    armed = True
                    pb_low = lo
                    swing_hi = run_high
                    atr_arm = atrv
                    pb_start = i

                if armed:
                    pb_low = min(pb_low, lo)
                    dur = i - pb_start
                    cancel = (
                        (not reg_ok_i)
                        or (swing_hi - pb_low) > depth_mult * atr_arm
                        or dur > max_dur
                    )
                    if cancel:
                        armed = False

                if armed:
                    dur = i - pb_start
                    if dur >= min_dur and i - trig_win >= 0:
                        trig_hh = float(high[i - trig_win : i].max())
                        trig_level = trig_hh * (1 + trig_buf_pct / 100)
                        if c > trig_level:
                            trigger[i] = True
                            pb_low_out[i] = pb_low
                            armed = False

                prev_above_cnt = above_cnt

            self.last_pullback_low = pd.Series(pb_low_out, index=df.index)
            return pd.Series(trigger, index=df.index)

        return pd.Series(False, index=df.index)


# Strategies already warned about a missing breadth context (log once, not per symbol).
_BREADTH_WARNED: set[str] = set()

# Directional operator mirrors for the short leg of a BOTH/SHORT strategy.
_MIRROR_OP: dict[str, str] = {
    "price_above": "price_below",
    "price_below": "price_above",
    "value_above": "value_below",
    "value_below": "value_above",
    "crossover": "crossunder",
    "crossunder": "crossover",
    "indicator_above": "indicator_below",
    "indicator_below": "indicator_above",
}


# ---------------------------------------------------------------------------
# Intraday session (ORB) helpers
# ---------------------------------------------------------------------------


def _parse_window(w: str) -> tuple[dtime, dtime]:
    """'0915-0920' → (time(9,15), time(9,20))."""
    a, b = w.split("-")
    return dtime(int(a[:2]), int(a[2:])), dtime(int(b[:2]), int(b[2:]))


def compute_session_levels(
    df: pd.DataFrame, session: Any
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Return (orb_high, orb_low, in_entry_window, in_exit_window) aligned to df.

    orb_high/low are the day's opening-range high/low (from range_window bars),
    broadcast to every row of that day. Requires a DatetimeIndex; otherwise the
    levels are NaN and the windows permissive (so non-intraday data is unaffected).
    """
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        nan = pd.Series(np.nan, index=df.index)
        return nan, nan, pd.Series(True, index=df.index), pd.Series(False, index=df.index)

    local = idx
    try:
        if idx.tz is not None:
            local = idx.tz_convert(session.timezone)
    except Exception:
        local = idx

    times = pd.Series(local.time, index=df.index)
    dates = pd.Series(local.date, index=df.index)
    rs, re = _parse_window(session.range_window)
    # Entry start is ALWAYS the moment the range closes — derived from range_window,
    # never read from entry_window. A separately-specified start (e.g. "0925") can
    # drift out of sync with range_window and silently exclude the first breakout
    # candle (observed 2026-07-30: config said "0925" while the range ended "0920",
    # excluding the 09:20-09:25 candle where the cleanest breakouts happen).
    _, ee = _parse_window(session.entry_window)
    es = re
    xs, xe = _parse_window(session.exit_window)

    in_range = times.map(lambda x: rs <= x <= re)
    in_entry = times.map(lambda x: es <= x <= ee)
    in_exit = times.map(lambda x: xs <= x <= xe)

    # Per-day opening range from range-window bars only, broadcast to all rows.
    rh = df["high"].where(in_range)
    rl = df["low"].where(in_range)
    orb_high = rh.groupby(dates).transform("max")
    orb_low = rl.groupby(dates).transform("min")
    return orb_high, orb_low, in_entry, in_exit


# ---------------------------------------------------------------------------
# SL / Target / Trail helpers  (direction-aware)
# ---------------------------------------------------------------------------

# Trailing stops only engage once the trade is in profit by this many R.
# Until then the structural (initial) stop holds, so a fresh breakout is given
# room to work instead of being shaken out on the first routine pullback.
_TRAIL_ACTIVATION_R = 1.0


def _compute_sl_value(
    sl: Any,
    entry: float,
    df: pd.DataFrame,
    idx: int,
    direction: Direction = Direction.LONG,
    session_range: float | None = None,
    pullback_low: float | None = None,
) -> float:
    is_short = direction == Direction.SHORT
    max_sl_pct = float(sl.max_sl_pct or 8.0)
    # Cap: SL can never sit more than max_sl_pct away from entry.
    # Long → cap below entry (floor); Short → cap above entry (ceiling).
    sl_cap = entry * (1 + max_sl_pct / 100) if is_short else entry * (1 - max_sl_pct / 100)
    min_sl_pct = float(getattr(sl, "min_sl_pct", 0.0) or 0.0)
    # Floor: SL can never sit closer than min_sl_pct to entry — guards structural
    # methods (e.g. entry_candle) against a near-zero stop where gap-slippage and
    # charges alone would exceed the intended risk.
    sl_floor = entry * (1 + min_sl_pct / 100) if is_short else entry * (1 - min_sl_pct / 100)

    def _clamp(raw: float) -> float:
        capped = min(raw, sl_cap) if is_short else max(raw, sl_cap)
        if min_sl_pct <= 0:
            return capped
        return max(capped, sl_floor) if is_short else min(capped, sl_floor)

    if sl.method == SLMethod.ORB_RANGE:
        frac = float(sl.range_fraction or 0.5)
        rng = (
            session_range if (session_range is not None and session_range == session_range) else 0.0
        )
        raw = entry + rng * frac if is_short else entry - rng * frac
        return _clamp(raw)

    if sl.method == SLMethod.PULLBACK_LOW:
        # Structural stop below the exact low tracked by a `pullback_trigger`
        # PATTERN condition while armed (Pine: pbLowL - stopBufMlt*atrV).
        # Falls back to the generic cap if no pullback-low was threaded
        # through (e.g. spec misconfigured — validator should have caught it).
        if pullback_low is None or pullback_low != pullback_low:  # NaN-safe
            return sl_cap
        period = int(sl.atr_period or 14)
        mult = float(sl.atr_multiplier or 0.5)
        atr_val = float(compute(df, "ATR", period=period).iloc[idx])
        raw = pullback_low + atr_val * mult if is_short else pullback_low - atr_val * mult
        return _clamp(raw)

    if sl.method == SLMethod.ENTRY_CANDLE:
        # Structural stop at the entry (breakout) candle's own low/high — unlike
        # RECENT_LOW/HIGH below, this reads df[idx] itself, not a lookback window
        # ending BEFORE it. Doesn't drift with fill slippage the way an
        # entry-relative distance (e.g. orb_range) does — a candle-low SL stays
        # put regardless of where within that candle the order actually filled.
        buf = float(sl.buffer_pct) / 100
        if is_short:
            ref_high = float(df["high"].iloc[idx])
            return _clamp(ref_high * (1 + buf))
        ref_low = float(df["low"].iloc[idx])
        return _clamp(ref_low * (1 - buf))

    if sl.method in (SLMethod.PATTERN_LEVEL, SLMethod.RECENT_LOW, SLMethod.RECENT_HIGH):
        default_lb = 21 if sl.method == SLMethod.PATTERN_LEVEL else 10
        lookback = sl.lookback_days or default_lb
        start = max(0, idx - lookback)
        buf = float(sl.buffer_pct) / 100
        if is_short:
            ref_high = float(df["high"].iloc[start:idx].max())
            return _clamp(ref_high * (1 + buf))
        ref_low = float(df["low"].iloc[start:idx].min())
        return _clamp(ref_low * (1 - buf))

    if sl.method == SLMethod.ATR_BASED:
        period = int(sl.atr_period or 14)
        mult = float(sl.atr_multiplier or 2.0)
        atr_val = float(compute(df, "ATR", period=period).iloc[idx])
        raw = entry + atr_val * mult if is_short else entry - atr_val * mult
        return _clamp(raw)

    if sl.method == SLMethod.FIXED_PCT:
        pct = float(sl.fixed_pct or sl.max_sl_pct)
        return entry * (1 + pct / 100) if is_short else entry * (1 - pct / 100)

    if sl.method == SLMethod.FIXED_POINTS:
        pts = float(sl.fixed_points or 0)
        return entry + pts if is_short else entry - pts

    return sl_cap


def _compute_target_value(
    target_spec: Any, entry: float, sl: float, direction: Direction = Direction.LONG
) -> float:
    is_short = direction == Direction.SHORT
    risk = abs(entry - sl)

    if target_spec.method == TargetMethod.RISK_REWARD:
        ratio = target_spec.ratio or 2.0
        return entry - risk * ratio if is_short else entry + risk * ratio

    if target_spec.method == TargetMethod.FIXED_PCT:
        pct = (target_spec.fixed_pct or 5.0) / 100
        return entry * (1 - pct) if is_short else entry * (1 + pct)

    if target_spec.method == TargetMethod.FIXED_POINTS:
        pts = float(target_spec.fixed_points or 0)
        return entry - pts if is_short else entry + pts

    return entry - risk * 2.0 if is_short else entry + risk * 2.0


def _compute_trail_value(
    trail: Any,
    current_sl: float,
    entry: float,
    df: pd.DataFrame,
    idx: int,
    entry_bar: int = 0,
    direction: Direction = Direction.LONG,
) -> float:
    is_short = direction == Direction.SHORT

    def _tighten(candidate: float) -> float:
        # Stops only ever ratchet toward price: down for long, up for short.
        return min(current_sl, candidate) if is_short else max(current_sl, candidate)

    if trail.method == TrailMethod.INDICATOR_CROSS:
        ind = (trail.indicator or "EMA").upper()
        period = trail.period or 10
        val = float(compute(df, ind, period=period).iloc[idx])
        # Stop rides at the moving-average line (the classic MA trail). The
        # earlier 0.1% (val*0.999) buffer hugged a fast EMA and was pierced by
        # routine intraday noise on the very next bar.
        return _tighten(val)

    if trail.method == TrailMethod.ATR_TRAIL:
        period = trail.atr_period or 14
        mult = trail.atr_multiplier or 2.0
        atr_val = float(compute(df, "ATR", period=period).iloc[idx])
        # ORB.md Pine trails on CLOSE (`close ± atr*mult`), not the running peak.
        # A close-based stop sits looser than a high-based one, so a strong trend
        # isn't cut short by a single intrabar wick. _tighten ratchets it one way.
        # (CHANDELIER below remains the highest-high/lowest-low variant.)
        close = float(df["close"].iloc[idx])
        if is_short:
            return _tighten(close + atr_val * mult)
        return _tighten(close - atr_val * mult)

    if trail.method == TrailMethod.CHANDELIER:
        period = trail.chandelier_period or 22
        mult = trail.chandelier_multiplier or 3.0
        atr_val = float(compute(df, "ATR", period=period).iloc[idx])
        start = max(entry_bar, idx - period + 1)
        if is_short:
            lowest = float(df["low"].iloc[start : idx + 1].min())
            return _tighten(lowest + atr_val * mult)
        highest = float(df["high"].iloc[start : idx + 1].max())
        return _tighten(highest - atr_val * mult)

    if trail.method == TrailMethod.SMA_BAND:
        # Trail at a long-period SMA of the OPPOSITE side of the candle (SMA of
        # lows for a long, SMA of highs for a short) rather than SMA of close —
        # a slow-moving band exit: exit when close crosses below (long) / above
        # (short) that band, not a fixed R multiple off entry.
        period = trail.period or 445
        if is_short:
            val = float(df["high"].rolling(period).mean().iloc[idx])
            return _tighten(val)
        val = float(df["low"].rolling(period).mean().iloc[idx])
        return _tighten(val)

    if trail.method == TrailMethod.FIXED_PCT:
        pct = (trail.fixed_pct or 5.0) / 100
        if is_short:
            trough = float(df["low"].iloc[entry_bar : idx + 1].min())
            return _tighten(trough * (1 + pct))
        peak = float(df["high"].iloc[entry_bar : idx + 1].max())
        return _tighten(peak * (1 - pct))

    return current_sl


def _compute_self_regime(rf: Any, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """(long_ok, short_ok) from RegimeFilter — the traded symbol's own price vs
    its own SMA, with a slope requirement. Shared by CompiledStrategy (backtest
    entries) and SmartExitTracker (live regime-flip exit) so both read the
    identical definition."""
    sma = compute(df, "SMA", period=rf.sma_period)
    sma_prior = sma.shift(rf.slope_lookback_bars)
    valid = sma.notna() & sma_prior.notna()
    rising = (sma > sma_prior) & valid
    falling = (sma < sma_prior) & valid
    long_ok = (df["close"] > sma) & rising & valid
    short_ok = (df["close"] < sma) & falling & valid
    return long_ok, short_ok


# ---------------------------------------------------------------------------
# Smart-exit stepper — SINGLE SOURCE OF TRUTH for both backtest and live.
# `generate_backtest_signals` and the live `SmartExitTracker` both call
# `_smart_exit_step`, so a live ORB position rides the same partial / breakeven /
# ATR-trail / reversal logic the backtest simulates (matches ORB.md Pine).
# ---------------------------------------------------------------------------


@dataclass
class _ExitState:
    entry: float
    sl: float
    target: float
    initial_risk: float
    is_short: bool
    partial_r: float | None
    be_r: float | None
    reversal_on: bool
    activation_r: float
    trail: Any
    time_stop_before_activation: bool = False
    regime_flip_on: bool = False
    partial_booked: bool = False
    be_moved: bool = False
    peak: float = 0.0  # running max high since entry (long run-up)
    trough: float = 0.0  # running min low since entry (short run-up)
    giveback: Any = None  # GivebackLock | None
    time_decay: Any = ()  # sequence of TimeDecayStep
    tz: str | None = None  # exchange timezone for time_decay clock reads


@dataclass
class _BarExit:
    exit: bool = False
    exit_price: float = 0.0
    reason: str = ""
    sl_changed: bool = False  # SL moved this bar (breakeven or trail) → re-arm stop
    booked_partial: bool = False  # 50% partial booked this bar


_GLOBAL_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "global.json"


@lru_cache(maxsize=1)
def _exit_defaults_cfg() -> dict[str, Any]:
    """`exit_defaults` block of config/global.json ({} when absent/unreadable)."""
    try:
        return dict(
            json.loads(_GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")).get("exit_defaults", {})
        )
    except Exception:
        return {}


def effective_exit_conditions(spec: Any) -> Any:
    """spec.exit_conditions, or — when the spec defines NO breakeven/partial/trail
    at all — those exit_conditions plus the global `exit_defaults` profile (swing vs
    intraday). A spec that sets any of the three has made a deliberate exit design
    (e.g. breakeven+trail with no partial) and is left untouched. The single source
    for tracker, live checks and backtest so they cannot disagree."""
    ec = spec.exit_conditions
    cfg = _exit_defaults_cfg()
    if not cfg.get("enabled", False):
        return ec
    if (
        ec.breakeven_at_r is not None
        or ec.partial_book_r is not None
        or ec.trailing_stop is not None
    ):
        return ec
    profile = cfg.get("intraday" if spec.intraday is not None else "swing") or {}
    update: dict[str, Any] = {
        k: float(profile[k])
        for k in ("breakeven_at_r", "partial_book_r", "trail_activation_r")
        if profile.get(k) is not None and getattr(ec, k) is None
    }
    if profile.get("trailing_stop"):
        update["trailing_stop"] = TrailingStop(**profile["trailing_stop"])
    return ec.model_copy(update=update) if update else ec


def _make_exit_state(
    spec: Any,
    entry: float,
    sl: float,
    target: float,
    direction: Direction,
    df: pd.DataFrame | None = None,
    idx: int | None = None,
) -> _ExitState:
    """Build the per-trade exit state from a spec's exit_conditions. When df/idx are
    given (backtest), seed peak/trough from the entry bar's high/low to match the
    backtest's slice-based run-up; otherwise (live) seed from the entry price."""
    ec = effective_exit_conditions(spec)
    is_intraday = spec.intraday is not None
    activation_r = (
        ec.trail_activation_r
        if ec.trail_activation_r is not None
        else (1.5 if is_intraday else _TRAIL_ACTIVATION_R)
    )
    if df is not None and idx is not None:
        peak = float(df["high"].iloc[idx])
        trough = float(df["low"].iloc[idx])
    else:
        peak = trough = entry
    return _ExitState(
        entry=entry,
        sl=sl,
        target=target,
        initial_risk=abs(entry - sl),
        is_short=(direction == Direction.SHORT),
        partial_r=ec.partial_book_r,
        be_r=ec.breakeven_at_r,
        reversal_on=bool(ec.reversal_exit),
        activation_r=activation_r,
        trail=ec.trailing_stop,
        time_stop_before_activation=bool(ec.time_stop_before_activation),
        regime_flip_on=bool(ec.regime_flip_exit),
        peak=peak,
        trough=trough,
        giveback=getattr(ec, "giveback_lock", None),
        time_decay=tuple(getattr(ec, "time_decay", None) or ()),
        tz=(spec.intraday.timezone if is_intraday else None),
    )


def _bar_clock_minutes(df: pd.DataFrame, i: int, tz: str | None) -> int | None:
    """Minutes-since-midnight of bar i in the exchange timezone (None when the index
    carries no time). Naive indexes are taken as already exchange-local, matching
    compute_session_levels."""
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        return None
    ts = idx[i]
    if ts.tzinfo is not None and tz:
        try:
            ts = ts.tz_convert(tz)
        except Exception:
            pass
    return int(ts.hour) * 60 + int(ts.minute)


def _protective_stops(state: _ExitState, prior_mfe_r: float, clock_min: int | None) -> list[float]:
    """Candidate stops from the giveback lock and session-clock decay. Uses MFE up to
    the PREVIOUS bar so a stop raised on this bar's extreme is never also 'hit' by the
    same bar (intrabar order unknown) — one bar of lag, never optimistic."""
    risk = state.initial_risk
    if risk <= 0:
        return []
    sign = -1.0 if state.is_short else 1.0
    out: list[float] = []
    gb = state.giveback
    if gb is not None and prior_mfe_r >= gb.activate_mfe_r:
        out.append(state.entry + sign * gb.lock_fraction * prior_mfe_r * risk)
    if clock_min is not None:
        for step in state.time_decay:
            hh, mm = (int(x) for x in step.at.split(":"))
            if clock_min < hh * 60 + mm:
                continue
            if step.min_mfe_r is not None and prior_mfe_r < step.min_mfe_r:
                continue
            out.append(state.entry + sign * step.lock_r * risk)
    return out


def _smart_exit_step(
    state: _ExitState,
    df: pd.DataFrame,
    i: int,
    in_exit: bool,
    bars_held: int,
    max_hold: int = 9999,
    ha_color: Any = None,
    ema9: Any = None,
    regime_ok: bool | None = None,
) -> _BarExit:
    """Advance the trade one bar. Mutates `state` (sl, partial_booked, be_moved,
    peak/trough). Returns the exit decision for this bar. `ha_color`/`ema9` are the
    precomputed reversal arrays (backtest); when None they are computed from df.
    `regime_ok` is this bar's regime-gate reading for the position's own side
    (None when regime_flip_exit is off or unavailable — never forces an exit)."""
    lo = float(df["low"].iloc[i])
    hi = float(df["high"].iloc[i])
    close = float(df["close"].iloc[i])
    is_short = state.is_short
    risk = state.initial_risk
    booked = False
    sl_changed = False

    # Exit 1a — book 50% at partial_book_r, then ride the rest from breakeven.
    if state.partial_r is not None and not state.partial_booked and risk > 0:
        lvl = (
            (state.entry - state.partial_r * risk)
            if is_short
            else (state.entry + state.partial_r * risk)
        )
        if (lo <= lvl) if is_short else (hi >= lvl):
            state.partial_booked = True
            booked = True
            new_sl = min(state.sl, state.entry) if is_short else max(state.sl, state.entry)
            if new_sl != state.sl:
                state.sl = new_sl
                sl_changed = True

    # Exit 1b — pull the stop to breakeven once breakeven_at_r run-up is reached.
    if state.be_r is not None and not state.be_moved and risk > 0:
        lvl = (state.entry - state.be_r * risk) if is_short else (state.entry + state.be_r * risk)
        if (lo <= lvl) if is_short else (hi >= lvl):
            state.be_moved = True
            new_sl = min(state.sl, state.entry) if is_short else max(state.sl, state.entry)
            if new_sl != state.sl:
                state.sl = new_sl
                sl_changed = True

    # Giveback lock + session-clock decay — tighten-only, from MFE up to the prior bar.
    lock_forced = False
    if state.giveback is not None or state.time_decay:
        if risk > 0:
            prior_mfe_r = (
                (state.entry - state.trough) / risk
                if is_short
                else (state.peak - state.entry) / risk
            )
        else:
            prior_mfe_r = 0.0
        clock = _bar_clock_minutes(df, i, state.tz) if state.time_decay else None
        for cand in _protective_stops(state, prior_mfe_r, clock):
            tighter = cand < state.sl if is_short else cand > state.sl
            if tighter:
                state.sl = cand
                sl_changed = True
                # A stop tightened this bar that price is already through cannot
                # be 'filled at the stop' — exit at the close instead (honest fill).
                if (close >= cand) if is_short else (close <= cand):
                    lock_forced = True

    # Profit gauge (run-up in R) — gates the trail and the reversal exit.
    if is_short:
        state.trough = min(state.trough, lo)
        profit_r = (state.entry - state.trough) / risk if risk > 0 else 0.0
    else:
        state.peak = max(state.peak, hi)
        profit_r = (state.peak - state.entry) / risk if risk > 0 else 0.0

    # Exit 2 — trail engages once run-up >= activation R.
    ma_close_exit = False
    if state.trail is not None and profit_r >= state.activation_r:
        if state.trail.method == TrailMethod.INDICATOR_CROSS and (state.trail.cross or "").endswith(
            "on_close"
        ):
            ma_val = float(
                compute(
                    df, (state.trail.indicator or "EMA").upper(), period=state.trail.period or 50
                ).iloc[i]
            )
            ma_close_exit = close > ma_val if is_short else close < ma_val
        else:
            new_sl = _compute_trail_value(
                state.trail,
                state.sl,
                state.entry,
                df,
                i,
                entry_bar=(i - bars_held),
                direction=(Direction.SHORT if is_short else Direction.LONG),
            )
            if new_sl != state.sl:
                state.sl = new_sl
                sl_changed = True

    # Exit 3 — reversal guard, only once protected (partial booked or stop at BE).
    reversal_exit = False
    if state.reversal_on and (state.partial_booked or state.be_moved):
        opp = 1 if is_short else -1
        if ha_color is None or ema9 is None:
            _o = df["open"].to_numpy(dtype=float)
            _h = df["high"].to_numpy(dtype=float)
            _l = df["low"].to_numpy(dtype=float)
            _c = df["close"].to_numpy(dtype=float)
            _hac = (_o + _h + _l + _c) / 4.0
            _hao = np.empty(len(df))
            if len(df):
                _hao[0] = (_o[0] + _c[0]) / 2.0
                for _k in range(1, len(df)):
                    _hao[_k] = (_hao[_k - 1] + _hac[_k - 1]) / 2.0
            ha_color = np.where(_hac >= _hao, 1, -1)
            ema9 = compute(df, "EMA", period=9).to_numpy(dtype=float)
        ha_streak = bool(ha_color[i] == opp and ha_color[i - 1] == opp)
        ema_cross = (close > ema9[i]) if is_short else (close < ema9[i])
        reversal_exit = ha_streak or bool(ema_cross)

    if is_short:
        hit_sl = hi >= state.sl
        hit_target = lo <= state.target
    else:
        hit_sl = lo <= state.sl
        hit_target = hi >= state.target
    # Time stop: unconditional by default; when time_stop_before_activation is
    # set, only fires while the trail hasn't engaged yet (Pine: `not trailOn`).
    time_out = bars_held >= max_hold and (
        not state.time_stop_before_activation or profit_r < state.activation_r
    )
    # Regime-flip: only an explicit False reading forces the exit — None (gate
    # unavailable) never does, matching the fail-open stance used elsewhere.
    regime_flip = state.regime_flip_on and regime_ok is False

    if lock_forced:
        return _BarExit(True, close, "Lock Stop", sl_changed, booked)
    if hit_sl or hit_target or time_out or ma_close_exit or in_exit or reversal_exit or regime_flip:
        if hit_sl and hit_target:
            exit_price, reason = state.sl, "SL Hit"  # pessimistic: SL first
        elif hit_sl:
            trailed = (state.sl > state.entry) if not is_short else (state.sl < state.entry)
            exit_price, reason = state.sl, ("Trail SL" if trailed else "SL Hit")
        elif hit_target:
            exit_price, reason = state.target, "Target Hit"
        elif reversal_exit:
            exit_price, reason = close, "Reversal"
        elif in_exit:
            exit_price, reason = close, "EOD Exit"
        elif ma_close_exit:
            exit_price, reason = close, "MA Close"
        elif regime_flip:
            exit_price, reason = close, "Regime Flip"
        else:
            exit_price, reason = close, "Time Stop"
        return _BarExit(True, exit_price, reason, sl_changed, booked)
    return _BarExit(False, 0.0, "", sl_changed, booked)


class SmartExitTracker:
    """Live wrapper around `_smart_exit_step` — one per open intraday position.
    Fed the latest 5-min df each scan cycle; returns the exit decision so the
    runtime can re-arm the stop, book the partial, or exit, exactly like the
    backtest. Reuses the SAME stepper, so live behaviour == backtest behaviour."""

    def __init__(
        self, spec: Any, entry: float, sl: float, target: float, direction: Direction
    ) -> None:
        self._spec = spec
        self._state = _make_exit_state(spec, entry, sl, target, direction)
        self._bars = 0
        self._max_hold = spec.exit_conditions.time_stop_days or 9999

    @property
    def current_sl(self) -> float:
        return self._state.sl

    @property
    def partial_booked(self) -> bool:
        return self._state.partial_booked

    @property
    def is_short(self) -> bool:
        return self._state.is_short

    def step(self, df: pd.DataFrame, in_exit: bool = False) -> _BarExit:
        self._bars += 1
        regime_ok: bool | None = None
        rf = self._spec.regime_filter
        if self._state.regime_flip_on and rf is not None and rf.enabled:
            long_ok, short_ok = _compute_self_regime(rf, df)
            gate = short_ok if self._state.is_short else long_ok
            regime_ok = bool(gate.iloc[-1])
        return _smart_exit_step(
            self._state,
            df,
            len(df) - 1,
            in_exit,
            bars_held=self._bars,
            max_hold=self._max_hold,
            regime_ok=regime_ok,
        )


# ---------------------------------------------------------------------------
# CompiledStrategy
# ---------------------------------------------------------------------------


@dataclass
class CompiledStrategy:
    spec: USSSpec
    _long_series: list[pd.Series] = field(default_factory=list, repr=False)
    _short_series: list[pd.Series] = field(default_factory=list, repr=False)
    _df_ref: pd.DataFrame | None = field(default=None, repr=False)
    _orb_range: pd.Series | None = field(default=None, repr=False)
    _in_entry: pd.Series | None = field(default=None, repr=False)
    _in_exit: pd.Series | None = field(default=None, repr=False)
    # Broad-market direction gate (set via set_market_context)
    _market_df: pd.DataFrame | None = field(default=None, repr=False)
    _mkt_long_ok: pd.Series | None = field(default=None, repr=False)
    _mkt_short_ok: pd.Series | None = field(default=None, repr=False)
    # Cross-sectional RS percentile override (set via set_rs_percentile_override)
    _rs_override: float | None = field(default=None, repr=False)
    # Self-regime gate (spec.regime_filter) and the pullback-low series a
    # `pullback_trigger` PATTERN condition tracks — see RegimeFilter/PULLBACK_LOW.
    _regime_long_ok: pd.Series | None = field(default=None, repr=False)
    _regime_short_ok: pd.Series | None = field(default=None, repr=False)
    _pullback_low: pd.Series | None = field(default=None, repr=False)
    # Cross-sectional breadth (date -> fraction of universe above its SMA), for
    # market_filter.mode == "breadth" (set via set_breadth_context).
    _breadth: pd.Series | None = field(default=None, repr=False)

    def set_breadth_context(self, breadth: pd.Series | None) -> None:
        """Supply the daily breadth series (index = date, value = fraction 0..1).
        Forces a recompile so the gate realigns to the next df."""
        self._breadth = breadth
        self._df_ref = None

    def set_market_context(self, market_df: pd.DataFrame | None) -> None:
        """Supply the benchmark (e.g. NIFTY 50) OHLCV for the market-direction
        gate. Forces a recompile so the per-bar gate realigns to the next df."""
        self._market_df = market_df
        self._df_ref = None

    def set_rs_percentile_override(self, value: float | None) -> None:
        """Override RS_PERCENTILE with a cross-sectional percentile for the
        CURRENT evaluation (this symbol ranked against its peer universe, e.g.
        from RSRankingEngine), instead of the indicator's default rank against
        its own trailing history. Forces a recompile on the next evaluate_candle."""
        self._rs_override = value
        self._df_ref = None

    @property
    def _wants_long(self) -> bool:
        return self.spec.meta.direction in (Direction.LONG, Direction.BOTH)

    @property
    def _wants_short(self) -> bool:
        return self.spec.meta.direction in (Direction.SHORT, Direction.BOTH)

    def _ensure_compiled(self, df: pd.DataFrame) -> None:
        if self._df_ref is not df:
            sess = self.spec.intraday
            bench = (
                self._market_df["close"]
                if self._market_df is not None and "close" in self._market_df
                else None
            )
            # Self-regime gate computed BEFORE the ConditionCompiler so a
            # `pullback_trigger` PATTERN condition can read it during compilation.
            self._regime_long_ok, self._regime_short_ok = self._self_regime_gates(df)
            if sess is not None:
                orb_high, orb_low, self._in_entry, self._in_exit = compute_session_levels(df, sess)
                self._orb_range = orb_high - orb_low
                cc = ConditionCompiler(
                    orb_high=orb_high,
                    orb_low=orb_low,
                    benchmark=bench,
                    rs_override=self._rs_override,
                    regime_long_ok=self._regime_long_ok,
                )
            else:
                self._orb_range = self._in_entry = self._in_exit = None
                cc = ConditionCompiler(
                    benchmark=bench,
                    rs_override=self._rs_override,
                    regime_long_ok=self._regime_long_ok,
                )
            conds = [c for c in self.spec.entry_conditions.conditions if c.enabled]
            self._long_series = (
                [cc.compile_series(c, df, short=False) for c in conds] if self._wants_long else []
            )
            self._short_series = (
                [cc.compile_series(c, df, short=True) for c in conds] if self._wants_short else []
            )
            self._pullback_low = cc.last_pullback_low
            self._mkt_long_ok, self._mkt_short_ok = self._market_gates(df)
            self._df_ref = df

    def _self_regime_gates(self, df: pd.DataFrame) -> tuple[pd.Series | None, pd.Series | None]:
        """Per-bar (long_ok, short_ok) from the traded symbol's OWN price vs its
        own long SMA, with a slope requirement — see RegimeFilter. Unlike
        `_market_gates` (a shared benchmark), this always reads `df` itself, so
        it's cheap to compute unconditionally alongside entry conditions.
        Returns (None, None) when the filter is unset/disabled.
        """
        rf = self.spec.regime_filter
        if rf is None or not rf.enabled:
            return None, None
        return _compute_self_regime(rf, df)

    def _market_gates(self, df: pd.DataFrame) -> tuple[pd.Series | None, pd.Series | None]:
        """Per-bar (long_ok, short_ok) from the benchmark's direction vs its
        previous-day close. Returns (None, None) when the filter is off or the
        benchmark data is unavailable (fail-open — never silently zeroes signals).
        """
        mf = self.spec.market_filter
        if mf is not None and mf.enabled and mf.mode == "breadth":
            return self._breadth_gates(df, mf.min_breadth)
        if mf is None or not mf.enabled or self._market_df is None or len(self._market_df) == 0:
            return None, None
        try:
            m = self._market_df
            if not isinstance(df.index, pd.DatetimeIndex) or not isinstance(
                m.index, pd.DatetimeIndex
            ):
                return None, None

            def _utc_naive(ix: pd.DatetimeIndex) -> pd.DatetimeIndex:
                return ix.tz_convert("UTC").tz_localize(None) if ix.tz is not None else ix

            sidx = _utc_naive(df.index)
            midx = _utc_naive(m.index)
            mclose = pd.Series(m["close"].to_numpy(dtype=float), index=midx).sort_index()
            mclose = mclose[~mclose.index.duplicated(keep="last")]

            # Benchmark close at/before each symbol bar (5m grids align; ffill is safe).
            cur = mclose.reindex(sidx, method="ffill").to_numpy()

            if mf.mode == "regime_sma":
                # Regime gate (O'Neil's "M"): longs only while the benchmark
                # closes above its N-bar SMA; shorts mirror. Fail-closed where
                # the benchmark/SMA is unavailable.
                sma = mclose.rolling(mf.sma_period).mean().reindex(sidx, method="ffill")
                sma_np = sma.to_numpy()
                valid = ~(np.isnan(cur) | np.isnan(sma_np))
                long_ok = pd.Series((cur > sma_np) & valid, index=df.index)
                short_ok = pd.Series((cur < sma_np) & valid, index=df.index)
                return long_ok, short_ok
            # Previous-day benchmark close, mapped onto each bar's date.
            daily = mclose.groupby(mclose.index.date).last()
            prev_by_date = daily.shift(1)
            bar_dates = pd.Series(sidx).dt.date
            prev = bar_dates.map(prev_by_date.to_dict()).to_numpy(dtype=float)

            valid = ~(np.isnan(cur) | np.isnan(prev))
            # FAIL-CLOSED: where benchmark data is missing, BOTH directions are
            # blocked. Previously this failed OPEN (allowed both), which let a LONG
            # fire on a down day when NIFTY data was missing — producing trades in
            # the WRONG direction vs the Pine script. Safety docs require failing
            # safely under uncertainty: no trade beats a wrong-direction trade.
            long_ok = pd.Series((cur > prev) & valid, index=df.index)
            short_ok = pd.Series((cur < prev) & valid, index=df.index)
            return long_ok, short_ok
        except Exception as exc:
            # Fails OPEN (no market gate) — must not do so silently.
            _log.warning(
                "market_gates_err strategy=%s err=%s: %s — market filter NOT applied",
                self.spec.meta.name,
                type(exc).__name__,
                exc,
            )
            return None, None

    def _breadth_gates(self, df: pd.DataFrame, min_breadth: float) -> tuple[pd.Series, pd.Series]:
        """Long allowed on a bar only when that bar's date has breadth >= min_breadth.
        FAIL-CLOSED: no breadth context, a non-date index, or a date missing from the
        series blocks entries (logged) — an unknown market state is not a green light.
        Shorts are never allowed by this gate (breadth is a long-side permission)."""
        blocked = pd.Series(False, index=df.index)
        if self._breadth is None or len(self._breadth) == 0:
            if self.spec.meta.name not in _BREADTH_WARNED:  # once per strategy, not per symbol
                _BREADTH_WARNED.add(self.spec.meta.name)
                _log.warning(
                    "breadth_gate_no_context strategy=%s — entries BLOCKED (fail-closed)",
                    self.spec.meta.name,
                )
            return blocked, blocked
        if not isinstance(df.index, pd.DatetimeIndex):
            _log.warning(
                "breadth_gate_bad_index strategy=%s — entries BLOCKED", self.spec.meta.name
            )
            return blocked, blocked
        ix = df.index.tz_convert("Asia/Kolkata") if df.index.tz is not None else df.index
        vals = pd.Series(ix.date, index=df.index).map(self._breadth)
        return vals.ge(min_breadth).fillna(False).astype(bool), blocked

    def explain_entry(self, df: pd.DataFrame, idx: int = -1) -> list[dict[str, Any]]:
        """Per-condition result at `idx` for the LONG side: what each entry
        condition checked, the value it found, and whether it passed. Diagnostic
        only — reuses the series evaluate_candle already compiled for this df.
        A failure to measure a value is reported in the row ('value' = 'ERR ...')
        and logged, never dropped."""
        if not self._wants_long:
            return []
        self._ensure_compiled(df)
        conds = [c for c in self.spec.entry_conditions.conditions if c.enabled]
        rows: list[dict[str, Any]] = []
        for cond, series in zip(conds, self._long_series, strict=True):
            p = cond.params.get("params", cond.params)
            rule = {k: v for k, v in p.items() if k not in ("indicator", "pattern")}
            value: Any = None
            try:
                if cond.type == ConditionType.INDICATOR:
                    ind = (p.get("indicator") or p.get("name", "")).upper()
                    if ind == "RS_PERCENTILE" and self._rs_override is not None:
                        value = self._rs_override
                    elif ind != "RS_PERCENTILE":
                        value = float(compute(df, ind, period=int(p.get("period", 20))).iloc[idx])
                elif cond.type == ConditionType.PRICE:
                    value = float(df["close"].iloc[idx])
                elif cond.type == ConditionType.VOLUME:
                    value = float(df["volume"].iloc[idx])
            except Exception as exc:
                value = f"ERR {type(exc).__name__}: {exc}"
                _log.warning(
                    "explain_entry_value_err strategy=%s cond=%s err=%s",
                    self.spec.meta.name,
                    cond.name,
                    value,
                )
            rows.append(
                {
                    "name": cond.name,
                    "type": str(cond.type),
                    "passed": bool(series.iloc[idx]),
                    "value": value,
                    "rule": rule,
                }
            )
        for gate, ok in (("market_gate", self._mkt_long_ok), ("regime_gate", self._regime_long_ok)):
            if ok is not None:
                rows.append(
                    {
                        "name": gate,
                        "type": "GATE",
                        "passed": bool(ok.iloc[idx]),
                        "value": None,
                        "rule": {},
                    }
                )
        return rows

    def _session_range_at(self, idx: int) -> float | None:
        if self._orb_range is None:
            return None
        return float(self._orb_range.iloc[idx])

    def _pullback_low_at(self, idx: int) -> float | None:
        if self._pullback_low is None:
            return None
        val = float(self._pullback_low.iloc[idx])
        return val if val == val else None  # NaN-safe

    def _combine(self, series: list[pd.Series], df: pd.DataFrame) -> pd.Series:
        if not series:
            return pd.Series(False, index=df.index)
        if self.spec.entry_conditions.logic == LogicOperator.AND:
            mask = pd.Series(True, index=df.index)
            for s in series:
                mask = mask & s
        else:
            mask = pd.Series(False, index=df.index)
            for s in series:
                mask = mask | s
        return mask

    def _gate_window(self, mask: pd.Series) -> pd.Series:
        # Intraday strategies may only enter inside the entry window.
        if self._in_entry is not None:
            return mask & self._in_entry
        return mask

    def _long_mask(self, df: pd.DataFrame) -> pd.Series:
        self._ensure_compiled(df)
        mask = self._gate_window(self._combine(self._long_series, df))
        if self._mkt_long_ok is not None:
            mask = mask & self._mkt_long_ok
        if self._regime_long_ok is not None:
            mask = mask & self._regime_long_ok
        return mask

    # Back-compat alias: the long entry mask was historically `_entry_mask`.
    def _entry_mask(self, df: pd.DataFrame) -> pd.Series:
        return self._long_mask(df)

    def _short_mask(self, df: pd.DataFrame) -> pd.Series:
        self._ensure_compiled(df)
        mask = self._gate_window(self._combine(self._short_series, df))
        if self._mkt_short_ok is not None:
            mask = mask & self._mkt_short_ok
        if self._regime_short_ok is not None:
            mask = mask & self._regime_short_ok
        return mask

    def evaluate_candle(self, df: pd.DataFrame, idx: int) -> Signal | None:
        entry = float(df["close"].iloc[idx])
        sr = self._session_range_at(idx)
        pl = self._pullback_low_at(idx)
        _log.debug(
            "evaluate_candle_received strategy=%s entry=%.2f idx=%s wants_long=%s wants_short=%s",
            self.spec.meta.name,
            entry,
            idx,
            self._wants_long,
            self._wants_short,
        )

        if self._wants_long and bool(self._long_mask(df).iloc[idx]):
            sl = _compute_sl_value(self.spec.stop_loss, entry, df, idx, Direction.LONG, sr, pl)
            target = _compute_target_value(self.spec.target, entry, sl, Direction.LONG)
            if sl < entry < target:
                _log.debug(
                    "evaluate_candle_found strategy=%s side=LONG action=signal entry=%.2f "
                    "sl=%.2f target=%.2f",
                    self.spec.meta.name,
                    entry,
                    sl,
                    target,
                )
                return Signal(self.spec.meta.name, Direction.LONG, entry, sl, target)
            _log.debug(
                "evaluate_candle_found strategy=%s side=LONG action=rejected "
                "reason=invalid_geometry entry=%.2f sl=%.2f target=%.2f",
                self.spec.meta.name,
                entry,
                sl,
                target,
            )

        if self._wants_short and bool(self._short_mask(df).iloc[idx]):
            sl = _compute_sl_value(self.spec.stop_loss, entry, df, idx, Direction.SHORT, sr, pl)
            target = _compute_target_value(self.spec.target, entry, sl, Direction.SHORT)
            if target < entry < sl:
                _log.debug(
                    "evaluate_candle_found strategy=%s side=SHORT action=signal entry=%.2f "
                    "sl=%.2f target=%.2f",
                    self.spec.meta.name,
                    entry,
                    sl,
                    target,
                )
                return Signal(self.spec.meta.name, Direction.SHORT, entry, sl, target)
            _log.debug(
                "evaluate_candle_found strategy=%s side=SHORT action=rejected "
                "reason=invalid_geometry entry=%.2f sl=%.2f target=%.2f",
                self.spec.meta.name,
                entry,
                sl,
                target,
            )

        return None

    def _exec_direction(self) -> Direction:
        # For BOTH, the public single-value helpers default to the long leg.
        return Direction.SHORT if self.spec.meta.direction == Direction.SHORT else Direction.LONG

    def compute_sl(self, entry: float, df: pd.DataFrame, idx: int) -> float:
        self._ensure_compiled(df)
        return _compute_sl_value(
            self.spec.stop_loss,
            entry,
            df,
            idx,
            self._exec_direction(),
            self._session_range_at(idx),
            self._pullback_low_at(idx),
        )

    def compute_target(self, entry: float, sl: float) -> float:
        return _compute_target_value(self.spec.target, entry, sl, self._exec_direction())

    def compute_trail(self, current_sl: float, entry: float, df: pd.DataFrame, idx: int) -> float:
        trail = effective_exit_conditions(self.spec).trailing_stop
        if trail is None:
            return current_sl
        return _compute_trail_value(
            trail, current_sl, entry, df, idx, direction=self._exec_direction()
        )

    def generate_backtest_signals(self, df: pd.DataFrame) -> list[BacktestSignal]:
        long_mask = self._long_mask(df) if self._wants_long else None
        short_mask = self._short_mask(df) if self._wants_short else None
        max_hold = self.spec.exit_conditions.time_stop_days or 9999

        signals: list[BacktestSignal] = []
        # ORB takes a single trade per day (Pine's orb_traded flag). Only enforced
        # for intraday specs; positional strategies may re-enter freely.
        one_per_day = self.spec.intraday is not None
        traded_days: set[Any] = set()
        in_trade = False
        side = Direction.LONG
        trade_bar = 0
        trade_sl_initial = 0.0  # stop at entry — fixed for journaling (state.sl trails)
        state: _ExitState | None = None

        # Exit model is driven by exit_conditions via the shared _smart_exit_step —
        # the SAME stepper the live SmartExitTracker uses, so live == backtest.
        ec = effective_exit_conditions(self.spec)
        partial_r = ec.partial_book_r
        max_hold = self.spec.exit_conditions.time_stop_days or 9999

        # Reversal arrays precomputed ONCE here (perf); passed into the stepper.
        ha_color = None
        ema9 = None
        if bool(ec.reversal_exit):
            _o = df["open"].to_numpy(dtype=float)
            _h = df["high"].to_numpy(dtype=float)
            _l = df["low"].to_numpy(dtype=float)
            _c = df["close"].to_numpy(dtype=float)
            _ha_close = (_o + _h + _l + _c) / 4.0
            _ha_open = np.empty(len(df))
            if len(df):
                _ha_open[0] = (_o[0] + _c[0]) / 2.0
                for _k in range(1, len(df)):
                    _ha_open[_k] = (_ha_open[_k - 1] + _ha_close[_k - 1]) / 2.0
            ha_color = np.where(_ha_close >= _ha_open, 1, -1)
            ema9 = compute(df, "EMA", period=9).to_numpy(dtype=float)

        # Regime series for the regime-flip exit (A5) — same self-regime gate
        # entries already use, read here per-bar for whichever side is open.
        regime_flip_on = bool(ec.regime_flip_exit) and self.spec.regime_filter is not None
        frac = float(ec.partial_fraction if ec.partial_fraction is not None else 0.5)

        for i in range(1, len(df)):
            if in_trade and state is not None:
                in_exit = self._in_exit is not None and bool(self._in_exit.iloc[i])
                regime_ok: bool | None = None
                if regime_flip_on:
                    gate = self._regime_short_ok if state.is_short else self._regime_long_ok
                    regime_ok = bool(gate.iloc[i]) if gate is not None else True
                be = _smart_exit_step(
                    state,
                    df,
                    i,
                    in_exit,
                    bars_held=(i - trade_bar),
                    max_hold=max_hold,
                    ha_color=ha_color,
                    ema9=ema9,
                    regime_ok=regime_ok,
                )
                if be.exit:
                    exit_price, reason = be.exit_price, be.reason
                    is_short = state.is_short
                    # Use initial_risk (at entry) — state.sl may have trailed past
                    # entry, making current risk meaningless.
                    leg_r = (
                        ((state.entry - exit_price) if is_short else (exit_price - state.entry))
                        / state.initial_risk
                        if state.initial_risk > 0
                        else 0.0
                    )
                    # Two-tranche P&L: booked `frac` locks (partial_r)×frac; remaining
                    # (1-frac) exits at trail/EOD. No partial → whole position at one exit.
                    if state.partial_booked:
                        pnl_r = frac * (partial_r or 0.0) + (1 - frac) * leg_r
                        partial_r_val: float | None = frac
                        leg_pnls = [frac * (partial_r or 0.0), (1 - frac) * leg_r]
                    else:
                        pnl_r = leg_r
                        partial_r_val = None
                        leg_pnls = [leg_r]
                    signals.append(
                        BacktestSignal(
                            bar_index=trade_bar,
                            date=df.index[trade_bar],
                            entry_price=state.entry,
                            sl=trade_sl_initial,
                            target=state.target,
                            exit_price=exit_price,
                            exit_bar=i,
                            pnl_r=pnl_r,
                            direction=side,
                            exit_reason=reason,
                            partial_r=partial_r_val,
                            leg_pnls_r=leg_pnls,
                        )
                    )
                    in_trade = False
                    state = None

            if not in_trade:
                entered = False
                sr = self._session_range_at(i)
                pl = self._pullback_low_at(i)
                day = df.index[i].date() if one_per_day else None
                blocked = one_per_day and day in traded_days
                if not blocked and long_mask is not None and bool(long_mask.iloc[i]):
                    entry = float(df["close"].iloc[i])
                    sl = _compute_sl_value(
                        self.spec.stop_loss, entry, df, i, Direction.LONG, sr, pl
                    )
                    target = _compute_target_value(self.spec.target, entry, sl, Direction.LONG)
                    if sl < entry < target:
                        side, trade_bar, trade_sl_initial = Direction.LONG, i, sl
                        state = _make_exit_state(
                            self.spec, entry, sl, target, Direction.LONG, df, i
                        )
                        in_trade = entered = True
                if (
                    not entered
                    and not blocked
                    and short_mask is not None
                    and bool(short_mask.iloc[i])
                ):
                    entry = float(df["close"].iloc[i])
                    sl = _compute_sl_value(
                        self.spec.stop_loss, entry, df, i, Direction.SHORT, sr, pl
                    )
                    target = _compute_target_value(self.spec.target, entry, sl, Direction.SHORT)
                    if target < entry < sl:
                        side, trade_bar, trade_sl_initial = Direction.SHORT, i, sl
                        state = _make_exit_state(
                            self.spec, entry, sl, target, Direction.SHORT, df, i
                        )
                        in_trade = True
                if in_trade and day is not None:
                    traded_days.add(day)

        return signals


# ---------------------------------------------------------------------------
# USSCompiler — entry point
# ---------------------------------------------------------------------------


class USSCompiler:
    def compile(self, spec: USSSpec) -> CompiledStrategy:
        return CompiledStrategy(spec=spec)
