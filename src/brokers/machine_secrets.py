"""
Machine-wide secret storage — DPAPI with CRYPTPROTECT_LOCAL_MACHINE.

Unlike Windows Credential Manager (what `keyring` uses), this is NOT scoped to
the account that wrote it: the machine's own key material does the encryption,
so any account on this machine can decrypt it back. That's a deliberate choice
here — the trading service's logon account has changed multiple times
(interactive user -> dedicated service account -> SYSTEM) and each change
broke Kite auth because Credential Manager is strictly per-account. This
trades that isolation for one that only depends on "is this the same machine",
which is what the deployment actually needs.

Cost of that trade: any process running as any account on this machine can
read these secrets, not just the trading service. No extra NTFS ACL is
applied on top of that: a custom ACL here either has to include the
creating user (in which case it adds nothing the DPAPI flag doesn't already
concede) or exclude them (in which case, on a single-user machine, it
breaks the very next write in the same interactive session the moment UAC
filters the Administrators SID out of a non-elevated admin user's token --
confirmed the hard way, twice, before this comment was written). Default
file permissions are the right call for a single-user machine; the DPAPI
encryption is the actual boundary being relied on here, not the filesystem.

Where DPAPI is unavailable (Linux / macOS / Docker, or Windows without pywin32)
secrets are read from environment variables instead -- ``ASHWAX_SECRET_`` plus
the name upper-cased with every non-alphanumeric turned into ``_``, e.g.
``kite_login:AB1234`` -> ``ASHWAX_SECRET_KITE_LOGIN_AB1234``. Writing is
refused there: the environment (Docker/compose secrets) is the store.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import cast

try:
    import win32crypt
    import win32cryptcon
except ImportError:  # not Windows, or pywin32 not installed -> environment fallback
    win32crypt = None  # type: ignore[assignment]
    win32cryptcon = None  # type: ignore[assignment]

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_PATH = Path(__file__).parent.parent.parent / "config" / ".machine_secrets.dat"

# The flag constant lives in win32cryptcon, not win32crypt itself.
_CRYPTPROTECT_LOCAL_MACHINE = (
    win32cryptcon.CRYPTPROTECT_LOCAL_MACHINE if win32cryptcon is not None else 0
)


def _env_name(name: str) -> str:
    return "ASHWAX_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def _load(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return cast("dict[str, str]", json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        logger.error("machine_secrets_read_failed path=%s err=%s", path, str(exc)[:200])
        return {}


def set_machine_secret(name: str, value: str, path: Path = _DEFAULT_PATH) -> None:
    """Encrypt `value` with machine-scoped DPAPI and store it under `name`.

    Other entries already in the file are preserved (one blob file holds
    several named secrets, each individually encrypted).
    """
    if win32crypt is None:
        raise RuntimeError(
            f"DPAPI is unavailable here; set the environment variable {_env_name(name)} instead"
        )
    blob: bytes = win32crypt.CryptProtectData(
        value.encode("utf-8"), name, None, None, None, _CRYPTPROTECT_LOCAL_MACHINE
    )
    data = _load(path)
    data[name] = blob.hex()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    logger.info("machine_secret_set name=%s path=%s", name, path)


def get_machine_secret(name: str, path: Path = _DEFAULT_PATH) -> str | None:
    """Decrypt and return the named secret, or None if missing/unreadable."""
    if win32crypt is None:
        return os.environ.get(_env_name(name)) or None
    data = _load(path)
    hex_blob = data.get(name)
    if not hex_blob:
        return None
    try:
        _descr, plaintext = win32crypt.CryptUnprotectData(
            bytes.fromhex(hex_blob), None, None, None, 0
        )
        return cast(bytes, plaintext).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.error("machine_secret_decrypt_failed name=%s err=%s", name, str(exc)[:200])
        return None
