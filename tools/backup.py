"""
tools/backup.py — AshwaX Backup Strategy (Phase 2.5)

Run nightly via Task Scheduler or cron:  python tools/backup.py

Backs up: PostgreSQL DB, config/, strategies/, logs/
Keeps last 7 daily copies; prunes older ones automatically.
"""

from __future__ import annotations

import subprocess
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

_BASE = Path(__file__).parent.parent
_BACKUP_DIR = _BASE / "backups"
_KEEP_DAYS = 7
_DB_NAME = "ashwax"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M")


def _prune(pattern: str) -> None:
    cutoff = datetime.now() - timedelta(days=_KEEP_DAYS)
    for f in _BACKUP_DIR.glob(pattern):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            print(f"  pruned {f.name}")


def backup_postgres() -> bool:
    out = _BACKUP_DIR / f"ashwax_{_stamp()}.dump"
    try:
        subprocess.run(
            ["pg_dump", "-Fc", "-d", _DB_NAME, "-f", str(out)],
            check=True,
            capture_output=True,
        )
        print(f"  DB → {out.name} ({out.stat().st_size // 1024} KB)")
        _prune("ashwax_*.dump")
        return True
    except Exception as exc:
        print(f"  DB backup FAILED: {exc}")
        return False


def backup_dir(src: Path, prefix: str) -> None:
    if not src.exists():
        return
    out = _BACKUP_DIR / f"{prefix}_{_stamp()}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(src, arcname=src.name)
    print(f"  {prefix} → {out.name} ({out.stat().st_size // 1024} KB)")
    _prune(f"{prefix}_*.tar.gz")


def main() -> None:
    _BACKUP_DIR.mkdir(exist_ok=True)
    print(f"AshwaX backup — {datetime.now().isoformat()}")
    backup_postgres()
    backup_dir(_BASE / "config", "config")
    backup_dir(_BASE / "strategies", "strategies")
    backup_dir(_BASE / "logs", "logs")
    print("Done.")


if __name__ == "__main__":
    main()
