"""Every log line about a strategy must START with `strategy=<name> | ` (2026-09-24)."""

from __future__ import annotations

import asyncio
import logging

import pytest
from src.infrastructure.logger import (
    _ConsoleFormatter,
    _FileFormatter,
    _lead_with_strategy,
    strategy_context,
    strategy_logged,
)


def _record(msg: str, *args) -> logging.LogRecord:
    return logging.LogRecord("llm", logging.INFO, __file__, 1, msg, args, None)


def test_existing_mid_line_strategy_field_is_moved_to_the_front() -> None:
    raw = (
        "g9_review_received symbol=NSE:CPPLUS strategy=llm_swing_agent direction=LONG entry=3831.9"
    )
    out = _lead_with_strategy(raw, None)
    assert (
        out
        == "strategy=llm_swing_agent | g9_review_received symbol=NSE:CPPLUS direction=LONG entry=3831.9"
    )


def test_context_strategy_is_added_when_the_line_has_none() -> None:
    out = _lead_with_strategy("g9_llm_error | symbol=NSE:CPPLUS err=budget", "llm_swing_agent")
    assert out.startswith("strategy=llm_swing_agent | g9_llm_error")


def test_line_already_leading_with_the_strategy_is_unchanged() -> None:
    raw = "strategy=llm_swing_agent | already_first x=1"
    assert _lead_with_strategy(raw, "llm_swing_agent") == raw


def test_line_with_its_own_strategy_wins_over_context() -> None:
    out = _lead_with_strategy("x strategy=minervini_vcp y", "llm_swing_agent")
    assert out.startswith("strategy=minervini_vcp | ")
    assert "llm_swing_agent" not in out


def test_no_strategy_anywhere_leaves_the_line_alone() -> None:
    assert _lead_with_strategy("heartbeat ok", None) == "heartbeat ok"


def test_both_formatters_apply_it_so_console_and_files_agree() -> None:
    rec = _record("g9_check sym=%s strategy=%s flag=CONFIRM", "NSE:SONACOMS", "llm_swing_agent")
    for fmt in (_ConsoleFormatter(), _FileFormatter()):
        line = fmt.format(rec)
        assert "| strategy=llm_swing_agent | g9_check sym=NSE:SONACOMS flag=CONFIRM" in line


def test_context_manager_scopes_and_restores() -> None:
    assert _lead_with_strategy("x", None) == "x"
    rec = _record("hello")
    with strategy_context("llm_intraday_agent"):
        assert "strategy=llm_intraday_agent | hello" in _FileFormatter().format(rec)
    assert "strategy=" not in _FileFormatter().format(rec)


@pytest.mark.asyncio
async def test_decorator_fixed_name_covers_async_body_and_worker_threads() -> None:
    seen: list[str] = []

    @strategy_logged(fixed="llm_swing_agent")
    async def loop() -> None:
        seen.append(_FileFormatter().format(_record("in_loop")))
        # asyncio.to_thread copies the context: SDK/worker-thread logs get the tag too
        seen.append(await asyncio.to_thread(lambda: _FileFormatter().format(_record("in_thread"))))

    await loop()
    assert all("strategy=llm_swing_agent | " in s for s in seen)
    assert "strategy=" not in _FileFormatter().format(_record("after"))


@pytest.mark.asyncio
async def test_decorator_reads_strategy_from_a_named_argument() -> None:
    seen: list[str] = []

    @strategy_logged(param="strategy_name")
    async def execute(shared, strategy_name, sym) -> None:
        seen.append(_FileFormatter().format(_record("placing")))

    await execute({}, "minervini_vcp", "NSE:X")
    await execute({}, strategy_name="oneil_canslim", sym="NSE:Y")
    assert "strategy=minervini_vcp | placing" in seen[0]
    assert "strategy=oneil_canslim | placing" in seen[1]


def test_decorator_on_a_sync_function_and_restores_on_exception() -> None:
    @strategy_logged(fixed="llm_intraday_agent")
    def evaluate() -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        evaluate()
    assert "strategy=" not in _FileFormatter().format(_record("after_error"))


def test_gate_lines_keep_their_own_fixed_column_format_untouched() -> None:
    """gates.log lines already carry the strategy as a column (GATE | SEGMENT | strategy |
    SYMBOL | ...); prefixing them again duplicated it and broke the agreed format."""
    line = "GATE | SWING | llm_swing_agent | NSE:SONACOMS | FINAL | APPROVED | qty=48"
    assert _lead_with_strategy(line, "llm_swing_agent") == line
    line2 = "GATE | INTRADAY | llm_intraday_agent | FSL | G3-Time-of-day | IST windows | BLOCK | x"
    assert _lead_with_strategy(line2, None) == line2
    rec = _record(line)
    with strategy_context("llm_swing_agent"):
        out = _FileFormatter().format(rec)
    assert out.endswith("| " + line) and "strategy=" not in out
