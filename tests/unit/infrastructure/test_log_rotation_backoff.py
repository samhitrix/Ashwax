"""Tests for _SafeRotatingFileHandler's rollover back-off.

api.log reached 189MB against a 10MB threshold with zero backup files ever
produced. Two processes (the launcher's stdout mirror and the uvicorn worker) held
the same file open, so the rename in doRollover() failed on Windows every time.
The failure was swallowed and retried on the very next emit, which left the handler
permanently above maxBytes and running a full close/rename/reopen cycle for EVERY
log line — measured at 1.19ms vs 0.033ms, 36x.
"""

from __future__ import annotations

import logging

from src.infrastructure.logger import _SafeRotatingFileHandler


def _handler(path, **kw):
    h = _SafeRotatingFileHandler(str(path), maxBytes=1024, backupCount=2, encoding="utf-8", **kw)
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


def _record() -> logging.LogRecord:
    return logging.LogRecord("t", logging.DEBUG, __file__, 1, "x" * 200, None, None)


def test_rotation_succeeds_when_file_is_not_locked(tmp_path):
    """The normal single-writer case must still roll over and reset the live file."""
    p = tmp_path / "api.log"
    p.write_bytes(b"x" * 2048)  # already past maxBytes
    h = _handler(p)
    try:
        h.emit(_record())
    finally:
        h.close()

    assert (tmp_path / "api.log.1").exists()
    assert p.stat().st_size < 1024


def test_failed_rotation_backs_off_instead_of_retrying_every_emit(tmp_path, monkeypatch):
    p = tmp_path / "api.log"
    p.write_bytes(b"x" * 2048)
    h = _handler(p)

    attempts = []

    def _locked_rotate(self, source, dest):  # noqa: ANN001 - patched onto the class
        attempts.append(1)
        raise PermissionError(32, "file in use by another process")

    # Patch the parent's rotate so _SafeRotatingFileHandler.rotate hits its
    # except-branch, exactly as it does under a real Windows lock.
    monkeypatch.setattr(logging.handlers.RotatingFileHandler, "rotate", _locked_rotate)
    try:
        for _ in range(50):
            h.emit(_record())
    finally:
        h.close()

    # Without the cooldown this was one rotate attempt per emit.
    assert len(attempts) == 1, f"expected a single attempt during cooldown, got {len(attempts)}"


def test_backoff_expires_and_rotation_is_retried(tmp_path, monkeypatch):
    """The cooldown must not disable rotation permanently — once the lock clears,
    the next emit past the threshold rolls the file as normal."""
    p = tmp_path / "api.log"
    p.write_bytes(b"x" * 2048)
    h = _handler(p)

    def _locked_rotate(self, source, dest):  # noqa: ANN001 - patched onto the class
        raise PermissionError(32, "file in use by another process")

    monkeypatch.setattr(logging.handlers.RotatingFileHandler, "rotate", _locked_rotate)
    h.emit(_record())
    assert not (tmp_path / "api.log.1").exists()

    monkeypatch.undo()
    # Simulate the cooldown having elapsed rather than sleeping for it.
    h._rotate_blocked_until = 0.0
    try:
        h.emit(_record())
    finally:
        h.close()

    assert (tmp_path / "api.log.1").exists()
