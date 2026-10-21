"""Single source of truth for every money limit: config/capital.json.

ONE shared risk pool across options / swing / intraday -- no capital sleeves. Every
percentage is of TOTAL account equity (live from the broker), so every derived rupee
value and every lot/qty size grows automatically when equity changes. See
config/capital.json's own "_what_this_means" / "_..._rule" comments for the plain-English
behaviour behind each key; this module only loads, validates and does the arithmetic.

Format rules, enforced at load (not just documented):
  - Percentages are strings "N%" or "N.ddd%", no trailing zero after the decimal point
    (so "0.5%" and "1%" are valid; "0.5", "1.0%" are rejected at startup, naming the key).
  - Rupees are strings "₹N,NN,NNN" (Indian digit grouping), e.g. "₹20,00,000".
A bad format, or a violated invariant (see Invariants below), raises CapitalConfigError
at load time -- the service must refuse to start rather than trade on an ambiguous or
self-contradictory risk setting.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / "config" / "capital.json"
_PCT_RE = re.compile(r"^\d+(\.\d*[1-9])?%$")
_RUPEE_RE = re.compile(r"^₹\d{1,3}(,\d{2})*,\d{3}$|^₹\d{1,3}$")


class CapitalConfigError(ValueError):
    """config/capital.json has a bad value, or a cross-key invariant is violated."""


def parse_pct(value: Any, key: str) -> float:
    """ "0.5%" -> 0.5. Rejects a bare number, a missing %, or a trailing-zero decimal
    ("1.0%") -- every percentage in capital.json must be written in its shortest form."""
    if not isinstance(value, str) or not _PCT_RE.match(value):
        raise CapitalConfigError(
            f'{key}: expected a percentage string like "0.5%" or "1%" (no trailing '
            f'zero, e.g. not "1.0%", and not a bare number like 0.5), got {value!r}'
        )
    return float(value[:-1])


def parse_rupees(value: Any, key: str) -> float:
    """ "₹20,00,000" -> 2000000.0. Requires the ₹ prefix and Indian digit grouping."""
    if not isinstance(value, str) or not _RUPEE_RE.match(value):
        raise CapitalConfigError(
            f'{key}: expected a rupee string like "₹20,00,000" (₹ prefix, Indian '
            f"digit grouping), got {value!r}"
        )
    return float(value[1:].replace(",", ""))


def _pct(block: dict[str, Any], key: str, path: str) -> float:
    if key not in block:
        raise CapitalConfigError(f"{path}.{key}: missing")
    return parse_pct(block[key], f"{path}.{key}")


def _int(block: dict[str, Any], key: str, path: str) -> int:
    v = block.get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise CapitalConfigError(f"{path}.{key}: expected a plain integer count, got {v!r}")
    return v


@dataclass(frozen=True)
class DrawdownStep:
    drawdown_from_start_pct: float
    position_size_pct: float


@dataclass(frozen=True)
class AccountRules:
    fallback_equity_rupees: float
    max_loss_if_everything_open_hits_stop_pct: float
    stop_trading_below_equity_pct: float
    drawdown_steps: tuple[DrawdownStep, ...]
    # Optional account.min_viable_equity_rupees: a live broker whose equity is below this
    # takes no new entries (only with system.per_broker_equity on). 0 / absent = disabled.
    min_viable_equity_rupees: float = 0.0

    def max_loss_if_everything_open_hits_stop_rupees(self, equity: float) -> float:
        return equity * self.max_loss_if_everything_open_hits_stop_pct / 100.0


@dataclass(frozen=True)
class OptionsRules:
    max_structure_loss_per_strategy_pct: float
    max_loss_per_day_per_strategy_pct: float
    max_loss_per_day_all_strategies_pct: float
    max_share_of_shared_risk_pct: float
    max_open_strategies: int
    max_open_strategies_per_index: int
    max_open_same_direction_spreads: int

    def max_structure_loss_per_strategy_rupees(self, equity: float) -> float:
        return equity * self.max_structure_loss_per_strategy_pct / 100.0

    def max_loss_per_day_per_strategy_rupees(self, equity: float) -> float:
        return equity * self.max_loss_per_day_per_strategy_pct / 100.0

    def max_loss_per_day_all_strategies_rupees(self, equity: float) -> float:
        return equity * self.max_loss_per_day_all_strategies_pct / 100.0

    def max_share_of_shared_risk_rupees(self, equity: float) -> float:
        return equity * self.max_share_of_shared_risk_pct / 100.0


@dataclass(frozen=True)
class SwingRules:
    max_loss_per_trade_pct: float
    max_share_of_shared_risk_pct: float
    max_open_trades: int
    max_new_trades_per_day: int
    max_new_trades_per_day_per_strategy: int
    cash_buffer_pct: float

    def max_loss_per_trade_rupees(self, equity: float) -> float:
        return equity * self.max_loss_per_trade_pct / 100.0

    def max_share_of_shared_risk_rupees(self, equity: float) -> float:
        return equity * self.max_share_of_shared_risk_pct / 100.0


@dataclass(frozen=True)
class IntradayRules:
    max_loss_per_trade_pct: float
    max_share_of_shared_risk_pct: float
    max_loss_per_day_pct: float
    close_all_on_daily_loss: bool
    max_open_trades: int
    max_new_trades_per_day: int
    max_new_trades_per_day_per_strategy: int

    def max_loss_per_trade_rupees(self, equity: float) -> float:
        return equity * self.max_loss_per_trade_pct / 100.0

    def max_share_of_shared_risk_rupees(self, equity: float) -> float:
        return equity * self.max_share_of_shared_risk_pct / 100.0

    def max_loss_per_day_rupees(self, equity: float) -> float:
        return equity * self.max_loss_per_day_pct / 100.0


@dataclass(frozen=True)
class RiskBudget:
    """The whole validated config/capital.json. One instance per load; immutable, so a
    reload always produces a fresh object rather than mutating one shared across threads."""

    account: AccountRules
    options: OptionsRules
    swing: SwingRules
    intraday: IntradayRules

    def log_line(self, equity: float) -> str:
        """One line naming every derived rupee value, for the startup/every-cycle log
        (capital.json's own documented contract) -- the operator should never have to
        compute a % by hand to know what a trade will actually be capped at."""
        o, s, i, a = self.options, self.swing, self.intraday, self.account
        return (
            f"risk_budget equity=₹{equity:,.0f} "
            f"account.shared_risk_cap=₹{a.max_loss_if_everything_open_hits_stop_rupees(equity):,.0f} "
            f"options.per_strategy_structure=₹{o.max_structure_loss_per_strategy_rupees(equity):,.0f} "
            f"options.per_strategy_daily=₹{o.max_loss_per_day_per_strategy_rupees(equity):,.0f} "
            f"options.all_strategies_daily=₹{o.max_loss_per_day_all_strategies_rupees(equity):,.0f} "
            f"options.shared_risk_ceiling=₹{o.max_share_of_shared_risk_rupees(equity):,.0f} "
            f"swing.per_trade=₹{s.max_loss_per_trade_rupees(equity):,.0f} "
            f"swing.shared_risk_ceiling=₹{s.max_share_of_shared_risk_rupees(equity):,.0f} "
            f"intraday.per_trade=₹{i.max_loss_per_trade_rupees(equity):,.0f} "
            f"intraday.shared_risk_ceiling=₹{i.max_share_of_shared_risk_rupees(equity):,.0f} "
            f"intraday.daily=₹{i.max_loss_per_day_rupees(equity):,.0f}"
        )


def _load_account(raw: dict[str, Any]) -> AccountRules:
    path = "account"
    steps = raw.get("drawdown_steps")
    if not isinstance(steps, list) or not steps:
        raise CapitalConfigError(f"{path}.drawdown_steps: missing or empty")
    parsed_steps = tuple(
        DrawdownStep(
            drawdown_from_start_pct=parse_pct(
                s.get("drawdown_from_start"), f"{path}.drawdown_steps[{idx}].drawdown_from_start"
            ),
            position_size_pct=parse_pct(
                s.get("position_size_becomes"),
                f"{path}.drawdown_steps[{idx}].position_size_becomes",
            ),
        )
        for idx, s in enumerate(steps)
    )
    return AccountRules(
        fallback_equity_rupees=parse_rupees(
            raw.get("fallback_equity_if_broker_unavailable"),
            f"{path}.fallback_equity_if_broker_unavailable",
        ),
        max_loss_if_everything_open_hits_stop_pct=_pct(
            raw, "max_loss_if_everything_open_hits_stop", path
        ),
        stop_trading_below_equity_pct=_pct(raw, "stop_all_trading_if_equity_falls_below", path),
        drawdown_steps=parsed_steps,
        min_viable_equity_rupees=(
            parse_rupees(raw["min_viable_equity_rupees"], f"{path}.min_viable_equity_rupees")
            if raw.get("min_viable_equity_rupees") is not None
            else 0.0
        ),
    )


def _load_options(raw: dict[str, Any]) -> OptionsRules:
    path = "options"
    return OptionsRules(
        max_structure_loss_per_strategy_pct=_pct(raw, "max_structure_loss_per_strategy", path),
        max_loss_per_day_per_strategy_pct=_pct(raw, "max_loss_per_day_per_strategy", path),
        max_loss_per_day_all_strategies_pct=_pct(
            raw, "max_loss_per_day_all_options_strategies", path
        ),
        max_share_of_shared_risk_pct=_pct(raw, "max_share_of_shared_risk", path),
        max_open_strategies=_int(raw, "max_open_strategies", path),
        max_open_strategies_per_index=_int(raw, "max_open_strategies_per_index", path),
        max_open_same_direction_spreads=_int(raw, "max_open_same_direction_spreads", path),
    )


def _load_swing(raw: dict[str, Any]) -> SwingRules:
    path = "swing"
    if "max_loss_per_day" in raw:
        raise CapitalConfigError(
            "swing.max_loss_per_day: not allowed. Swing has no daily loss limit by "
            "design (operator decision 2026-10-06) -- each trade lives by its own "
            "stop-loss and target. Remove this key."
        )
    return SwingRules(
        max_loss_per_trade_pct=_pct(raw, "max_loss_per_trade", path),
        max_share_of_shared_risk_pct=_pct(raw, "max_share_of_shared_risk", path),
        max_open_trades=_int(raw, "max_open_trades", path),
        max_new_trades_per_day=_int(raw, "max_new_trades_per_day", path),
        max_new_trades_per_day_per_strategy=_int(raw, "max_new_trades_per_day_per_strategy", path),
        cash_buffer_pct=_pct(raw, "keep_unused_cash_buffer", path),
    )


def _load_intraday(raw: dict[str, Any]) -> IntradayRules:
    path = "intraday"
    return IntradayRules(
        max_loss_per_trade_pct=_pct(raw, "max_loss_per_trade", path),
        max_share_of_shared_risk_pct=_pct(raw, "max_share_of_shared_risk", path),
        max_loss_per_day_pct=_pct(raw, "max_loss_per_day", path),
        close_all_on_daily_loss=bool(raw.get("close_all_positions_when_daily_loss_hit", False)),
        max_open_trades=_int(raw, "max_open_trades", path),
        max_new_trades_per_day=_int(raw, "max_new_trades_per_day", path),
        max_new_trades_per_day_per_strategy=_int(raw, "max_new_trades_per_day_per_strategy", path),
    )


def _check_invariants(b: RiskBudget) -> None:
    """Cross-key rules from the approved design -- a violation means the config is
    self-contradictory (a daily limit that can't stop a single trade, a segment ceiling
    smaller than its own per-trade risk, ...), so startup refuses rather than trade
    under a limit that doesn't limit anything."""
    o, s, i, a = b.options, b.swing, b.intraday, b.account

    if o.max_loss_per_day_per_strategy_pct >= o.max_structure_loss_per_strategy_pct:
        raise CapitalConfigError(
            "options.max_loss_per_day_per_strategy must be LESS than "
            "max_structure_loss_per_strategy, so the daily stop fires before a "
            f"structure's own max loss ({o.max_loss_per_day_per_strategy_pct}% >= "
            f"{o.max_structure_loss_per_strategy_pct}%)"
        )
    if o.max_loss_per_day_per_strategy_pct > o.max_loss_per_day_all_strategies_pct:
        raise CapitalConfigError(
            "options.max_loss_per_day_per_strategy must be <= "
            "max_loss_per_day_all_options_strategies "
            f"({o.max_loss_per_day_per_strategy_pct}% > {o.max_loss_per_day_all_strategies_pct}%)"
        )
    if o.max_loss_per_day_all_strategies_pct > o.max_share_of_shared_risk_pct:
        raise CapitalConfigError(
            "options.max_loss_per_day_all_options_strategies must be <= "
            "max_share_of_shared_risk "
            f"({o.max_loss_per_day_all_strategies_pct}% > {o.max_share_of_shared_risk_pct}%)"
        )
    if i.max_loss_per_trade_pct > i.max_loss_per_day_pct / 2:
        raise CapitalConfigError(
            "intraday.max_loss_per_trade must be <= max_loss_per_day / 2, so one "
            f"trade can never spend more than half the day's budget "
            f"({i.max_loss_per_trade_pct}% > {i.max_loss_per_day_pct / 2}%)"
        )
    for name, per_trade_pct, ceiling_pct in (
        ("options", o.max_structure_loss_per_strategy_pct, o.max_share_of_shared_risk_pct),
        ("swing", s.max_loss_per_trade_pct, s.max_share_of_shared_risk_pct),
        ("intraday", i.max_loss_per_trade_pct, i.max_share_of_shared_risk_pct),
    ):
        if per_trade_pct > ceiling_pct:
            raise CapitalConfigError(
                f"{name}.max_share_of_shared_risk must be >= its own per-trade/"
                f"per-strategy risk ({ceiling_pct}% < {per_trade_pct}%)"
            )
        if ceiling_pct > a.max_loss_if_everything_open_hits_stop_pct:
            raise CapitalConfigError(
                f"{name}.max_share_of_shared_risk must be <= "
                f"account.max_loss_if_everything_open_hits_stop "
                f"({ceiling_pct}% > {a.max_loss_if_everything_open_hits_stop_pct}%)"
            )


_cached_budget: RiskBudget | None = None
_current_equity: float | None = None
# Per live stack key ("live:<broker>"), only with system.per_broker_equity on.
_equity_by_key: dict[str, float] = {}


def get_budget() -> RiskBudget:
    """Process-wide cached RiskBudget (loaded once, validated at load). Use
    reload_budget() to pick up an edited capital.json without a restart."""
    global _cached_budget
    if _cached_budget is None:
        _cached_budget = load_risk_budget()
    return _cached_budget


def reload_budget() -> RiskBudget:
    global _cached_budget
    _cached_budget = load_risk_budget()
    return _cached_budget


def set_current_equity(value: float, key: str | None = None) -> None:
    """Called wherever live broker equity is refreshed (same cadence as
    crm_ctx.current_equity), so every RiskBudget-derived rupee figure tracks it. With a
    stack `key` it records that one broker's equity instead (per-broker risk)."""
    global _current_equity
    if key is not None:
        _equity_by_key[key] = value
        return
    _current_equity = value


def clear_current_equity(key: str) -> None:
    """Forget one broker's equity (its fetch failed): get_current_equity(key) is 0 again."""
    _equity_by_key.pop(key, None)


def get_current_equity(key: str | None = None) -> float:
    """Live equity if set, else the configured fallback -- callers (e.g. at boot,
    before the first broker refresh) must never divide by an unknown equity.

    With a stack `key`: that broker's last fetched equity, or 0.0 when unknown. There is
    deliberately no fallback figure for a broker -- sizing a real account on an invented
    equity is never safe; the CRM blocks entries for a broker without a fresh equity."""
    if key is not None:
        value = _equity_by_key.get(key, 0.0)
        return value if value > 0 else 0.0
    if _current_equity is not None and _current_equity > 0:
        return _current_equity
    return get_budget().account.fallback_equity_rupees


def load_risk_budget(path: Path | None = None) -> RiskBudget:
    """Parse, format-validate and invariant-check config/capital.json. Raises
    CapitalConfigError naming the offending key on any problem -- the caller (boot)
    should let this propagate and refuse to start, never fall back to a default."""
    p = path or _PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CapitalConfigError(f"{p} not found") from exc
    except json.JSONDecodeError as exc:
        raise CapitalConfigError(f"{p}: invalid JSON: {exc}") from exc

    for top in ("account", "options", "swing", "intraday"):
        if not isinstance(raw.get(top), dict):
            raise CapitalConfigError(f"{top}: missing top-level block")

    budget = RiskBudget(
        account=_load_account(raw["account"]),
        options=_load_options(raw["options"]),
        swing=_load_swing(raw["swing"]),
        intraday=_load_intraday(raw["intraday"]),
    )
    _check_invariants(budget)
    return budget
