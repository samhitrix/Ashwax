# Installation guide

This guide takes you from a fresh machine to a running **paper-trading** AshwaX with the
dashboard open. Plan on 20–30 minutes; the first market-data backfill runs in the background
after that.

- [1. Prerequisites](#1-prerequisites)
- [2. Get the code](#2-get-the-code)
- [3. Python environment](#3-python-environment)
- [4. PostgreSQL](#4-postgresql)
- [5. Environment file (.env)](#5-environment-file-env)
- [6. Broker connection (Kite Connect, static IP)](#6-broker-connection-kite-connect--needed-even-for-paper-trading)
- [7. LLM providers](#7-llm-providers)
- [8. Telegram alerts (optional)](#8-telegram-alerts-optional)
- [9. Dashboard](#9-dashboard)
- [10. First run](#10-first-run)
- [11. Run as a Windows service (optional)](#11-run-as-a-windows-service-optional)
- [12. Verify the install](#12-verify-the-install)
- [13. Troubleshooting](#13-troubleshooting)
- [14. Before you ever go live](#14-before-you-ever-go-live)
- [Docker](#docker)

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

## 6. Broker connection (Kite Connect — needed even for paper trading)

AshwaX uses **Zerodha Kite Connect**. Paper mode simulates every order locally, so no order
ever reaches a broker, but it still takes live prices, option chains and history from Kite.
Without `KITE_API_KEY` the orchestrator stops at startup.

### 6.1 What you need from Zerodha

| Goal | Plan you need | Static IP |
|---|---|---|
| **Paper trading** (market data only, no orders sent) | **Kite Connect paid plan** — the free Personal plan has no live or historical market data | Not needed |
| **Live trading** (real orders) | Paid plan, plus a Zerodha account with API access | **Required** — see 6.3 |

At the time of writing Zerodha lists the Personal plan as free (orders, GTT, portfolio, no market
data) and the Connect plan as ₹500 per month per API key (adds live and historical data).
Prices and terms change; confirm on Zerodha's
[Kite Connect page](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/how-do-i-sign-up-for-kite-connect).

### 6.2 Create the Kite Connect account and app

1. You need a **Zerodha trading account** (client ID). The app form asks for it.
2. Go to <https://developers.kite.trade/signup> and register with the email linked to your
   Zerodha account (name, password, mobile number, state).
3. Choose and pay for a plan (see 6.1). For AshwaX's market data you need the paid one.
4. Open **My Apps → Create New App** and fill in:
   - **App name:** anything, for example `AshwaX`
   - **Zerodha client ID:** your own ID — a wrong one gives "User not enabled for the app"
   - **Redirect URL:** `http://127.0.0.1:8000` works (Zerodha's own localhost example). The URL
     is only used to receive the one-time login token.
   - **Postback URL:** leave empty.
5. After creating it you get an **API key** and an **API secret**. Keep the secret private.
6. Turn on TOTP two-factor login for your Zerodha account if you want automated daily login (see
   Zerodha's support pages), and keep the secret key shown when you set it up.

### 6.3 Static IP (needed to place real orders)

Under the retail algo-trading rules that apply to API trading in India, Zerodha only accepts API
**order** requests from an **IP address you have registered**, and that address must be
**static** — a fixed public IPv4 address that does not change. Facts from Zerodha's own
documentation and staff forum replies:

- It applies to **placing orders** only. Market data, positions and the order book work from any
  IP. That is why **paper trading needs no static IP**.
- Register it in the Developer Console at <https://developers.kite.trade> → your profile →
  **IP Whitelist**. Zerodha allows one primary and one optional secondary IP, and the IP can be
  changed about once a week.
- Zerodha checks the IP the request **actually leaves from**. A home connection with a changing
  IP, a mobile hotspot or a VPN that changes address will have orders **rejected**.
- Order requests from an unregistered IP are rejected, so test with a small AMO order first.
- One IP may be shared only within immediate family (spouse, dependent children, dependent
  parents). Sharing beyond that can get the API keys suspended.
- Other rules apply at the same time: at most **10 orders per second** per client account (above
  that the strategy must be registered with the exchange), and market / SL-M orders need a
  non-zero **market protection** value.

How to get a static IP: ask your internet provider for one, or run AshwaX on a cloud server
(VPS) that has a fixed public address and register that. Which of these is acceptable to the
exchange for your situation is for you to confirm with Zerodha. Zerodha's pages do not name the
exact SEBI circular, so check the current rules with your broker before trading live.

### 6.4 Connect AshwaX to Kite

1. On Windows, install the credential-store dependency used by the login helper:
   `pip install pywin32` (not needed for paper mode or Docker).
2. Put the API key, secret and user ID in `.env`:
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

Kite access tokens are valid for one trading day and expire the next morning before market open,
and creating a new session invalidates the older token. This is why the system logs in again at
each start.

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
| API backend (`src/api/server.py`) | internal, port 3000 — the dashboard talks to it; you never open it |
| **React dashboard — open this** | <http://localhost:5173> |
| Orchestrator (scan / risk / exit loops) | background process |

`launcher.py` is the **only** supported entry point; don't start the API server or the orchestrator
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

## Docker

One command starts the whole system — the app (API, orchestrator and dashboard), PostgreSQL and
Redis — as a Compose project named `ashwax`. Everything runs in **paper mode** with the shipped
config.

### What you need

- Docker with Compose v2 (Docker Desktop on Windows / macOS, or Docker Engine on Linux).
- Kite Connect keys and the paid market-data plan: the app cannot start without `KITE_API_KEY`,
  because paper trading takes its market data from Kite (see [section 6](#6-broker-connection-kite-connect--needed-even-for-paper-trading)).

### Start

```bash
cp .env.example .env
# edit .env: KITE_API_KEY, KITE_SECRET, KITE_USER_ID, KITE_ACCESS_TOKEN (today's token),
# and at least one LLM key such as GEMINI_API_KEY
docker compose up -d --build
```

The first build takes several minutes. Then open **<http://localhost:5173>**. The container
reports `healthy` once the API answers; check with `docker compose ps`.

### What gets started

| Service | Image | Purpose |
|---|---|---|
| `app` | built from `Dockerfile` (`ashwax:latest`) | `launcher.py`: API, orchestrator, dashboard |
| `db` | `postgres:16` | all state; the schema is created automatically on first start |
| `redis` | `redis:7-alpine` | news de-duplication and option-chain cache |

Only the dashboard port is published. The API listens inside the container and the dashboard
reaches it through its own `/api` address, so there is nothing else to open or secure.

### Settings (all in `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DASHBOARD_PORT` | `5173` | Host port for the dashboard (change it if 5173 is taken) |
| `POSTGRES_PASSWORD` | `ashwax` | Database password. Change it if you ever expose the database port |
| `KITE_ACCESS_TOKEN` | — | Kite's daily session token (see below) |

### Where your data lives

| Data | Location |
|---|---|
| Configuration (`config/*.json`) | your project folder, mounted into the container, so edits in the dashboard Configuration tab are saved there |
| Database | Docker volume `ashwax_pgdata` |
| Logs and downloaded market data | Docker volumes `ashwax_ashwax_logs` and `ashwax_ashwax_data` |

### Everyday commands

```bash
docker compose logs -f app        # follow the application log
docker compose restart app        # restart after editing config
docker compose up -d --build      # rebuild after pulling new code
docker compose down               # stop, keep all data
docker compose down -v            # stop and DELETE the database and logs
```

### Daily Kite login

Kite needs a fresh access token every trading day, and the image has no browser for the
automated login. Each morning put the day's token into `KITE_ACCESS_TOKEN` in `.env`, then run
`docker compose up -d` again. Without a valid token the app starts, but live quotes fail.

Instead of `.env`, you can pass the login password and TOTP secret as
`ASHWAX_SECRET_KITE_LOGIN_<USERID>` and `ASHWAX_SECRET_KITE_TOTP_<USERID>`; the Windows
credential store is not available in a container.

### Limits of the Docker setup

- **Paper trading only.** Live trading from a container is untested — don't use it for real money.
- The dashboard runs from the Vite development server inside the image. It is fine for one
  user on a trusted machine, but don't put it on the public internet.
- `restart` is deliberately off: if the app stops it stays stopped until you look at the logs,
  the same as the Windows service.
- The dashboard shows an error panel instead of going blank if one tab fails to render; the
  error text is also in the browser console.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `app` exits straight away with `KeyError: 'KITE_API_KEY'` | Add the Kite keys to `.env` and run `docker compose up -d` |
| Port already allocated | Set `DASHBOARD_PORT` to a free port in `.env` |
| Dashboard loads but shows no live prices | `KITE_ACCESS_TOKEN` is missing or expired — refresh it |
| `docker compose` can't connect to the daemon | Start Docker Desktop first |
| Start over completely | `docker compose down -v`, then `docker compose up -d --build` |
