"""Unit tests for src/infrastructure/logger.py.

Log FILES are pipe-delimited plain text (NOT JSON), identical layout to the
console minus colour codes:

    dd-mm-YYYY HH:MM:SS.mmm | LEVEL | script | message | k=v k=v
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from src.infrastructure.logger import (
    SUCCESS,
    _AshwaxAdapter,
    _FileFormatter,
    _reset_for_testing,
    get_logger,
    setup_logging,
)


@pytest.fixture(autouse=True)  # type: ignore[misc]
def reset_logging(tmp_path: Path) -> Iterator[None]:
    """Reset logger state before and after every test."""
    _reset_for_testing()
    yield
    _reset_for_testing()


def _last_fields(path: Path) -> list[str]:
    """Split the last log line on ' | ' and strip each field."""
    line = path.read_text(encoding="utf-8").splitlines()[-1]
    return [p.strip() for p in line.split(" | ")]


def _master_log(tmp_path: Path) -> Path:
    """The dated master log setup_logging() just created (trading-YYYY-MM-DD.log,
    2026-09-23 design — one file per IST calendar day, not the old fixed
    "trading.log" name)."""
    matches = list(tmp_path.glob("trading-????-??-??.log"))
    assert len(matches) == 1, f"expected exactly one dated master log, found {matches}"
    return matches[0]


# ── setup_logging ─────────────────────────────────────────────────────────────


def test_setup_creates_log_directory(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    setup_logging("test_engine", log_dir=log_dir)
    assert log_dir.exists()


def test_setup_creates_trading_log(tmp_path: Path) -> None:
    # Dated master log (trading-YYYY-MM-DD.log, one per IST calendar day) — not the
    # old fixed "trading.log" name. It exists after setup_logging() because setup
    # itself emits the "logging_initialised" line, which is enough to trigger the
    # delayed file open (see the empty-file tests below for the delay=True case).
    setup_logging("test_engine", log_dir=tmp_path)
    assert _master_log(tmp_path).exists()


def test_component_log_not_created_until_something_is_logged(tmp_path: Path) -> None:
    """2026-09-23: files are delay=True now — setup_logging() alone must NOT create
    any component file. This is the fix for the empty circuit_breaker.log /
    infrastructure.log / etc. files that used to exist just because setup_logging()
    attached a handler, whether or not that component ever logged anything."""
    setup_logging("test_engine", log_dir=tmp_path)
    for name in ("circuit_breaker", "infrastructure", "learning", "shared"):
        assert not (tmp_path / f"{name}.log").exists(), f"{name}.log created with nothing logged"


def test_setup_creates_per_component_logs_once_used(tmp_path: Path) -> None:
    from src.infrastructure.logger import _COMPONENTS

    setup_logging("test_engine", log_dir=tmp_path)
    # One DEBUG file per component, created lazily on first use (delay=True)
    # rather than eagerly at setup. Logger names are the real _COMPONENTS keys
    # (mostly dotted, e.g. "src.execution") -- NOT bare package-name guesses:
    # logging.getLogger("execution") is unrelated to "src.execution" in the
    # logger hierarchy, and a version of this test using bare names passed
    # under the old eager-creation design purely by accident (every file was
    # created regardless of whether the test's names were even right).
    for key in ("src.execution", "src.risk", "src.learning", "src.db", "llm", "orchestrator"):
        assert key in _COMPONENTS, f"{key} no longer in _COMPONENTS -- update this test"
        logging.getLogger(key).info("probe")
        filename = _COMPONENTS[key]
        assert (tmp_path / filename).exists(), f"{filename} not created after logging to {key}"


def test_setup_does_not_create_audit_log_until_used(tmp_path: Path) -> None:
    """delay=True applies to audit too — a day nothing is audited leaves no
    audit-<date>.log file (previously always created at setup)."""
    setup_logging("test_engine", log_dir=tmp_path)
    assert list(tmp_path.glob("audit-*.log")) == []


def test_audit_log_created_on_first_use(tmp_path: Path) -> None:
    setup_logging("test_engine", log_dir=tmp_path)
    logging.getLogger("audit").info("compliance_event")
    audit_files = list(tmp_path.glob("audit-*.log"))
    assert len(audit_files) == 1
    assert re.match(r"audit-\d{4}-\d{2}-\d{2}\.log", audit_files[0].name)


def test_setup_is_idempotent(tmp_path: Path) -> None:
    setup_logging("test_engine", log_dir=tmp_path)
    handler_count = len(logging.getLogger().handlers)
    setup_logging("test_engine", log_dir=tmp_path)  # second call must be no-op
    assert len(logging.getLogger().handlers) == handler_count


# ── get_logger ────────────────────────────────────────────────────────────────


def test_get_logger_returns_adapter() -> None:
    log = get_logger("my_module")
    assert isinstance(log, _AshwaxAdapter)


def test_get_logger_stamps_script_name(tmp_path: Path) -> None:
    setup_logging("test_engine", log_dir=tmp_path)
    get_logger("crm").info("test_event")
    fields = _last_fields(_master_log(tmp_path))
    assert fields[2] == "crm"  # ts | level | script | message


# ── Custom SUCCESS level ──────────────────────────────────────────────────────


def test_success_level_value() -> None:
    assert SUCCESS == 25
    assert logging.getLevelName(SUCCESS) == "SUCCESS"


def test_success_level_written_to_file(tmp_path: Path) -> None:
    setup_logging("test_engine", log_dir=tmp_path)
    logging.getLogger("success_test").success("order_filled")  # type: ignore[attr-defined]
    fields = _last_fields(_master_log(tmp_path))
    assert fields[1] == "SUCCESS"


# ── ctx dict ─────────────────────────────────────────────────────────────────


def test_ctx_appears_in_pipe_detail(tmp_path: Path) -> None:
    setup_logging("test_engine", log_dir=tmp_path)
    get_logger("order_monitor").info(
        "sl_placed", extra={"ctx": {"symbol": "RELIANCE", "sl": 2700.0}}
    )
    fields = _last_fields(_master_log(tmp_path))
    # ts | level | script | message | detail
    assert fields[3] == "sl_placed"
    assert fields[4] == "symbol=RELIANCE sl=2700.0"


def test_no_ctx_has_no_detail_field(tmp_path: Path) -> None:
    setup_logging("test_engine", log_dir=tmp_path)
    get_logger("test_mod").info("simple_event")
    fields = _last_fields(_master_log(tmp_path))
    # ts | level | script | message  — exactly four fields, no trailing detail
    assert len(fields) == 4
    assert fields[3] == "simple_event"


# ── IST timestamp ─────────────────────────────────────────────────────────────


def test_timestamp_format(tmp_path: Path) -> None:
    setup_logging("test_engine", log_dir=tmp_path)
    get_logger("ts_test").info("timing_check")
    fields = _last_fields(_master_log(tmp_path))
    # Matches dd-mm-yyyy HH:MM:SS.mmm
    assert re.match(r"\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2}\.\d{3}", fields[0])


# ── Audit log ────────────────────────────────────────────────────────────────


def test_audit_propagates_to_master_log(tmp_path: Path) -> None:
    """2026-09-23: changed from isolated to propagating. Audit used to be the one
    component excluded from trading.log, so "the master log has everything" was
    not actually true. Audit's OWN file and retention are untouched by this —
    only visibility into the master log changed."""
    setup_logging("test_engine", log_dir=tmp_path)
    logging.getLogger("audit").info("compliance_event")
    fields = _last_fields(_master_log(tmp_path))
    assert fields[3] == "compliance_event"


def test_audit_writes_to_own_file(tmp_path: Path) -> None:
    setup_logging("test_engine", log_dir=tmp_path)
    logging.getLogger("audit").info("order_placed", extra={"ctx": {"order_id": "ORD-1"}})
    audit_file = next(iter(tmp_path.glob("audit-*.log")))
    fields = [p.strip() for p in audit_file.read_text().splitlines()[-1].split(" | ")]
    assert fields[3] == "order_placed"


# ── File formatter (pipe-delimited, never JSON) ───────────────────────────────


# ── Day-boundary rollover (2026-09-23) ────────────────────────────────────────


def test_component_log_resets_on_day_change(tmp_path: Path, monkeypatch) -> None:
    """A component file holds only the CURRENT day. At IST midnight it is deleted
    and reopens empty -- its full record for the old day already reached that
    day's dated master log via propagation, so nothing is lost."""
    import src.infrastructure.logger as logger_mod

    setup_logging("test_engine", log_dir=tmp_path)
    log = logging.getLogger("src.risk")  # real _COMPONENTS key, not bare "risk"
    log.info("before_midnight")
    path = tmp_path / "risk.log"
    assert path.exists()
    assert "before_midnight" in path.read_text(encoding="utf-8")

    monkeypatch.setattr(logger_mod, "_today_ist", lambda: "2099-01-01")
    log.info("after_midnight")
    # Same filename (component files are NOT dated) but reset to just today's line.
    content = path.read_text(encoding="utf-8")
    assert "before_midnight" not in content
    assert "after_midnight" in content


def test_master_log_rolls_to_new_dated_file_on_day_change(tmp_path: Path, monkeypatch) -> None:
    """The master log's rollover RENAMES forward (new dated file) rather than
    deleting -- yesterday's file is the archive, not scratch space."""
    import src.infrastructure.logger as logger_mod

    setup_logging("test_engine", log_dir=tmp_path)
    yesterday_path = _master_log(tmp_path)
    get_logger("crm").info("before_midnight")

    monkeypatch.setattr(logger_mod, "_today_ist", lambda: "2099-01-01")
    get_logger("crm").info("after_midnight")

    assert yesterday_path.exists(), "yesterday's master log must survive the rollover"
    assert "before_midnight" in yesterday_path.read_text(encoding="utf-8")
    assert "after_midnight" not in yesterday_path.read_text(encoding="utf-8")

    today_path = tmp_path / "trading-2099-01-01.log"
    assert today_path.exists()
    assert "after_midnight" in today_path.read_text(encoding="utf-8")


def test_file_formatter_is_pipe_delimited_not_json() -> None:
    fmt = _FileFormatter()
    rec = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test_message",
        args=(),
        exc_info=None,
    )
    line = fmt.format(rec)
    fields = [p.strip() for p in line.split(" | ")]
    assert fields[1] == "INFO"
    assert fields[2] == "test"  # script_name falls back to record.name
    assert fields[3] == "test_message"
    # Must NOT be JSON — no braces, no colour codes
    assert "{" not in line and "}" not in line
    assert "\033" not in line
