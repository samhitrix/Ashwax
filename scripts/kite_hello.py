"""
Phase-0 sanity check: verifies Kite API key, TOTP flow, and get_margins().
Run once before starting Phase 1. Not part of the main app.

Usage:
  python scripts/kite_hello.py

Prerequisites:
  pip install kiteconnect pyotp keyring python-dotenv
  .env filled with KITE_API_KEY, KITE_SECRET, KITE_USER_ID
  TOTP secret stored via:
    python -c "import keyring; keyring.set_password('kite_totp', '<USER_ID>', '<TOTP_SECRET>')"
"""

import json
import os
import sys
import webbrowser
from urllib.parse import parse_qs, urlparse

try:
    import keyring
    import pyotp
    from dotenv import load_dotenv
    from kiteconnect import KiteConnect
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install kiteconnect pyotp keyring python-dotenv")

load_dotenv()

API_KEY = os.environ.get("KITE_API_KEY", "")
API_SECRET = os.environ.get("KITE_SECRET", "")
USER_ID = os.environ.get("KITE_USER_ID", "")

if not all([API_KEY, API_SECRET, USER_ID]):
    sys.exit("Set KITE_API_KEY, KITE_SECRET, KITE_USER_ID in .env")

kite = KiteConnect(api_key=API_KEY)

# Step 1: open login URL
login_url = kite.login_url()
print(f"\n[1] Opening Kite login URL:\n    {login_url}")
webbrowser.open(login_url)

# Step 2: TOTP (auto if secret stored in keyring)
totp_secret = keyring.get_password("kite_totp", USER_ID)
if totp_secret:
    otp = pyotp.TOTP(totp_secret).now()
    print(f"[2] TOTP (valid ~30s): {otp}")
else:
    print("[2] No TOTP secret in keyring — enter OTP manually in browser")

# Step 3: paste request_token OR the full redirect URL
raw = input("\n[3] Paste the request_token (or the full redirect URL): ").strip()
if not raw:
    sys.exit("No input provided")

# Accept full URL: extract request_token param if a URL was pasted
if raw.startswith("http"):
    params = parse_qs(urlparse(raw).query)
    tokens = params.get("request_token", [])
    if not tokens:
        sys.exit("Could not find request_token in URL")
    request_token = tokens[0]
    print(f"    Extracted request_token: {request_token}")
else:
    request_token = raw

# Step 4: generate session
data = kite.generate_session(request_token, api_secret=API_SECRET)
access_token = data["access_token"]
kite.set_access_token(access_token)
print(f"[4] Session OK — access_token: {access_token[:8]}...")

# Step 5: verify margins
margins = kite.margins()
print("\n[5] get_margins() response:")
print(json.dumps(margins, indent=2, default=str))
print("\n[OK] Kite hello-world passed.")
print(f"     Add to .env: KITE_ACCESS_TOKEN={access_token}")
