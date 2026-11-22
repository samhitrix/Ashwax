"""
Angel One SmartAPI session manager — standalone CLI, mirrors kite_auth.py.

PIN and TOTP secret live in the OS keyring (never in any file). Only the
resulting ANGEL_ONE_ACCESS_TOKEN/ANGEL_ONE_REFRESH_TOKEN are written to .env.

Unlike Kite (no refresh grant — only a full daily re-login), Angel One's JWT
supports a real refresh-token renewal (AngelOneAuth.refresh_token(), no TOTP
needed) — this script uses it first and only falls back to a fresh TOTP login
when the stored refresh token itself has expired.

One-time credential setup (run once per machine):
    python -m src.brokers.angel_one_auth --setup

After that:
    python -m src.brokers.angel_one_auth            # refresh if needed, print status
    python -m src.brokers.angel_one_auth --force     # force a fresh TOTP login
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from datetime import datetime
from pathlib import Path

import keyring
import pyotp
from dotenv import load_dotenv, set_key

from src.brokers.adapters.angel_one.auth import AngelOneAuth
from src.infrastructure.logger import get_logger

logger = get_logger(__name__)

_ENV_PATH = Path(__file__).parent.parent.parent / ".env"


def _write_tokens(env_path: Path, auth: AngelOneAuth, access_token: str) -> None:
    set_key(str(env_path), "ANGEL_ONE_ACCESS_TOKEN", access_token)
    os.environ["ANGEL_ONE_ACCESS_TOKEN"] = access_token
    if auth._refresh_token:  # noqa: SLF001
        set_key(str(env_path), "ANGEL_ONE_REFRESH_TOKEN", auth._refresh_token)  # noqa: SLF001
        os.environ["ANGEL_ONE_REFRESH_TOKEN"] = auth._refresh_token  # noqa: SLF001


def auto_refresh(env_path: Path = _ENV_PATH, *, force: bool = False) -> str:
    """
    Ensure a valid ANGEL_ONE_ACCESS_TOKEN is in env_path.

    1. If not forced, try the current token via get_session_status().
    2. Otherwise try the stored refresh token (no TOTP needed).
    3. Otherwise fall back to a full TOTP login.
    4. Write the resulting access/refresh tokens back to env_path.

    Raises RuntimeError (from AngelOneAuth.login()) if keyring credentials are
    missing — the message names the --setup command to fix it.
    """
    load_dotenv(env_path, override=True)
    api_key = os.environ.get("ANGEL_ONE_API_KEY", "")
    client_code = os.environ.get("ANGEL_ONE_CLIENT_CODE", "")
    current_token = os.environ.get("ANGEL_ONE_ACCESS_TOKEN", "")
    refresh_token = os.environ.get("ANGEL_ONE_REFRESH_TOKEN", "")

    auth = AngelOneAuth(api_key=api_key, client_code=client_code)
    if refresh_token:
        auth._refresh_token = refresh_token  # noqa: SLF001

    if not force and current_token:
        auth.set_access_token(current_token)
        status = asyncio.run(auth.get_session_status())
        if status.is_valid:
            logger.info("angel_one_auth: token valid, no refresh needed")
            return current_token

    logger.info("angel_one_auth: refreshing session")
    info = asyncio.run(auth.login() if force else auth.refresh_token())
    _write_tokens(env_path, auth, info.access_token)
    logger.info("angel_one_auth: token refreshed and saved to .env")
    return info.access_token


def run_setup() -> None:
    """Interactive credential setup. Stores PIN + TOTP secret in keyring."""
    logger.info("angel_one_setup: starting")

    client_code = input("Angel One client code: ").strip()
    pin = getpass.getpass("Angel One 4-digit trading PIN (NOT your web login password): ")
    raw_secret = getpass.getpass(
        "TOTP secret key (the base32 string from 2FA setup, NOT a 6-digit code): "
    )
    totp_secret = raw_secret.replace(" ", "").replace("-", "").upper()

    try:
        totp = pyotp.TOTP(totp_secret)
        now = datetime.now()
        print("\nWhich of these matches your authenticator app RIGHT NOW?")
        for offset in (-60, -30, 0, 30, 60):
            label = f"{offset:+d}s" if offset else " now"
            print(f"  [{label}]  {totp.at(now.timestamp() + offset)}")
    except Exception as exc:
        logger.error("angel_one_setup: TOTP secret invalid", extra={"ctx": {"error": str(exc)}})
        sys.exit(1)

    confirm = input("\nDoes the [ now] code match your app? [y/N]: ").strip().lower()
    if confirm != "y":
        logger.error(
            "angel_one_setup: TOTP not confirmed — if a +/-30s code matched, fix the PC clock; "
            "if none matched, the secret differs from the app's"
        )
        sys.exit(1)

    keyring.set_password("angel_one_login", client_code, pin)
    keyring.set_password("angel_one_totp", client_code, totp_secret)
    logger.info("angel_one_setup: credentials stored in OS keyring")
    print(
        f"\nStored. Add ANGEL_ONE_CLIENT_CODE={client_code} (and ANGEL_ONE_API_KEY) to .env, "
        "then run:  python -m src.brokers.angel_one_auth"
    )


def _main() -> None:
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Angel One SmartAPI session manager")
    parser.add_argument("--setup", action="store_true", help="First-time credential setup")
    parser.add_argument(
        "--force", action="store_true", help="Force a fresh TOTP login even if a token exists"
    )
    args = parser.parse_args()

    if args.setup:
        run_setup()
        return

    print("Checking current token / refreshing if needed...")
    token = auto_refresh(force=args.force)
    print(
        f"Token OK (length={len(token)}). If downloads still fail, run:  "
        "python -m src.brokers.angel_one_auth --force"
    )


if __name__ == "__main__":
    _main()
