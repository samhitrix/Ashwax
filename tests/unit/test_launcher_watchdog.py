"""Launcher liveness watchdog + fixed-port guard."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import launcher
import psutil
import pytest


def test_hung_when_heartbeat_stale_and_orchestrator_past_boot_grace() -> None:
    assert launcher._heartbeat_hung(age_s=2400.0, uptime_s=300.0)


def test_not_hung_during_boot_grace_even_if_old_row_is_stale() -> None:
    # right after a restart the newest heartbeat row is the PREVIOUS process's
    assert not launcher._heartbeat_hung(age_s=2400.0, uptime_s=30.0)


def test_not_hung_when_heartbeat_fresh() -> None:
    assert not launcher._heartbeat_hung(age_s=4.0, uptime_s=3600.0)


def test_never_acts_when_api_unreachable() -> None:
    assert not launcher._heartbeat_hung(age_s=None, uptime_s=3600.0)


def test_restart_guard_window_covers_the_observed_hang_cadence() -> None:
    # 2026-09-24: the orchestrator hung every ~430s for hours. The old 900s window
    # held at most 2 restarts, so the 3-restart boot-loop guard never tripped (30 kills).
    assert launcher._RESTART_WINDOW_S / 430.0 > launcher._MAX_RESTARTS


def test_find_pyspy_prefers_env_then_project_tools(tmp_path, monkeypatch) -> None:
    exe = tmp_path / "py-spy.exe"
    exe.write_bytes(b"x")
    monkeypatch.setenv("ASHWAX_PYSPY", str(exe))
    assert launcher._find_pyspy() == str(exe)
    monkeypatch.delenv("ASHWAX_PYSPY")
    tools = tmp_path / "tools" / "bin"
    tools.mkdir(parents=True)
    (tools / "py-spy.exe").write_bytes(b"x")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    with patch("shutil.which", return_value=None):
        assert launcher._find_pyspy() == str(tools / "py-spy.exe")


def test_find_pyspy_none_when_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ASHWAX_PYSPY", raising=False)
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "nope"))
    with patch("shutil.which", return_value=None):
        assert launcher._find_pyspy() is None


def test_dump_stack_warns_loudly_when_pyspy_missing() -> None:
    log = MagicMock()
    with patch("launcher._find_pyspy", return_value=None):
        launcher._dump_stack(1234, log)
    log.warning.assert_called_once()
    assert "hang_stack_dump_unavailable" in log.warning.call_args.args[0]


def test_push_alert_posts_to_telegram_with_env_credentials(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        sent["url"], sent["body"] = req.full_url, req.data.decode()
        return _Resp()

    with patch("urllib.request.urlopen", fake_urlopen):
        assert launcher._push_alert("orchestrator frozen", MagicMock()) is True
    assert sent["url"] == "https://api.telegram.org/bottok123/sendMessage"
    assert "orchestrator frozen" in sent["body"] and '"chat_id": "42"' in sent["body"]


def test_push_alert_never_raises_and_reports_failure(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    log = MagicMock()
    with patch("urllib.request.urlopen", side_effect=OSError("net down")):
        assert launcher._push_alert("x", log) is False
    log.warning.assert_called_once()
    assert "tok123" not in str(log.warning.call_args)  # token never logged


def test_push_alert_unconfigured_is_logged_not_silent(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(launcher, "ROOT", tmp_path)  # no .env here
    log = MagicMock()
    assert launcher._push_alert("x", log) is False
    log.warning.assert_called_once()


def test_free_port_refuses_unrelated_owner() -> None:
    conn = SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=3000), pid=999_999)
    other = MagicMock()
    other.name.return_value = "postgres.exe"
    with (
        patch("psutil.net_connections", return_value=[conn]),
        patch("psutil.Process", return_value=other),
        pytest.raises(SystemExit, match="port 3000"),
    ):
        launcher._free_port(3000, MagicMock())
    other.kill.assert_not_called()


def test_free_port_kills_stale_own_process() -> None:
    conn = SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=5173), pid=999_999)
    node = MagicMock()
    node.name.return_value = "node.exe"
    node.children.return_value = []
    with (
        patch("psutil.net_connections", return_value=[conn]),
        patch("psutil.Process", return_value=node),
        patch("launcher.time.sleep"),
    ):
        launcher._free_port(5173, MagicMock())
    node.kill.assert_called_once()


# ── pipe reader must never die (2026-09-24 root cause of the ~7-minute freezes) ──


def _run_child_through_stream(child_src: str, stdout_encoding: str = "cp1252"):
    """Run `child_src` as a real subprocess behind a real pipe, drained by launcher._stream,
    with the launcher's own stdout constrained to `stdout_encoding` (NSSM pipes are cp1252)."""
    import io
    import subprocess
    import sys
    import threading

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", child_src], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    fake_stdout = io.TextIOWrapper(io.BytesIO(), encoding=stdout_encoding, errors="strict")
    with patch("sys.stdout", fake_stdout):
        t = threading.Thread(target=launcher._stream, args=(proc, MagicMock()), daemon=True)
        t.start()
        try:
            proc.wait(timeout=20)
            blocked = False
        except subprocess.TimeoutExpired:
            blocked = True
            proc.kill()
        t.join(timeout=5)
    fake_stdout.flush()
    return blocked, t.is_alive(), fake_stdout.buffer.getvalue().decode(stdout_encoding, "replace")


_UTF8_CHILD = (
    "import sys\n"
    "for i in range(3000):\n"
    "    line = '24-09-2026 11:14:10.6 | INFO | x | line %d ' % i + 'y' * 100\n"
    "    if i == 50: line += ' rs_excess ' + chr(0x2212) + '0.06 rupee ' + chr(0x20B9)\n"
    "    sys.stdout.buffer.write((line + chr(10)).encode('utf-8')); sys.stdout.buffer.flush()\n"
    "sys.stdout.buffer.write(b'24-09-2026 11:14:10.6 | INFO | x | CHILD_FINISHED' + bytes([10]))\n"
    "sys.stdout.buffer.flush()\n"
)


def test_stream_survives_chars_the_launcher_stdout_cannot_encode() -> None:
    # A U+2212 / rupee sign in ONE line used to raise UnicodeEncodeError inside print(),
    # killing the reader thread; the child then blocked forever on a full pipe.
    blocked, reader_alive, printed = _run_child_through_stream(_UTF8_CHILD, "cp1252")
    assert blocked is False, "child blocked on its stdout pipe: reader thread died"
    assert reader_alive is False  # ended normally at EOF, not stuck
    assert "line 2999" in printed and "CHILD_FINISHED" in printed
    assert (
        "line 50 " in printed
    )  # the offending line itself is still delivered (degraded, not dropped)


def test_stream_keeps_draining_after_an_unexpected_per_line_error() -> None:
    def boom_once(line, _n=[0]):  # noqa: B006
        _n[0] += 1
        if _n[0] == 5:
            raise RuntimeError("formatter exploded")
        return line

    with patch("launcher._safe_print", side_effect=boom_once):
        blocked, _alive, _out = _run_child_through_stream(_UTF8_CHILD, "utf-8")
    assert blocked is False
