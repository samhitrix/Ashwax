# Installation guide

This guide takes you from a fresh machine to a running **paper-trading** AshwaX with the
dashboard open. Plan on 20–30 minutes; the first market-data backfill runs in the background
after that.

- [1. Prerequisites](#1-prerequisites)
- [2. Get the code](#2-get-the-code)
- [3. Python environment](#3-python-environment)
- [4. PostgreSQL](#4-postgresql)
- [5. Environment file (.env)](#5-environment-file-env)
- [6. Broker connection (optional in paper mode)](#6-broker-connection-optional-in-paper-mode)
- [7. LLM providers](#7-llm-providers)
- [8. Telegram alerts (optional)](#8-telegram-alerts-optional)
- [9. Dashboard](#9-dashboard)
- [10. First run](#10-first-run)
- [11. Run as a Windows service (optional)](#11-run-as-a-windows-service-optional)
- [12. Verify the install](#12-verify-the-install)
- [13. Troubleshooting](#13-troubleshooting)
- [14. Before you ever go live](#14-before-you-ever-go-live)

---

## 1. Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| Python | **3.13+** | everything |
| PostgreSQL | 14+ | **required** — all state lives here |
| Node.js | 20.19+ or 22+ | dashboard only (Vite) |
| Git | any | cloning |
| Redis | 5+ | *optional* — news dedup and chain cache fall back to in-memory |
| Zerodha Kite Connect app | — | *optional in paper mode* — live prices, option chains, live orders |

AshwaX is developed and run on **Windows 11** (PowerShell). Linux and macOS work for paper
trading; the Windows-service step in §11 is Windows-only.

## 2. Get the code

```bash
git clone <your-fork-url> AshwaX
cd AshwaX
```

## 3. Python environment

```bash
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# Linux / macOS:         source .venv/bin/activate

pip install -e ".[dev]"            # core + test/lint tools
# or, without the editable install:  pip install -r requirements.txt
```

Optional extras (each feature switches itself off cleanly when its extra is missing):

```bash
pip install -e ".[redis]"          # Redis-backed news dedup / chain cache
pip install -e ".[anthropic]"      # Claude as an LLM provider
pip install -e ".[fundamentals]"   # yfinance fundamentals snapshot
pip install -e ".[autologin]"      # Playwright for automated Kite login
playwright install chromium        # only if you installed [autologin]
```

## 4. PostgreSQL

Create one empty database. AshwaX creates every table itself.

```bash
createdb ashwax
# or in psql:  CREATE DATABASE ashwax;
```

The schema lives in `migrations/*.sql` and is applied **automatically at startup**. You can
also apply it by hand:

```bash
python -m src.db.migrate
```

## 5. Environment file (.env)

```bash
cp .env.example .env
```

At minimum, set the database URL:

```ini
DB_URL=postgresql://postgres:<your-password>@localhost:5432/ashwax
```

Every other key in `.env.example` is optional and documented inline. `.env` is git-ignored;
**never commit it.**

## 6. Broker connection (optional in paper mode)

Paper mode runs without a broker. For live prices, option chains and (much later) live
orders, connect Zerodha Kite Connect:

1. Create an app at <https://developers.kite.trade> and note its API key and secret.
2. Put them in `.env`:
   ```ini
   KITE_API_KEY=...
   KITE_SECRET=...
   KITE_USER_ID=...
   # KITE_ACCESS_TOKEN is written automatically each day; leave it blank
   ```
3. For automated daily login (needs the `[autologin]` extra), store your password and TOTP
   secret once in the OS-protected machine store. They are never written to a file:
   ```bash
   python -m src.brokers.kite_auth --setup
   ```
   After that, `launcher.py` refreshes the session token on every start.

**Angel One SmartAPI** is a second, optional broker adapter. See the commented `ANGEL_ONE_*` and
`BROKER=` lines in `.env.example`.

## 7. LLM providers

LLMs tag news, generate ideas for the LLM agents and act as the G9 reviewer. Free tiers are
enough to get started. Fill in the keys you have:

```ini
GEMINI_API_KEY=          # https://aistudio.google.com  (free tier)
# GROQ_API_KEY=          # https://console.groq.com     (free tier)
CLOUDFLARE_API_TOKEN=    # Cloudflare Workers AI        (free tier)
CLOUDFLARE_ACCOUNT_ID=
ANTHROPIC_API_KEY=       # optional, paid
# OLLAMA_MODEL=llama3    # optional local fallback
```

The order in which providers are tried for each job is set in `config/global.json` → `llm`
(`trade_gate`, `background`, `discretionary`, `swing_g9`, …). Providers without a key are
skipped. If a strategy needs a G9 verdict and no provider answers, that trade is **not** taken.

## 8. Telegram alerts (optional)

1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `result[0].message.chat.id`.
3. Add both to `.env`:
   ```ini
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```

Leave both blank to turn notifications off. Which events are sent is set in
`config/notifications.json`.

## 9. Dashboard

```bash
cd dashboard_react
npm install
cd ..
```

`launcher.py` starts the dashboard for you, so you don't need to run `npm run dev` yourself.

## 10. First run

```bash
python launcher.py
```

It starts:

| Component | Address |
|---|---|
| FastAPI backend | <http://localhost:3000> |
| React dashboard | <http://localhost:5173> |
| Orchestrator (scan / risk / exit loops) | background process |

`launcher.py` is the **only** supported entry point; don't start uvicorn or the orchestrator
separately. Press `Ctrl+C` to stop everything cleanly.

On the first start, AshwaX applies the migrations and begins backfilling market history. The
backfill respects broker rate limits and can take a while. Strategies that need history stay
idle until it finishes.

## 11. Run as a Windows service (optional)

To keep AshwaX running across reboots, run this from an **Administrator** PowerShell:

```powershell
.\ops\service_setup.ps1
```

This registers `launcher.py` as the `AshwaX` service through NSSM, installing NSSM via
Chocolatey if needed. The service starts on boot and is deliberately **not** auto-restarted after
a crash, so a failure waits for a person to look at it instead of looping unattended.

```powershell
Restart-Service AshwaX      # or: nssm restart AshwaX
Stop-Service AshwaX
```

## 12. Verify the install

```bash
pytest -q                                   # unit tests should pass
python tools/config_audit.py                # checks config/*.json for consistency
```

Then open <http://localhost:5173> and check:

- Every segment and strategy shows as **paper**.
- The system-health view shows the database connected and the loops running.
- During market hours, open positions and orders show real paper fills only; the dashboard
  never shows mock data.

## 13. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `could not connect to server` at startup | PostgreSQL isn't running, or `DB_URL` is wrong. |
| Port 3000 or 5173 already in use | Another copy is running. `launcher.py` sweeps stale processes on start; close any old terminal that still has it open. |
| No signals all day | Expected at first: market history is still backfilling, or regime/breadth gates are correctly blocking entries. Check the scan log lines. |
| Every trade rejected at **G9** | No LLM provider answered. Add at least one key (§7) and check the provider chain in `config/global.json`. |
| Kite `TokenException` | The daily session expired. Restart the launcher (it refreshes the token) or rerun `python -m src.brokers.kite_auth`. |
| Dashboard blank | Run `npm install` in `dashboard_react/` and use Node 20.19+ or 22+. |

Logs are written to `logs/` as pipe-delimited lines (`timestamp | level | module | message`).

## 14. Before you ever go live

Going live is three deliberate edits; nothing in the code makes them for you:

1. `config/operator.json` → `account.paper_mode_locked: false`
2. `config/global.json` → `system.segments.<segment>.paper_mode: false` (one segment at a time)
3. `config/strategies_registry.json` → that strategy's `execution_mode: "live"`

Do this only after the strategy has a long, positive **after-cost** paper record (for example
dozens of trades, profit factor ≥ 1.2, positive expectancy), the money limits in
`config/capital.json` fit your account, and broker-side protective stops are confirmed working.
To return to safety at any time, run `cp config/examples/*.json config/` and restart.
