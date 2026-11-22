"""Unit tests for src/infrastructure/singleton.py."""

from __future__ import annotations

import os
from pathlib import Path

from src.infrastructure.singleton import (
    _mutex_name,
    _pid_alive,
    _read_pidfile,
    _remove_pidfile,
    _write_pidfile,
)

# ── _mutex_name ───────────────────────────────────────────────────────────────


def test_mutex_name_format() -> None:
    name = _mutex_name("ZQ1234")
    assert name.startswith("Global\\ashwax_")
    assert len(name) == len("Global\\ashwax_") + 12


def test_mutex_name_is_deterministic() -> None:
    assert _mutex_name("ZQ1234") == _mutex_name("ZQ1234")


def test_mutex_names_differ_per_user() -> None:
    assert _mutex_name("USER_A") != _mutex_name("USER_B")


# ── PID file helpers ──────────────────────────────────────────────────────────


def test_write_and_read_pidfile(tmp_path: Path) -> None:
    _write_pidfile(tmp_path)
    assert _read_pidfile(tmp_path) == os.getpid()


def test_read_pidfile_missing_returns_none(tmp_path: Path) -> None:
    assert _read_pidfile(tmp_path) is None


def test_read_pidfile_corrupt_returns_none(tmp_path: Path) -> None:
    (tmp_path / "orchestrator.lock").write_text("not_a_number")
    assert _read_pidfile(tmp_path) is None


def test_remove_pidfile(tmp_path: Path) -> None:
    _write_pidfile(tmp_path)
    _remove_pidfile(tmp_path)
    assert not (tmp_path / "orchestrator.lock").exists()


def test_remove_pidfile_missing_is_safe(tmp_path: Path) -> None:
    _remove_pidfile(tmp_path)  # must not raise


# ── _pid_alive ────────────────────────────────────────────────────────────────


def test_own_pid_is_alive() -> None:
    assert _pid_alive(os.getpid()) is True


def test_nonexistent_pid_is_not_alive() -> None:
    assert _pid_alive(99999999) is False
