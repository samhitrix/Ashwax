"""options_intraday_short_premium -- daily intraday NIFTY option-selling strategy hosted inside SellerDesk.

NAMING: deliberately generic. The same code is a short STRADDLE (ce_offset = pe_offset = 0), a short
STRANGLE (the default, +150 / -150 around ATM) or an iron fly/condor (wing_points > 0), depending only on
config -- so it is named for what it does (intraday, sells premium), not for one structure.

Rules (frozen, docs/hypotheses/I_options_intraday_short_premium.md): at ``entry_time`` sell the
ATM+``ce_offset`` call and ATM+``pe_offset`` put on the nearest weekly expiry; every leg carries its OWN
stop (``sl_pct`` % of its entry premium); a stopped leg is closed alone; optionally everything is booked
when total profit reaches ``target_pct`` % of the credit; whatever is left is closed
at ``exit_time``. No rolls/adjustments.

Runs daily and is exempt from open-position / trades-per-day / exposure caps (CRM
``_UNCAPPED_OPTIONS_STRATEGIES``); it still obeys the kill switch, its own per-strategy daily-loss
cap, the segment daily-loss breaker and the real-margin check. Naked (``wing_points`` 0) is allowed
ONLY because ``allow_naked`` is explicitly true -- the executor's other callers are untouched.

The 5-minute desk loop cannot hit a 10:30 entry or a 23% stop (median stop in the real run fires 29 min
after entry), so ``tick()`` is driven by its own fast loop (orchestrator ``_options_intraday_short_premium_loop``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from src.notifications import EventKind, notify
from src.notifications.events import Segment
from src.options import seller_settings as _settings
from src.options.nfo_chain import index_spot_quote_symbol
from src.options.structure_builder import leg_from_chain_row

if TYPE_CHECKING:
    from src.options.nfo_chain import OptionChain
    from src.options.seller_desk import OpenPosition, SellerDesk
    from src.options.structure_selector import OptionLeg

SHORT_PREMIUM_TYPE = "options_intraday_short_premium"
# Second instance of the same engine with its own config block, registry name and position family
# (hypothesis P: SENSEX ATM straddle, per-leg SL). Everything is parameterised in its settings block.
SHORT_PREMIUM2_TYPE = "options_intraday_short_premium2"
# Types whose positions carry per-leg stops from the first tick and are managed by this engine.
SHORT_PREMIUM_TYPES = frozenset({SHORT_PREMIUM_TYPE, SHORT_PREMIUM2_TYPE})
# The expiry-day strangle (config options_expiry_day with structure="strangle") runs on THIS engine under the
# strategy's own name; must equal seller_desk.EXPIRY_DAY_TYPE (seller_desk imports this module, so it cannot be imported here).
EXPIRY_STRANGLE_TYPE = "options_expiry_day"
_IST = ZoneInfo("Asia/Kolkata")
_LOG = logging.getLogger("options.short_premium")
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_MAX_ENTRY_ATTEMPTS = 2
_MISSING_LTP_ALERT_TICKS = 6


@dataclass(frozen=True)
class ShortPremiumCfg:
    registry_name: str
    underlying: str
    entry_time: dtime
    entry_grace_minutes: int
    exit_time: dtime
    skip_weekdays: frozenset[int]
    ce_offset: float
    pe_offset: float
    sl_pct_ce: float
    sl_pct_pe: float
    target_pct: float
    lots: int
    max_lots: int
    risk_fraction_of_daily_cap: float
    wing_points: float
    allow_naked: bool
    min_leg_premium: float
    min_oi: int
    min_volume: int
    max_spread_pct: float
    max_spread_points: float
    poll_seconds: float
    respect_event_blackout: bool
    llm_shadow: bool
    strategy_type: str = SHORT_PREMIUM_TYPE
    expiry_only: bool = False  # trade only on the underlying's own expiry day


def _hhmm(value: Any, key: str) -> dtime:
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"{key}={value!r} is not HH:MM") from exc


def parse_cfg(
    raw: dict[str, Any], *, strategy_type: str = SHORT_PREMIUM_TYPE, expiry_only: bool = False
) -> ShortPremiumCfg:
    """Validate the ``options_intraday_short_premium`` (or, expiry-only, ``options_expiry_day``) block. Raises ValueError with the reason -- a bad edit must
    disable new entries loudly, never trade a half-understood configuration."""
    sl = float(raw.get("sl_pct", 23))
    cfg = ShortPremiumCfg(
        registry_name=str(raw.get("registry_name", strategy_type)),
        underlying=str(raw.get("underlying", "NIFTY")).upper(),
        entry_time=_hhmm(raw.get("entry_time", "10:30"), "entry_time"),
        entry_grace_minutes=int(raw.get("entry_grace_minutes", 5)),
        exit_time=_hhmm(raw.get("exit_time", "15:01"), "exit_time"),
        skip_weekdays=frozenset(
            _WEEKDAYS[str(d).strip().lower()[:3]]
            for d in raw.get("skip_weekdays", [] if expiry_only else ["Wed"])
        ),
        ce_offset=float(raw.get("ce_offset_points", 150)),
        pe_offset=float(raw.get("pe_offset_points", -150)),
        sl_pct_ce=float(raw.get("sl_pct_ce", sl)),
        sl_pct_pe=float(raw.get("sl_pct_pe", sl)),
        target_pct=float(raw.get("target_pct", 0)),
        lots=int(raw.get("lots", 0)),
        max_lots=int(raw.get("max_lots", 5)),
        risk_fraction_of_daily_cap=float(raw.get("risk_fraction_of_daily_cap", 0.8)),
        wing_points=float(raw.get("wing_points", 0)),
        allow_naked=bool(raw.get("allow_naked", False)),
        min_leg_premium=float(raw.get("min_leg_premium", 0.0)),
        min_oi=int(raw.get("min_oi", 500)),
        min_volume=int(raw.get("min_volume", 200)),
        max_spread_pct=float(raw.get("max_spread_pct", 15.0)),
        max_spread_points=float(raw.get("max_spread_points", 0.30)),
        poll_seconds=float(raw.get("poll_seconds", 10)),
        respect_event_blackout=bool(raw.get("respect_event_blackout", False)),
        llm_shadow=bool(raw.get("llm_shadow", True)),
        strategy_type=strategy_type,
        expiry_only=expiry_only,
    )
    if not cfg.entry_time < cfg.exit_time:
        raise ValueError("entry_time must be before exit_time")
    # The operator's exit_time is the exit (the expiry guardian defers to it, see
    # SellerDesk._guardian_one); the only hard bound is that the market is still open to fill it.
    from src.market import sessions  # noqa: PLC0415

    fo_close = sessions.regular_close("nse_fo")
    if cfg.exit_time >= fo_close:
        raise ValueError(f"exit_time must be before the F&O close {fo_close:%H:%M}")
    if not (0 < cfg.sl_pct_ce <= 500 and 0 < cfg.sl_pct_pe <= 500):
        raise ValueError(
            "sl_pct must be in (0, 500] -- a short-premium leg must always have a stop"
        )
    if cfg.target_pct < 0:
        raise ValueError("target_pct must be >= 0 (0 = no profit target)")
    if cfg.lots < 0 or cfg.max_lots < 1:
        raise ValueError("lots must be >= 0 (0 = auto-size from the % budget) and max_lots >= 1")
    if not 0 < cfg.risk_fraction_of_daily_cap <= 1:
        raise ValueError("risk_fraction_of_daily_cap must be in (0, 1]")
    if cfg.wing_points < 0:
        raise ValueError("wing_points must be >= 0")
    if cfg.wing_points == 0 and not cfg.allow_naked:
        raise ValueError("wing_points=0 means naked shorts; set allow_naked=true to opt in")
    if cfg.ce_offset < cfg.pe_offset:
        raise ValueError("ce_offset_points must be >= pe_offset_points (no inverted strikes)")
    if cfg.poll_seconds < 1:
        raise ValueError("poll_seconds must be >= 1")
    return cfg


_last_cfg_error: str | None = None


def load_cfg() -> ShortPremiumCfg | None:
    """The live config, or None when the block is absent/disabled/invalid (invalid is logged once)."""
    global _last_cfg_error
    raw = _settings.short_premium()
    if not raw.get("enabled", False):
        return None
    try:
        cfg = parse_cfg(raw)
    except (ValueError, KeyError) as exc:
        if str(exc) != _last_cfg_error:
            _LOG.error(
                "options_intraday_short_premium_config_invalid err=%s -- new entries disabled", exc
            )
            _last_cfg_error = str(exc)
        return None
    _last_cfg_error = None
    return cfg


_last_cfg2_error: str | None = None


def load_cfg2() -> ShortPremiumCfg | None:
    """The second instance's config (``options_intraday_short_premium2`` block), or None when the block is
    absent/disabled/invalid. Same validation as the first; every rule (SL, entry/exit time, offsets, weekdays,
    underlying) is read from the block on each tick, so edits apply with no restart."""
    global _last_cfg2_error
    raw = _settings.short_premium2()
    if not raw.get("enabled", False):
        return None
    try:
        cfg = parse_cfg(raw, strategy_type=SHORT_PREMIUM2_TYPE)
    except (ValueError, KeyError) as exc:
        if str(exc) != _last_cfg2_error:
            _LOG.error(
                "options_intraday_short_premium2_config_invalid err=%s -- new entries disabled", exc
            )
            _last_cfg2_error = str(exc)
        return None
    _last_cfg2_error = None
    return cfg


_last_expiry_cfg_error: str | None = None


def load_expiry_cfg() -> ShortPremiumCfg | None:
    """The expiry-day strangle's config (``options_expiry_day`` block, ``structure`` = "strangle"), or None when
    the block is absent/disabled/another structure/invalid. Same validation as the daily strategy."""
    global _last_expiry_cfg_error
    raw = _settings.expiry_day()
    if not raw.get("enabled", False) or str(raw.get("structure", "butterfly")) != "strangle":
        return None
    try:
        # ``wing_points`` in this block is the legacy butterfly's; the strangle has its own key (default 0 = naked,
        # which parse_cfg then requires ``allow_naked`` for).
        mapped = {**raw, "wing_points": raw.get("strangle_wing_points", 0)}
        cfg = parse_cfg(mapped, strategy_type=EXPIRY_STRANGLE_TYPE, expiry_only=True)
    except (ValueError, KeyError) as exc:
        if str(exc) != _last_expiry_cfg_error:
            _LOG.error(
                "options_expiry_day_strangle_config_invalid err=%s -- new entries disabled", exc
            )
            _last_expiry_cfg_error = str(exc)
        return None
    _last_expiry_cfg_error = None
    return cfg


def is_strangle_position(pos: Any, strategy_type: str) -> bool:
    """True when ``pos`` is run by this engine. The expiry-day type is shared with the legacy butterfly, which
    has no per-leg stops; only strangle rows carry the ``sl_pct`` meta frozen at entry."""
    if pos.strategy_type != strategy_type:
        return False
    return strategy_type in SHORT_PREMIUM_TYPES or "sl_pct" in pos.meta


def _state_attrs(cfg_type: str) -> tuple[str, str, str]:
    """Names of the desk's per-strategy (attempts, entered_day, missed_day) fields."""
    if cfg_type == SHORT_PREMIUM_TYPE:
        return "_short_premium_attempts", "_short_premium_entered_day", "_short_premium_missed_day"
    if cfg_type == SHORT_PREMIUM2_TYPE:
        return (
            "_short_premium2_attempts",
            "_short_premium2_entered_day",
            "_short_premium2_missed_day",
        )
    return (
        "_expiry_strangle_attempts",
        "_expiry_strangle_entered_day",
        "_expiry_strangle_missed_day",
    )


def _q_desc(q: Any) -> str:
    return "none" if q is None else f"bid={q.bid:g}/ask={q.ask:g}/oi={q.oi}/vol={q.volume}"


def _quote_ok(
    quote: Any, *, min_oi: int, min_volume: int, max_spread_pct: float, max_spread_points: float
) -> bool:
    """This strategy's own liquidity test. The shared row_is_liquid() demands a <=5% bid-ask spread,
    which a Rs2-5 expiry-day leg (26% of days, cheaper leg median Rs6.7 at 10:30) fails on a Rs0.15
    spread -- a skipped day for a harmless reason. Pass when the spread is within max_spread_pct OR
    within max_spread_points absolute; OI/volume floors stay."""
    if quote is None or quote.bid <= 0.0 or quote.ask <= 0.0:
        return False
    if quote.oi < min_oi or quote.volume < min_volume:
        return False
    spread = quote.ask - quote.bid
    mid = (quote.ask + quote.bid) / 2.0
    return bool(
        spread <= max_spread_points or (mid > 0.0 and spread / mid * 100.0 <= max_spread_pct)
    )


def build_short_premium_legs(
    chain: OptionChain,
    lots: int,
    *,
    ce_offset: float,
    pe_offset: float,
    wing_points: float,
    min_oi: int = 500,
    min_volume: int = 200,
    max_spread_pct: float = 15.0,
    max_spread_points: float = 0.30,
    why: list[str] | None = None,
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """(protection, income) legs: SELL ATM+offset CE and PE, optional BUY wings ``wing_points``
    beyond each short. ATM = listed strike nearest spot. None when any strike/quote/liquidity is
    missing -- never a substituted strike, so the traded structure is always the tested one."""
    why = why if why is not None else []
    rows = [r for r in chain.strikes if r.call is not None and r.put is not None]
    if not rows:
        why.append("no strike has both a call and a put quote")
        return None
    atm = min(rows, key=lambda r: abs(r.strike - chain.spot)).strike
    by_strike = {r.strike: r for r in chain.strikes}
    ce_row, pe_row = by_strike.get(atm + ce_offset), by_strike.get(atm + pe_offset)
    if ce_row is None or pe_row is None:
        why.append(f"strike missing: atm={atm:g} ce={atm + ce_offset:g} pe={atm + pe_offset:g}")
        return None

    def _ok(quote: Any) -> bool:
        return _quote_ok(
            quote,
            min_oi=min_oi,
            min_volume=min_volume,
            max_spread_pct=max_spread_pct,
            max_spread_points=max_spread_points,
        )

    if not (_ok(ce_row.call) and _ok(pe_row.put)):
        why.append(
            f"illiquid: ce={_q_desc(ce_row.call)} pe={_q_desc(pe_row.put)} "
            f"limits=oi>={min_oi} vol>={min_volume} spread<={max_spread_pct:g}% or {max_spread_points:g}pt"
        )
        return None
    income = [
        leg_from_chain_row(pe_row, "PE", "SELL", lots, chain.lot_size),
        leg_from_chain_row(ce_row, "CE", "SELL", lots, chain.lot_size),
    ]
    protection: list[OptionLeg | None] = []
    if wing_points > 0:
        wc, wp = (
            by_strike.get(ce_row.strike + wing_points),
            by_strike.get(pe_row.strike - wing_points),
        )
        if wc is None or wp is None or not (_ok(wc.call) and _ok(wp.put)):
            return None
        protection = [
            leg_from_chain_row(wp, "PE", "BUY", lots, chain.lot_size),
            leg_from_chain_row(wc, "CE", "BUY", lots, chain.lot_size),
        ]
    if any(leg is None for leg in income + protection):
        return None
    return [leg for leg in protection if leg is not None], [
        leg for leg in income if leg is not None
    ]


def open_legs_credit(legs: list[OptionLeg]) -> float:
    """Net premium received on these legs in rupees (short legs +, long wings -)."""
    return sum(
        (1.0 if leg.direction == "SELL" else -1.0) * leg.premium * leg.quantity for leg in legs
    )


def sl_pct_for(cfg: ShortPremiumCfg, kind: str) -> float:
    return cfg.sl_pct_ce if kind == "CE" else cfg.sl_pct_pe


def stop_trigger(premium: float, sl_pct: float) -> float:
    """Price at/above which a short leg is stopped: entry premium x (1 + sl_pct/100)."""
    return premium * (1.0 + sl_pct / 100.0)


def sl_defined_max_loss(income: list[OptionLeg], cfg: ShortPremiumCfg) -> float:
    """Rupees lost if every short leg is stopped exactly at its trigger -- the SL-DEFINED risk
    (not structural: a gap through a stop loses more). Fed to CRM as the trade's risk."""
    return sum(
        (stop_trigger(leg.premium, sl_pct_for(cfg, leg.instrument_type)) - leg.premium)
        * leg.quantity
        for leg in income
    )


def in_entry_window(now: datetime, cfg: ShortPremiumCfg) -> bool:
    start = datetime.combine(now.date(), cfg.entry_time, tzinfo=_IST)
    return start <= now < start + timedelta(minutes=cfg.entry_grace_minutes)


def _today_position_exists(
    desk: SellerDesk, today: date, strategy_type: str = SHORT_PREMIUM_TYPE
) -> bool:
    return any(
        is_strangle_position(p, strategy_type) and p.entered_at.astimezone(_IST).date() == today
        for p in desk.positions.values()
    )


def _alert_kw(strategy_type: str) -> dict[str, str]:
    """Extra _alert() kwargs: the daily strategy keeps its original call shape, the expiry strangle names itself."""
    return {} if strategy_type == SHORT_PREMIUM_TYPE else {"strategy_type": strategy_type}


_LAST_SKIP: dict[str, tuple[date, str]] = {}


class _SkipRecorder(logging.Filter):
    """Remember each strategy's latest '<strategy_type> ... skipped reason=...' line (they all share that
    shape in run_entry) so the missed-entry alert can name the real reason instead of 'see logs'."""

    def filter(self, record: logging.LogRecord) -> bool:
        head, sep, rest = record.getMessage().partition(" skipped reason=")
        if sep:
            _LAST_SKIP[head.split(" ")[0]] = (datetime.now(_IST).date(), rest)
        return True


_LOG.addFilter(_SkipRecorder())


def _alert(
    desk: SellerDesk,
    *,
    is_paper: bool,
    reason: str,
    strategy_type: str = SHORT_PREMIUM_TYPE,
    title: str = "",
) -> None:
    symbol = (
        str(_settings.short_premium2().get("underlying", "SENSEX")).upper()
        if strategy_type == SHORT_PREMIUM2_TYPE
        else "NIFTY"
    )
    try:
        notify(
            EventKind.STRATEGY_ERROR,
            is_paper=is_paper,
            symbol=symbol,
            segment=Segment.OPTIONS,
            strategy=strategy_type,
            reason=reason,
            title=title,
        )
    except Exception:  # noqa: BLE001 -- notify() must never break the trade path
        pass


def size_lots(
    cfg: ShortPremiumCfg, income_1lot: list[OptionLeg], equity_key: str | None = None
) -> tuple[int, str]:
    """Lots to trade, derived from the PERCENTAGE budget so it scales with equity -- no rupee constants.

    config/capital.json options.max_loss_per_day_per_strategy (% of live equity) is this strategy's own
    daily kill. The all-legs-stopped loss (sl_defined_max_loss) of ``lots`` lots must fit within
    ``risk_fraction_of_daily_cap`` of it, else a normal double stop would trip the kill and force-close the
    rest of the day. ``lots`` > 0 in the config overrides (fixed count, still capped by max_lots);
    returns (0, reason) when not even one lot fits (refused, never silently resized).
    ``equity_key`` (system.per_broker_equity, a live entry): that broker's own equity; unknown
    refuses the entry."""
    if cfg.lots > 0:
        return min(cfg.lots, cfg.max_lots), f"fixed lots={cfg.lots}"
    from src.risk.budget import get_budget, get_current_equity  # noqa: PLC0415

    equity = get_current_equity() if equity_key is None else get_current_equity(equity_key)
    if equity_key is not None and equity <= 0:
        return 0, f"broker equity unknown for {equity_key}"
    cap = get_budget().options.max_loss_per_day_per_strategy_rupees(equity)
    if cap <= 0:
        return 1, "no per-strategy daily cap configured -> 1 lot"
    per_lot = sl_defined_max_loss(income_1lot, cfg) / max(income_1lot[0].lots, 1)
    if per_lot <= 0:
        return 0, "degenerate per-lot risk"
    n = int(cfg.risk_fraction_of_daily_cap * cap // per_lot)
    detail = f"{cfg.risk_fraction_of_daily_cap:g} x daily cap Rs{cap:.0f} / Rs{per_lot:.0f} per lot = {n}"
    return (min(n, cfg.max_lots), detail) if n >= 1 else (0, "capital too small: " + detail)


async def _fetch_chain(
    desk: SellerDesk, underlying: str, today: date, *, expiry_only: bool = False
) -> OptionChain | None:
    """The chain to trade: TODAY's expiry when today is an expiry day, else the nearest one.

    fetch_tier1() cannot be trusted on an expiry day: Kite stamps expiry as a bare date (00:00), so the
    chain builder's rule (expiry - now).days >= 0 is false at 10:30 on the expiry itself and it hands back
    NEXT week's chain (found by the paper-day run, 2026-10-12). The tested strategy trades the nearest
    weekly INCLUDING expiry day (26% of days), so ask for today's expiry first -- fetch_for_expiry returns
    None when today is not an expiry."""
    fetcher = desk._chain_fetcher
    try:
        chain = await fetcher.fetch_for_expiry(underlying, today)
    except Exception as exc:  # noqa: BLE001 -- fall back to the nearest-expiry chain, never abort the entry
        _LOG.warning("options_short_premium_expiry_chain_failed err=%s", exc)
        chain = None
    if (
        chain is not None or expiry_only
    ):  # expiry-only: no chain for today means today is not an expiry
        return chain
    return await fetcher.fetch_tier1(underlying)


async def run_entry(
    desk: SellerDesk,
    cfg: ShortPremiumCfg,
    underlying: str,
    *,
    today: date,
    now: datetime,
    available_margin: float,
    regime: str,
    vix: float | None,
    crm: Any,
    crm_ctx: Any,
) -> OpenPosition | None:
    """One entry attempt. Returns the opened position or None (every skip is logged)."""
    from src.options.seller_crm_adapter import build_seller_crm_signal  # noqa: PLC0415
    from src.options.seller_desk import _net_premium  # noqa: PLC0415

    name, tag = cfg.registry_name, f"{cfg.strategy_type} underlying={underlying}"
    attempts_attr, entered_attr, _ = _state_attrs(cfg.strategy_type)
    if crm is None or crm_ctx is None:
        _LOG.warning("%s skipped reason=crm_unavailable (fail-closed)", tag)
        return None
    if today.weekday() in cfg.skip_weekdays:
        _LOG.info("%s skipped reason=skip_weekday weekday=%s", tag, today.strftime("%a"))
        return None
    if not in_entry_window(now, cfg):
        return None
    mode_paper = desk.strategy_is_paper(name, crm_ctx)
    if not mode_paper and desk.live_route_rejection(name, crm_ctx):
        return None
    route_broker = desk.strategy_broker(name, mode_paper, crm_ctx)
    executor = desk._executor_for(mode_paper, route_broker)
    if executor is None:
        _LOG.error(
            "%s skipped reason=mode_not_wired mode=%s", tag, "paper" if mode_paper else "live"
        )
        return None
    if _today_position_exists(desk, today, cfg.strategy_type):
        return None
    if desk._repo is not None:
        try:
            if await desk._repo.has_position_opened_on(
                strategy_type=cfg.strategy_type, is_paper=mode_paper, day=today
            ):
                _LOG.info("%s skipped reason=already_traded_today", tag)
                return None
        except Exception as exc:  # noqa: BLE001 -- unknown history = do not risk a duplicate entry
            _LOG.error("%s skipped reason=history_check_failed err=%s (fail-closed)", tag, exc)
            return None
    attempts = getattr(desk, attempts_attr).get(today, 0)
    if attempts >= _MAX_ENTRY_ATTEMPTS:
        return None
    # ONLY the real safety breakers apply -- no open-position / trade-count / exposure / slot caps.
    blocked = desk.entries_blocked_reason(today, available_margin, mode_paper)
    if blocked is not None:
        _LOG.warning("%s skipped reason=entries_blocked detail=%s", tag, blocked)
        return None
    halted = desk.strategy_halted_reason(name, today, mode_paper)
    if halted is not None:
        _LOG.warning("%s skipped reason=%s", tag, halted)
        return None
    if cfg.respect_event_blackout:
        blackout = await desk._event_calendar.selling_blocked(underlying, now)
        if blackout.blocked:
            _LOG.info("%s skipped reason=event_blackout detail=%s", tag, blackout.reason)
            return None
    chain = await _fetch_chain(desk, underlying, today, expiry_only=cfg.expiry_only)
    if chain is None:
        _LOG.info(
            "%s skipped reason=%s",
            tag,
            "not_expiry_day" if cfg.expiry_only else "no_chain_data",
        )
        return None
    if (
        not cfg.expiry_only
    ):  # expiry-day ATM IV is inflated; it must not feed the desk's shared IV state
        desk._observe_chain(underlying, chain, today)
    getattr(desk, attempts_attr)[today] = attempts + 1
    why: list[str] = []
    live_chain: OptionChain = chain  # narrowed above; a closure loses mypy's narrowing

    def _build(n_lots: int) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
        built = build_short_premium_legs(
            live_chain,
            n_lots,
            ce_offset=cfg.ce_offset,
            pe_offset=cfg.pe_offset,
            wing_points=cfg.wing_points,
            min_oi=cfg.min_oi,
            min_volume=cfg.min_volume,
            max_spread_pct=cfg.max_spread_pct,
            max_spread_points=cfg.max_spread_points,
            why=why,
        )
        if built is None:
            return None
        # Every position of this engine is flat by its same-day exit_time, so it goes as MIS:
        # same F&O margin as NRML, plus the broker's own square-off if our exit never happens.
        protection, income = built
        return (
            [replace(leg, product="INTRADAY") for leg in protection],
            [replace(leg, product="INTRADAY") for leg in income],
        )

    probe = _build(1)
    if probe is None:
        _LOG.info("%s skipped reason=structure_build_failed detail=%s", tag, "; ".join(why) or "?")
        return None
    lots, size_note = size_lots(cfg, probe[1], desk._equity_key(mode_paper, route_broker))
    if lots < 1:
        _LOG.warning("%s skipped reason=sizing_refused detail=%s", tag, size_note)
        return None
    built = _build(lots)
    if built is None:
        _LOG.info("%s skipped reason=structure_build_failed detail=%s", tag, "; ".join(why) or "?")
        return None
    protection, income = built
    thin = (
        [leg.tradingsymbol for leg in income if leg.premium < cfg.min_leg_premium]
        if cfg.min_leg_premium > 0
        else []
    )
    if thin:
        _LOG.info("%s skipped reason=leg_premium_below_min legs=%s", tag, thin)
        return None
    net_premium = _net_premium(protection, income)
    max_loss = sl_defined_max_loss(income, cfg)
    if net_premium <= 0.0 or max_loss <= 0.0:
        _LOG.info(
            "%s skipped reason=degenerate_economics credit=%.0f max_loss=%.0f",
            tag,
            net_premium,
            max_loss,
        )
        return None
    signal_id = f"SELLER-{uuid.uuid4().hex[:10].upper()}"
    try:
        margin = await executor.get_order_margin_for_structure(
            protection, income, f"{signal_id}-SIZING"
        )
    except Exception as exc:  # noqa: BLE001 -- never size/enter on a fake margin number
        _LOG.error("%s skipped reason=margin_fetch_failed err=%s", tag, exc)
        return None
    # Auto-sizing (lots=0) sizes from the loss budget only, so on a thin-margin day it can ask for more
    # lots than the account can margin (2026-10-19: 5 lots = Rs12.6L vs Rs11.6L free -> refused, no trade).
    # Step down to the largest count that fits instead. A fixed lots count is never silently resized.
    sized_lots = lots
    while margin.total_margin > available_margin and cfg.lots == 0 and lots > 1:
        lots = max(1, min(lots - 1, int(lots * available_margin / margin.total_margin)))
        built = _build(lots)
        if built is None:
            _LOG.info(
                "%s skipped reason=structure_build_failed detail=%s", tag, "; ".join(why) or "?"
            )
            return None
        protection, income = built
        net_premium = _net_premium(protection, income)
        max_loss = sl_defined_max_loss(income, cfg)
        try:
            margin = await executor.get_order_margin_for_structure(
                protection, income, f"{signal_id}-SIZING"
            )
        except Exception as exc:  # noqa: BLE001 -- never size/enter on a fake margin number
            _LOG.error("%s skipped reason=margin_fetch_failed err=%s", tag, exc)
            return None
    if lots != sized_lots:
        size_note += (
            f"; margin-fit {sized_lots}->{lots} lots (free margin Rs{available_margin:.0f})"
        )
    if margin.total_margin <= 0.0 or margin.total_margin < max_loss:
        _LOG.error(
            "%s skipped reason=implausible_margin real=%.0f sl_max_loss=%.0f",
            tag,
            margin.total_margin,
            max_loss,
        )
        return None
    if margin.total_margin > available_margin:
        _LOG.warning(
            "%s skipped reason=insufficient_margin need=%.0f have=%.0f",
            tag,
            margin.total_margin,
            available_margin,
        )
        return None
    _LOG.info(
        "%s candidate mode=%s lots=%d (%s) atm_ce=%g atm_pe=%g wings=%g credit=%.0f "
        "sl_max_loss=%.0f margin=%.0f vix=%s",
        tag,
        "paper" if mode_paper else "live",
        lots,
        size_note,
        income[1].strike,
        income[0].strike,
        cfg.wing_points,
        net_premium,
        max_loss,
        margin.total_margin,
        vix,
    )
    crm_signal = build_seller_crm_signal(
        signal_id=signal_id,
        underlying=underlying,
        strategy_type=cfg.strategy_type,
        lots=lots,
        max_loss=max_loss,
        max_profit=net_premium,
        regime=regime,
        vix=vix if vix is not None else 15.0,
        registry_name=name,
    )
    crm_signal.strategy_name = name
    if not desk._crm_precheck(crm, crm_signal, crm_ctx, lots, underlying):
        return None
    if not await desk._g9_review(
        crm_signal,
        crm_ctx,
        underlying=underlying,
        strategy_type=cfg.strategy_type,
        regime=regime,
        max_loss=max_loss,
        max_profit=net_premium,
        lots=lots,
        vix=vix,
    ):
        return None
    desk._stamp_open_strategies(crm_ctx)
    decision = crm.evaluate(crm_signal, crm_ctx)
    from src.risk.gate_log import log_gate_decisions  # noqa: PLC0415

    log_gate_decisions(crm_signal, decision)
    if not decision.approved or decision.final_quantity < lots:
        _LOG.info(
            "%s skipped reason=crm gate=%s detail=%s",
            tag,
            decision.blocking_gate,
            decision.blocking_reason,
        )
        return None
    _hint(executor, {leg.tradingsymbol: leg.premium for leg in protection + income})
    position = await desk._execute_and_record(
        underlying=underlying,
        strategy_type=cfg.strategy_type,
        protection_legs=protection,
        income_legs=income,
        lots=lots,
        chain=chain,
        net_premium=net_premium,
        max_loss=max_loss,
        max_profit=net_premium,
        direction="credit",
        signal_id=signal_id,
        regime=regime,
        today=today,
        now=now,
        is_paper=mode_paper,
        skip_shared_risk=True,
        broker=route_broker,
    )
    if position is None:
        return None
    setattr(desk, entered_attr, today)
    position.meta = {
        "sl_pct": {"CE": cfg.sl_pct_ce, "PE": cfg.sl_pct_pe},
        "exit_time": f"{cfg.exit_time:%H:%M}",
        "target_pct": cfg.target_pct,
        "ce_offset": cfg.ce_offset,
        "pe_offset": cfg.pe_offset,
        "wing_points": cfg.wing_points,
    }
    await _persist_meta(desk, position)  # SL%/exit time frozen at entry, before anything slow
    if not crm_ctx.llm_gate_required.get(name, False) and cfg.llm_shadow:
        # Background task: the LLM call can take up to 30s and this coroutine is the same one
        # that polls the per-leg stops every few seconds -- it must never wait for the LLM.
        task = asyncio.create_task(
            _shadow_and_persist(
                desk, crm_signal, position, underlying, regime, max_loss, net_premium, lots, vix
            )
        )
        desk._short_premium_bg.add(task)
        task.add_done_callback(desk._short_premium_bg.discard)
    return position


async def _shadow_and_persist(
    desk: SellerDesk,
    crm_signal: Any,
    position: OpenPosition,
    underlying: str,
    regime: str,
    max_loss: float,
    max_profit: float,
    lots: int,
    vix: float | None,
) -> None:
    await _shadow_verdict(
        desk, crm_signal, position, underlying, regime, max_loss, max_profit, lots, vix
    )
    if "llm_shadow" in position.meta:
        await _persist_meta(desk, position)


async def _shadow_verdict(
    desk: SellerDesk,
    crm_signal: Any,
    position: OpenPosition,
    underlying: str,
    regime: str,
    max_loss: float,
    max_profit: float,
    lots: int,
    vix: float | None,
) -> None:
    """LLM gate off -> still ask the LLM once AFTER the order (never delays/blocks entry) and keep its
    verdict on the position, so paper trades can later be bucketed 'LLM would have vetoed' vs not."""
    try:
        verdict = await desk._g9_shadow(
            crm_signal,
            underlying=underlying,
            regime=regime,
            max_loss=max_loss,
            max_profit=max_profit,
            lots=lots,
            vix=vix,
        )
    except Exception as exc:  # noqa: BLE001 -- shadow must never affect a live position
        _LOG.warning("options_intraday_short_premium_shadow_failed err=%s", exc)
        return
    if verdict:
        position.meta["llm_shadow"] = verdict


async def _persist_meta(desk: SellerDesk, position: OpenPosition) -> None:
    if desk._repo is None:
        return
    try:
        await desk._repo.set_legs_meta(position.position_id, position.meta)
    except Exception as exc:  # noqa: BLE001 -- in-memory meta still holds; SL falls back to config
        _LOG.error(
            "options_intraday_short_premium_meta_persist_failed id=%s err=%s",
            position.position_id,
            exc,
        )


async def _close_one_leg(
    desk: SellerDesk,
    pos: OpenPosition,
    leg: OptionLeg,
    ltp: float | None,
    now: datetime,
    reason: str,
    trigger: float | None,
) -> bool:
    """Close ONE leg. The mechanics (record the exit, persist, realised-P&L Telegram alert) live in
    SellerDesk.close_single_leg so every strategy gets them; this only alerts when the broker call fails
    (the leg stays open and is retried next tick)."""
    ok = await desk.close_single_leg(pos, leg, ltp=ltp, now=now, reason=reason, trigger=trigger)
    if not ok:
        _alert(
            desk,
            is_paper=desk._pos_is_paper(pos),
            reason=f"close failed {leg.tradingsymbol} (will retry)",
            **_alert_kw(pos.strategy_type),
        )
    return ok


async def _check_target(
    desk: SellerDesk,
    pos: OpenPosition,
    prices: dict[str, float],
    now: datetime,
    cfg: ShortPremiumCfg | None,
) -> None:
    """Profit target, as a pure percentage. While every leg is open: total P&L vs ``target_pct`` % of the net
    credit received. Once a leg has been closed (per-leg stop) the original target is unreachable -- the
    stopped leg's loss alone can exceed what the remaining leg can still earn -- so it ADJUSTS to the
    legs still open: their live P&L vs ``target_pct`` % of THEIR entry premium. Off when 0. Skipped for a
    tick that has no price for an open leg (a blind mark must not trigger a profit exit)."""
    pct = float(pos.meta.get("target_pct", cfg.target_pct if cfg else 0.0))
    open_legs = pos.income_legs + pos.protection_legs
    if pct <= 0 or not open_legs:
        return
    if any(leg.tradingsymbol not in prices for leg in open_legs):
        return
    pnl = desk._unrealized_pnl(pos, prices)
    if pos.closed_legs:
        pnl -= (
            pos.closed_pnl
        )  # booked loss/profit of the closed legs is not part of the remaining target
        base = open_legs_credit(open_legs)
    else:
        base = abs(pos.entry_premium)
    target = pct / 100.0 * base
    if pnl < target:
        return
    reason = f"SHORT_PREMIUM_TARGET pnl={pnl:.0f} >= {target:.0f}"
    _LOG.warning("options_short_premium_target_hit id=%s %s", pos.position_id, reason)
    for leg in list(pos.income_legs + pos.protection_legs):  # shorts first, wings last
        await _close_one_leg(desk, pos, leg, prices.get(leg.tradingsymbol), now, reason, None)


# Hypothesis J (docs/hypotheses/J_short_premium_combined_trail_lock.md): frozen BEFORE any data, so
# constants and not config -- tuning them would turn a pre-registered test into a curve fit.
_TRAIL_ARM_PCT = 30.0
_TRAIL_STEP_PCT = 15.0


async def _trail_lock_shadow(
    desk: SellerDesk, pos: OpenPosition, prices: dict[str, float], now: datetime
) -> None:
    """SHADOW ONLY -- never closes a leg. Combined-MTM trailing lock: arms when total P&L (open + closed
    legs) reaches _TRAIL_ARM_PCT % of the net credit; the floor (peak - _TRAIL_STEP_PCT % of credit) only
    rises; the first armed tick at/below the floor is recorded once in meta["trail_lock_shadow"]."""
    if "trail_lock_shadow" in pos.meta:
        return
    open_legs = pos.income_legs + pos.protection_legs
    if not open_legs or any(leg.tradingsymbol not in prices for leg in open_legs):
        return
    credit = abs(pos.entry_premium)
    if credit <= 0:
        return
    total = desk._unrealized_pnl(pos, prices)
    peak = max(float(pos.meta.get("_trail_peak", total)), total)
    pos.meta["_trail_peak"] = peak
    if peak < _TRAIL_ARM_PCT / 100.0 * credit:
        return
    floor = peak - _TRAIL_STEP_PCT / 100.0 * credit
    if total > floor:
        return
    pos.meta["trail_lock_shadow"] = {
        "at": now.isoformat(),
        "total": round(total, 2),
        "peak": round(peak, 2),
        "floor": round(floor, 2),
        "credit": round(credit, 2),
    }
    _LOG.info(
        "short_premium_trail_lock_shadow id=%s total=%.0f peak=%.0f floor=%.0f credit=%.0f",
        pos.position_id,
        total,
        peak,
        floor,
        credit,
    )
    await _persist_meta(desk, pos)


async def manage(
    desk: SellerDesk,
    today: date,
    now: datetime,
    prices: dict[str, float],
    cfg: ShortPremiumCfg | None,
    strategy_type: str = SHORT_PREMIUM_TYPE,
) -> None:
    """Per-leg stops + time exit for every open position of ``strategy_type`` run by this engine (also with the
    strategy disabled: an open position is always managed to its exit)."""
    for pos in [p for p in desk.positions.values() if is_strangle_position(p, strategy_type)]:
        exit_str = str(pos.meta.get("exit_time") or (f"{cfg.exit_time:%H:%M}" if cfg else "15:01"))
        exit_dt = datetime.combine(today, _hhmm(exit_str, "exit_time"), tzinfo=_IST)
        stale = pos.entered_at.astimezone(_IST).date() < today
        if stale or now >= exit_dt:
            why = "SHORT_PREMIUM_STALE_CLOSE" if stale else f"SHORT_PREMIUM_TIME_EXIT {exit_str}"
            for leg in list(pos.income_legs + pos.protection_legs):
                await _close_one_leg(desk, pos, leg, prices.get(leg.tradingsymbol), now, why, None)
        else:
            sl_map = pos.meta.get("sl_pct") or {}
            missing = 0
            for leg in list(pos.income_legs):
                ltp = prices.get(leg.tradingsymbol)
                if ltp is None:
                    missing += 1
                    continue
                default_sl = sl_pct_for(cfg, leg.instrument_type) if cfg else 23.0
                sl = float(sl_map.get(leg.instrument_type, default_sl))
                trigger = stop_trigger(leg.premium, sl)
                if ltp >= trigger:
                    await _close_one_leg(
                        desk, pos, leg, ltp, now, f"SHORT_PREMIUM_SL {sl:g}%", trigger
                    )
            await _check_target(desk, pos, prices, now, cfg)
            if strategy_type == SHORT_PREMIUM_TYPE:
                try:
                    await _trail_lock_shadow(desk, pos, prices, now)
                except Exception as exc:  # noqa: BLE001 -- a shadow must never affect the position
                    _LOG.warning("short_premium_trail_lock_shadow_failed err=%s", exc)
            if not pos.income_legs:  # every short is out: the protective wings have no job left
                for leg in list(pos.protection_legs):
                    await _close_one_leg(
                        desk,
                        pos,
                        leg,
                        prices.get(leg.tradingsymbol),
                        now,
                        "SHORT_PREMIUM_SL_WINGS",
                        None,
                    )
            desk._short_premium_missing[pos.position_id] = (
                desk._short_premium_missing.get(pos.position_id, 0) + 1 if missing else 0
            )
            if desk._short_premium_missing.get(pos.position_id, 0) == _MISSING_LTP_ALERT_TICKS:
                _alert(
                    desk,
                    is_paper=desk._pos_is_paper(pos),
                    reason=f"no LTP for {missing} leg(s) for {_MISSING_LTP_ALERT_TICKS} ticks -- stops are blind",
                    **_alert_kw(strategy_type),
                )
        if not pos.income_legs and not pos.protection_legs and pos.position_id in desk.positions:
            reasons = {c.get("reason", "") for c in pos.closed_legs}
            await desk._close_position(
                pos.position_id,
                reason=(
                    "SHORT_PREMIUM_TIME_EXIT"
                    if any(
                        r.startswith(("SHORT_PREMIUM_TIME", "SHORT_PREMIUM_STALE")) for r in reasons
                    )
                    else "SHORT_PREMIUM_TARGET"
                    if any(r.startswith("SHORT_PREMIUM_TARGET") for r in reasons)
                    else "SHORT_PREMIUM_ALL_LEGS_STOPPED"
                ),
            )
    # an exit that could not complete past the strategy's time keeps retrying every tick; escalate once
    late = datetime.combine(today, dtime(15, 10), tzinfo=_IST)
    if now >= late:
        for pos in [p for p in desk.positions.values() if is_strangle_position(p, strategy_type)]:
            if desk._short_premium_late_alert.get(pos.position_id) is None:
                desk._short_premium_late_alert[pos.position_id] = True
                _alert(
                    desk,
                    is_paper=desk._pos_is_paper(pos),
                    reason="position still open after 15:10 -- guardian backstop at 15:15",
                    **_alert_kw(strategy_type),
                )


def _hint(executor: Any, prices: dict[str, float]) -> None:
    """Give a PAPER executor the prices a decision was just made on (no Kite round trip); no-op otherwise."""
    fn = getattr(executor, "hint_prices", None)
    if callable(fn):
        fn(prices)


async def _ltp(broker: Any, symbols: list[str]) -> dict[str, float]:
    """Price read on the time-critical lane when the broker offers one (Kite/paper), else plain get_ltp."""
    fn = getattr(broker, "priority_ltp", None) or broker.get_ltp
    return await fn(symbols)  # type: ignore[no-any-return]


async def tick(shared: dict[str, Any], now: datetime) -> None:
    """One loop iteration for each strategy this engine runs: the daily short premium, its second instance and
    the expiry-day strangle. One failing must not stop the others."""
    for strategy_type, cfg in (
        (SHORT_PREMIUM_TYPE, load_cfg()),
        (SHORT_PREMIUM2_TYPE, load_cfg2()),
        (EXPIRY_STRANGLE_TYPE, load_expiry_cfg()),
    ):
        try:
            await _tick_one(shared, now, strategy_type, cfg)
        except Exception as exc:  # noqa: BLE001 -- isolate the two strategies from each other
            _LOG.warning("%s_tick_failed err=%s", strategy_type, exc)


async def _tick_one(
    shared: dict[str, Any], now: datetime, strategy_type: str, cfg: ShortPremiumCfg | None
) -> None:
    """Manage open positions of ``strategy_type``, then (inside the entry window) try to enter."""
    from src.market.calendar import is_trading_day  # noqa: PLC0415

    seller = shared.get("seller_options")
    desk = seller.get("desk") if seller else None
    broker = shared.get("broker")
    if desk is None or broker is None:
        return
    today = now.date()
    held = [p for p in desk.positions.values() if is_strangle_position(p, strategy_type)]
    if held:
        syms = [leg.tradingsymbol for p in held for leg in p.protection_legs + p.income_legs]
        prices = await _ltp(broker, syms) if syms else {}
        await manage(desk, today, now, prices, cfg, strategy_type)
    if cfg is None or not is_trading_day(today) or not in_entry_window(now, cfg):
        await _maybe_report_missed(desk, cfg, today, now)
        return
    crm_ctx = shared.get("crm_ctx")
    if crm_ctx is not None and not crm_ctx.segment_master_switch.get("options", True):
        return
    if crm_ctx is not None and (
        (crm_ctx.segment_autonomy_mode.get("options") or "autonomous") == "manual"
        or crm_ctx.segment_shadow_mode.get("options", False)
    ):
        return
    reg = shared.get("strategy_registry")
    if reg is None or reg.get(cfg.registry_name) is None:
        _LOG.warning(
            "%s skipped reason=not_in_strategy_registry name=%s",
            cfg.strategy_type,
            cfg.registry_name,
        )
        return
    from src.orchestrator import _hosted_margin  # noqa: PLC0415

    underlying = f"{index_spot_quote_symbol(cfg.underlying).split(':')[0]}:{cfg.underlying}"
    vix_map = await _ltp(broker, ["NSE:INDIA VIX"])
    vix = next(iter(vix_map.values()), None) if vix_map else None
    margin = await _hosted_margin(
        shared, desk, broker, desk.strategy_is_paper(cfg.registry_name, crm_ctx)
    )
    await run_entry(
        desk,
        cfg,
        underlying,
        today=today,
        now=now,
        available_margin=margin,
        regime=desk._last_regime.get(underlying) or "UNDEFINED",
        vix=vix,
        crm=shared.get("crm"),
        crm_ctx=crm_ctx,
    )


async def _maybe_report_missed(
    desk: SellerDesk, cfg: ShortPremiumCfg | None, today: date, now: datetime
) -> None:
    """Once per day, right after the grace window closes with NO entry today: say so loudly. A position
    that was entered and has already closed (quick target / double stop) is NOT a miss -- checked via the
    in-memory entry flag, else the DB (restart safe). An expiry-only strategy has nothing to miss on a day with no
    expiry, so it reports only when an entry attempt was actually made (a chain for today's expiry existed)."""
    if cfg is None or today.weekday() in cfg.skip_weekdays:
        return
    attempts_attr, entered_attr, missed_attr = _state_attrs(cfg.strategy_type)
    if getattr(desk, missed_attr) == today:
        return
    if cfg.expiry_only and not getattr(desk, attempts_attr).get(today):
        return
    end = datetime.combine(today, cfg.entry_time, tzinfo=_IST) + timedelta(
        minutes=cfg.entry_grace_minutes
    )
    if now < end or now > end + timedelta(minutes=30):
        return
    from src.market.calendar import is_trading_day  # noqa: PLC0415

    if not is_trading_day(today):
        return
    if getattr(desk, entered_attr) == today or _today_position_exists(
        desk, today, cfg.strategy_type
    ):
        return
    if desk._repo is not None:
        try:
            if await desk._repo.has_position_opened_on(
                strategy_type=cfg.strategy_type,
                is_paper=desk.strategy_is_paper(cfg.registry_name, None),
                day=today,
            ):
                setattr(desk, entered_attr, today)
                return
        except Exception as exc:  # noqa: BLE001 -- unknown: do not cry wolf, the skip reason is in the logs
            _LOG.warning("options_short_premium_missed_check_failed err=%s", exc)
            return
    setattr(desk, missed_attr, today)
    _LOG.error(
        "%s_missed_entry date=%s -- no position was opened inside the window",
        cfg.strategy_type,
        today,
    )
    last = _LAST_SKIP.get(cfg.strategy_type)
    why = last[1] if last and last[0] == today else "no entry attempt was logged (see logs)"
    _alert(
        desk,
        is_paper=True,
        reason=f"No entry today inside {cfg.entry_time:%H:%M}+{cfg.entry_grace_minutes}m. Last skip: {why}",
        title="NO ENTRY",
        **_alert_kw(cfg.strategy_type),
    )
