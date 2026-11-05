"""
Flattrade Pi API session helper — standalone CLI, MANUAL-ASSISTED login.

Flattrade's daily login needs a human in a browser (no documented headless or
TOTP path; terms 2(a)/2(e) rule out scripting auth.flattrade.in). This helper
only does the parts that are allowed: print the login URL, take the redirect
URL/code the user pastes, exchange it for the session token (must run from the
API key's registered static IP) and save FLATTRADE_ACCESS_TOKEN to .env.

The API secret lives in the OS keyring (never in any file); only the resulting
token is written to .env. Tokens are cleared daily ~5-6 AM IST.

One-time credential setup (run once per machine):
    python -m src.brokers.flattrade_auth --setup

Each trading day (after 6 AM IST):
    python -m src.brokers.flattrade_auth
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import keyring
from dotenv import load_dotenv, set_key

from src.brokers.adapters.flattrade.auth import MANUAL_LOGIN_HINT, FlattradeAuth
from src.infrastructure.logger import get_logger

logger = get_logger(__name__)

_ENV_PATH = Path(__file__).parent.parent.parent / ".env"
_KEYRING_SERVICE = "flattrade_api_secret"
_EXPIRED_MSG = f"Flattrade token expired; {MANUAL_LOGIN_HINT}"
_PASTE_JUNK = "\"'<>.,;:)( \t"


def _get_api_secret(user_id: str) -> str | None:
    secret = keyring.get_password(_KEYRING_SERVICE, user_id)
    return str(secret) if secret else None


def parse_request_code(pasted: str) -> str:
    """Extract the one-time code from a pasted redirect URL, a `code=`/`request_code=`
    fragment, or a bare code. The docs say `request_code`, the official sample reads
    `code` (findings A2), so both are accepted.

    Raises:
        ValueError: nothing usable was pasted.
    """
    # Terminals and chat apps wrap pasted links in quotes/<...> and drag in a trailing
    # sentence mark; none of those characters can be part of the code itself.
    text = pasted.strip().strip(_PASTE_JUNK).strip()
    if not text:
        raise ValueError("nothing pasted")
    if "=" in text:
        query = urlsplit(text).query if "?" in text else text
        params = parse_qs(query)
        for key in ("request_code", "code"):
            values = params.get(key)
            if values and values[0].strip():
                return values[0].strip()
        raise ValueError("pasted text has no code= or request_code= parameter")
    if any(c.isspace() for c in text) or "/" in text:
        raise ValueError("pasted text is neither a redirect URL nor a code")
    return text


def run_login(env_path: Path = _ENV_PATH) -> str:
    """Interactive manual-assisted login; writes FLATTRADE_ACCESS_TOKEN to env_path."""
    load_dotenv(env_path, override=True)
    api_key = os.environ.get("FLATTRADE_API_KEY", "").strip()
    user_id = os.environ.get("FLATTRADE_USER_ID", "").strip()
    if not api_key or not user_id:
        raise RuntimeError(f"Set FLATTRADE_API_KEY and FLATTRADE_USER_ID in {env_path} first")
    api_secret = _get_api_secret(user_id)
    if not api_secret:
        raise RuntimeError(
            f"No Flattrade API secret in keyring for {user_id!r}; "
            "run python -m src.brokers.flattrade_auth --setup"
        )

    auth = FlattradeAuth(api_key=api_key, user_id=user_id)
    print("\n1. Open this URL in your browser and log in:")
    print(f"   {auth.login_url()}")
    print("2. After the redirect, copy the full address-bar URL (or just the code).")
    print("   The code is valid for a few minutes only; this must run from the registered IP.")
    code = parse_request_code(input("\nPaste redirect URL or code: "))

    token = asyncio.run(auth.exchange_request_code(code, api_secret))
    set_key(str(env_path), "FLATTRADE_ACCESS_TOKEN", token)
    os.environ["FLATTRADE_ACCESS_TOKEN"] = token
    logger.info("flattrade_auth: token saved to .env")
    print(
        f"\nToken saved to {env_path} (length={len(token)}). "
        "It expires ~5 AM IST (cleared 5-6 AM); log in again after 6 AM."
    )
    return token


def auto_refresh(env_path: Path = _ENV_PATH, *, force: bool = False) -> str:
    """Return the stored token if UserDetails accepts it; never logs in.

    Raises:
        RuntimeError: no token, an expired token, or force=True — a human must
            run the manual login (this function cannot and must not do it).
        FlattradeError: any other API/network failure (propagated as is).
    """
    load_dotenv(env_path, override=True)
    token = os.environ.get("FLATTRADE_ACCESS_TOKEN", "").strip()
    if force or not token:
        raise RuntimeError(_EXPIRED_MSG)
    auth = FlattradeAuth(
        api_key=os.environ.get("FLATTRADE_API_KEY", ""),
        user_id=os.environ.get("FLATTRADE_USER_ID", ""),
    )
    auth.set_access_token(token)
    status = asyncio.run(auth.get_session_status())
    if not status.is_valid:
        raise RuntimeError(_EXPIRED_MSG)
    logger.info("flattrade_auth: token valid")
    return token


def run_setup() -> None:
    """Interactive one-time setup: stores the API secret in the OS keyring."""
    user_id = input("Flattrade client id (UCC): ").strip()
    api_secret = getpass.getpass("Flattrade API secret (wall.flattrade.in -> Pi): ").strip()
    if not user_id or not api_secret:
        raise RuntimeError("client id and API secret are both required")
    keyring.set_password(_KEYRING_SERVICE, user_id, api_secret)
    logger.info("flattrade_setup: API secret stored in OS keyring")
    print(
        f"\nStored. Add FLATTRADE_USER_ID={user_id} and FLATTRADE_API_KEY to .env, "
        "then each day run:  python -m src.brokers.flattrade_auth"
    )


def _main() -> None:
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Flattrade manual-assisted session helper")
    parser.add_argument("--setup", action="store_true", help="Store the API secret in keyring")
    args = parser.parse_args()
    if args.setup:
        run_setup()
        return
    run_login()


if __name__ == "__main__":
    _main()
