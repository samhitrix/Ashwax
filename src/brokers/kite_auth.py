"""
Automated Kite Connect daily session manager.

Uses Playwright (headless Chromium) + TOTP for fully automated token refresh.
Password and TOTP secret live in machine-wide DPAPI storage (src.brokers.
machine_secrets) — never in any file in plaintext. Only the resulting
KITE_ACCESS_TOKEN is written back to .env.

Machine-wide (not Windows Credential Manager) on purpose: this service's
logon account has changed (interactive user -> dedicated service account ->
SYSTEM) and Credential Manager is strictly per-account, breaking auth every
time. See machine_secrets.py's module docstring for the tradeoff this makes.

One-time credential setup (run once per machine):
    python -m src.brokers.kite_auth --setup

After that, auto_refresh() handles daily token renewal automatically and is called
by launcher.py on startup.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pyotp
from dotenv import load_dotenv, set_key
from kiteconnect import KiteConnect

from src.brokers.machine_secrets import get_machine_secret, set_machine_secret
from src.infrastructure.logger import get_logger

logger = get_logger(__name__)

_ENV_PATH = Path(__file__).parent.parent.parent / ".env"


class KiteAuthError(RuntimeError):
    pass


class CredentialsMissingError(KiteAuthError):
    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_token_valid(api_key: str, access_token: str) -> bool:
    """Quick check — returns True only if token is accepted by Kite right now."""
    if not api_key or not access_token:
        return False
    try:
        k = KiteConnect(api_key=api_key)
        k.set_access_token(access_token)
        k.profile()
        return True
    except Exception:
        return False


def auto_refresh(env_path: Path = _ENV_PATH) -> str:
    """
    Ensure a valid KITE_ACCESS_TOKEN is in env_path.

    1. Load current values from env_path.
    2. If token is valid → return it immediately (noop).
    3. Fetch password + TOTP secret from machine-wide DPAPI storage.
    4. Headless Playwright login → request_token → generate_session.
    5. Write new KITE_ACCESS_TOKEN to env_path.
    6. Return new token.

    Raises CredentialsMissingError if credentials not stored.
    Raises KiteAuthError on any login step failure.
    """
    load_dotenv(env_path, override=True)
    api_key = os.environ.get("KITE_API_KEY", "")
    api_secret = os.environ.get("KITE_SECRET", "")
    user_id = os.environ.get("KITE_USER_ID", "")
    current_token = os.environ.get("KITE_ACCESS_TOKEN", "")

    if is_token_valid(api_key, current_token):
        logger.info("kite_auth: token valid, no refresh needed")
        return current_token

    logger.info("kite_auth: token expired — starting headless refresh")

    password = get_machine_secret("KITE_PASSWORD")
    totp_secret = get_machine_secret("KITE_TOTP_SECRET")

    if not password or not totp_secret:
        raise CredentialsMissingError(
            "Kite login credentials not found.\n"
            "Run once to store them securely (never written to any file in plaintext):\n"
            "  python -m src.brokers.kite_auth --setup"
        )

    request_token = asyncio.run(_playwright_login(api_key, user_id, password, totp_secret))

    kite = KiteConnect(api_key=api_key)
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token: str = session_data["access_token"]

    # Persist so next startup reuses it without re-login
    set_key(str(env_path), "KITE_ACCESS_TOKEN", access_token)
    os.environ["KITE_ACCESS_TOKEN"] = access_token

    logger.info("kite_auth: token refreshed and saved to .env")
    return access_token


# ---------------------------------------------------------------------------
# First-time credential setup
# ---------------------------------------------------------------------------


def run_setup() -> None:
    """Interactive credential setup. Stores password + TOTP secret via machine_secrets."""
    logger.info("kite_setup: starting")

    password = getpass.getpass("Zerodha password: ")
    raw_secret = getpass.getpass(
        "TOTP secret key (the base32 string from 2FA setup, NOT a 6-digit code): "
    )
    # Base32 secrets are case-insensitive and often displayed grouped with spaces.
    totp_secret = raw_secret.replace(" ", "").replace("-", "").upper()

    try:
        totp = pyotp.TOTP(totp_secret)
        now = datetime.now()
        # Print neighbouring 30s windows so the user can tell clock-drift from a
        # wrong secret: if a +/-30s code matches the app, the clock is off; if no
        # code matches, the secret itself is wrong.
        print("\nWhich of these matches your authenticator app RIGHT NOW?")
        for offset in (-60, -30, 0, 30, 60):
            label = f"{offset:+d}s" if offset else " now"
            print(f"  [{label}]  {totp.at(int(now.timestamp()) + offset)}")
    except Exception as exc:
        logger.error("kite_setup: TOTP secret invalid", extra={"ctx": {"error": str(exc)}})
        sys.exit(1)

    confirm = input("\nDoes the [ now] code match your app? [y/N]: ").strip().lower()
    if confirm != "y":
        logger.error(
            "kite_setup: TOTP not confirmed — if a +/-30s code matched, fix the PC clock; "
            "if none matched, the secret differs from the app's"
        )
        sys.exit(1)

    set_machine_secret("KITE_PASSWORD", password)
    set_machine_secret("KITE_TOTP_SECRET", totp_secret)
    logger.info("kite_setup: credentials stored (machine-wide DPAPI)")


# ---------------------------------------------------------------------------
# Headless Playwright login (drives the real Kite login page)
# ---------------------------------------------------------------------------


async def _playwright_login(api_key: str, user_id: str, password: str, totp_secret: str) -> str:
    """Headless Chromium login on the real Kite page → returns request_token."""
    from playwright.async_api import async_playwright

    connect_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Abort the localhost callback — we only need the URL, not the response
        await page.route("**localhost**", lambda r: r.abort())
        await page.route("**127.0.0.1**", lambda r: r.abort())

        # Step 1: navigate + submit user_id / password
        await page.goto(connect_url, wait_until="domcontentloaded")
        await page.wait_for_selector("#userid", timeout=15_000)
        await page.fill("#userid", user_id)
        await page.fill("#password", password)
        await page.click('button[type="submit"]')
        logger.info("kite_auth: login step 1 ok", extra={"ctx": {"user_id": user_id}})

        # Step 2: TOTP. Kite auto-submits when all 6 digits are entered, so we
        # start expect_request BEFORE filling to catch the callback redirect.
        await page.wait_for_selector(
            "#totp, input[type='number'], input[autocomplete='one-time-code']",
            timeout=15_000,
        )
        totp_locator = page.locator(
            "#totp, input[type='number'], input[autocomplete='one-time-code']"
        ).first

        async with page.expect_request(
            lambda req: "request_token" in req.url, timeout=60_000
        ) as req_info:
            await totp_locator.fill(pyotp.TOTP(totp_secret).now())
            logger.info("kite_auth: login step 2 (TOTP) ok")

            # If the form doesn't auto-submit, click the button
            with contextlib.suppress(Exception):
                await page.click(
                    'button[type="submit"]', timeout=3_000
                )  # else already auto-submitted

            # Handle the Authorise consent page if it appears (else auto-redirected)
            with contextlib.suppress(Exception):
                await page.wait_for_selector(
                    "button:has-text('Authorise'), button:has-text('Authorize')",
                    timeout=15_000,
                )
                await page.click("button:has-text('Authorise'), button:has-text('Authorize')")
                logger.info("kite_auth: authorize clicked")

        cb_req = await req_info.value
        m = re.search(r"request_token=([A-Za-z0-9]+)", cb_req.url)
        await browser.close()

        if not m:
            raise KiteAuthError(f"request_token not found in callback URL: {cb_req.url!r}")
        logger.info("kite_auth: request_token captured")
        return m.group(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> None:
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Kite session manager")
    parser.add_argument("--setup", action="store_true", help="First-time credential setup")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force token refresh even if current token appears valid",
    )
    args = parser.parse_args()

    if args.setup:
        run_setup()
    elif args.force:
        load_dotenv(_ENV_PATH, override=True)
        api_key = os.environ.get("KITE_API_KEY", "")
        api_secret = os.environ.get("KITE_SECRET", "")
        user_id = os.environ.get("KITE_USER_ID", "")
        print("Forcing token refresh via headless Playwright login...")
        password = get_machine_secret("KITE_PASSWORD")
        totp_secret = get_machine_secret("KITE_TOTP_SECRET")
        if not password or not totp_secret:
            print("ERROR: credentials not found. Run with --setup first.")
            sys.exit(1)
        request_token = asyncio.run(_playwright_login(api_key, user_id, password, totp_secret))
        kite = KiteConnect(api_key=api_key)
        session_data = kite.generate_session(request_token, api_secret=api_secret)
        access_token: str = session_data["access_token"]
        set_key(str(_ENV_PATH), "KITE_ACCESS_TOKEN", access_token)
        os.environ["KITE_ACCESS_TOKEN"] = access_token
        print(f"Token refreshed and saved to .env (length={len(access_token)})")
    else:
        print("Checking current token validity...")
        token = auto_refresh()
        print(
            f"Token OK (length={len(token)}). If downloads still fail, run:  python -m src.brokers.kite_auth --force"
        )


if __name__ == "__main__":
    _main()
