"""
Singleton process lock for ashwax orchestrator.

Must be called as the very first action in src/orchestrator.py:
    from src.infrastructure.singleton import acquire_singleton_lock
    acquire_singleton_lock()

Guarantees only one ashwax instance runs per Kite account on this machine.
Mutex name includes a hash of KITE_USER_ID so different accounts can coexist.

On lock held by live process  → sys.exit(99).
On lock held by dead process  → force-acquire (previous instance crashed).
On clean acquisition          → writes PID to logs/orchestrator.lock, registers atexit.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_LOCK_HANDLE: object = None  # holds the Windows HANDLE so GC can't release it


def acquire_singleton_lock(lock_dir: str | Path = "logs") -> None:
    """
    Acquire the per-account singleton mutex.

    Args:
        lock_dir: directory where orchestrator.lock PID file is written.

    Raises:
        SystemExit(99): if another live instance is already running.
    """
    user_id = os.getenv("KITE_USER_ID", "default")
    mutex_name = _mutex_name(user_id)

    if sys.platform == "win32":
        _acquire_windows(mutex_name, lock_dir)
    else:
        _acquire_posix(lock_dir)


def _mutex_name(user_id: str) -> str:
    uid_hash = hashlib.sha256(user_id.encode()).hexdigest()[:12]
    return f"Global\\ashwax_{uid_hash}"


# ── Windows implementation ───────────────────────────────────────────────────


def _acquire_windows(mutex_name: str, lock_dir: str | Path) -> None:
    import ctypes

    global _LOCK_HANDLE

    kernel32 = ctypes.windll.kernel32
    ERROR_ALREADY_EXISTS = 183

    handle = kernel32.CreateMutexW(None, True, mutex_name)
    last_err = kernel32.GetLastError()

    if handle == 0:
        # CreateMutex failed entirely — fall back to PID file
        _acquire_via_pidfile(lock_dir)
        return

    if last_err == ERROR_ALREADY_EXISTS:
        pid = _read_pidfile(lock_dir)
        if pid and _pid_alive(pid):
            kernel32.CloseHandle(handle)
            print(
                f"FATAL: Another ashwax instance is running (PID {pid}). Exiting.", file=sys.stderr
            )
            sys.exit(99)
        # Stale mutex from crashed previous run — we won the race, proceed
        print(
            f"WARNING: Previous ashwax instance (PID {pid}) crashed. Recovering.", file=sys.stderr
        )

    _LOCK_HANDLE = handle  # prevent GC from releasing the handle
    _write_pidfile(lock_dir)
    atexit.register(_release_windows, handle, lock_dir)


def _release_windows(handle: object, lock_dir: str | Path) -> None:
    import ctypes

    ctypes.windll.kernel32.ReleaseMutex(handle)
    ctypes.windll.kernel32.CloseHandle(handle)
    _remove_pidfile(lock_dir)


# ── POSIX fallback (Linux/macOS dev environments) ────────────────────────────


def _acquire_posix(lock_dir: str | Path) -> None:
    """Fallback for non-Windows. Uses fcntl advisory lock on PID file."""
    import fcntl

    lock_path = Path(lock_dir) / "orchestrator.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    f = open(lock_path, "w")  # noqa: SIM115 — file kept open to hold the lock
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
    except OSError:
        pid = lock_path.read_text().strip()
        print(f"FATAL: Another ashwax instance is running (PID {pid}). Exiting.", file=sys.stderr)
        sys.exit(99)

    f.write(str(os.getpid()))
    f.flush()
    atexit.register(_release_posix, f, lock_path)


def _release_posix(f: object, lock_path: Path) -> None:
    import fcntl

    fcntl.flock(f, fcntl.LOCK_UN)  # type: ignore[attr-defined]
    lock_path.unlink(missing_ok=True)


# ── PID file helpers ──────────────────────────────────────────────────────────


def _write_pidfile(lock_dir: str | Path) -> None:
    path = Path(lock_dir) / "orchestrator.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")


def _read_pidfile(lock_dir: str | Path) -> int | None:
    path = Path(lock_dir) / "orchestrator.lock"
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _remove_pidfile(lock_dir: str | Path) -> None:
    Path(lock_dir).joinpath("orchestrator.lock").unlink(missing_ok=True)


def _acquire_via_pidfile(lock_dir: str | Path) -> None:
    """Last-resort PID file lock when CreateMutex itself fails."""
    pid = _read_pidfile(lock_dir)
    if pid and _pid_alive(pid):
        print(f"FATAL: Another ashwax instance is running (PID {pid}). Exiting.", file=sys.stderr)
        sys.exit(99)
    _write_pidfile(lock_dir)
    atexit.register(_remove_pidfile, lock_dir)


def _pid_alive(pid: int) -> bool:
    """Return True if a process with this PID is currently running."""
    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
