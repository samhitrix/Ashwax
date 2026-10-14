# AshwaX

Autonomous, **risk-first** trading system for Indian markets (NSE equities and NIFTY / BANKNIFTY
index options). Every order — from any strategy, paper or live — passes an 11-gate Central
Risk Manager and an independent LLM second opinion before it reaches a broker.

> **Paper trading is the default and this repository ships paper-locked.** Nothing here is
> financial advice. If you switch anything to live, you alone are responsible for the orders
> it places.

![Dashboard overview (sample data)](assets/screenshots/overview.png)

<sub>All screenshots in this README use made-up sample data, not a real account.</sub>

**Contents:** [How it works](#how-it-works) · [Supported brokers](#supported-brokers) ·
[Phase 1](#phase-1--what-is-in-this-release) · [Roadmap](#roadmap) ·
[Dashboard tour](#dashboard-tour) · [Quick start](#quick-start) ·
[Configuration](#configuration-at-a-glance) · [Safety design](#safety-design) ·
[Project layout](#whats-inside)

---

## How it works

![How AshwaX works](assets/how-it-works.svg)

1. **Data in** — live prices and option chains from the broker, NSE reference files (bhavcopy,
   instruments, holidays, expiries), fundamentals (yfinance), a news feed tagged by a background
   LLM, and a market-regime / breadth model. Everything is stored in PostgreSQL, which is the
   single source of truth for state.
2. **Strategy engines** — each strategy enabled in `config/strategies_registry.json` produces a
   signal that already includes its entry, stop and target:
   - **Swing (CNC)** — rule-based specs (USS JSON, compiled) plus an LLM idea-generation agent.
   - **Intraday (MIS)** — ideas are pooled and ranked as a batch; nothing is taken just because it
     arrived first.
   - **Index-options seller desk** — defined-risk credit spreads and condors, expiry-day and
     intraday strangles with per-leg stops, a Greeks budget, IV-rank and event blackouts, and
     frozen rule-based adjustments.
3. **Central Risk Manager (CRM)** — gates G0–G10 run in order: kill switch, hard caps, circuit and
   daily-loss breakers, time windows, regime, news veto, strategy health, portfolio and margin,
   stop-loss and %-of-equity sizing, LLM confirmation, autonomy mode. The first gate that fails
   stops the trade. No strategy can bypass the CRM.
4. **AI second opinion (G9)** — a reviewer chain with automatic failover (Gemini, Cloudflare
   Workers AI, Groq, Anthropic, Ollama). The reviewer is never the model that came up with the
   idea. It can veto a trade but never start or enlarge one, and every verdict is written to the
   database.
5. **Execute** — fills go to the paper engine (with simulated costs) unless that segment **and**
   that strategy are both switched to live. Protective stops are placed first. An order monitor
   and a reconciler keep the database in line with the broker, and every fill is recorded in an
   append-only trade ledger.
6. **Watch & learn** — supervisor invariants, Telegram alerts and a React dashboard that shows
   only real data. A learning engine tracks expectancy, profit factor and slippage and feeds
   strategy-health scores back into gate G6. It never promotes a strategy to live on its own.

---

## Supported brokers

| Broker | Status | Notes |
|---|---|---|
| **Paper (built-in)** | ✅ Default, fully supported | Simulated fills, stops, GTT-style OCO and costs. No account needed. Everything in the dashboard works with it. |
| **Zerodha Kite Connect** | ✅ Primary live broker | Quotes, option chains, historical data, margins, orders, native GTT for swing stops, automated daily TOTP login. |
| **Angel One SmartAPI** | 🟡 Adapter implemented, not yet validated live | Quotes, history, option quotes, order placement / status. It has no native GTT, so stops use the manual SL / target-order fallback. Treat as experimental. |
| **Upstox** | 🔲 Planned | Capability profile exists in `src/brokers/capability_matrix.json`; no adapter yet. |

Select the broker with `BROKER=paper | kite | angel_one` in `.env` (unset = paper). Every adapter
implements the same `BrokerInterface` (`src/brokers/base.py`), so the CRM, strategies and
dashboard never call a broker directly. Per-broker differences — product codes, GTT support,
bracket orders, websocket limits — are declared in `src/brokers/capability_matrix.json`.

Exchanges and instruments: NSE cash equities (CNC delivery and MIS intraday) and NSE index
options on NIFTY / BANKNIFTY. Futures, commodities and currency are out of scope.

---

## Phase 1 — what is in this release

Phase 1 is a **paper-first trading lab with a hardened risk core**. The goal is to collect
honest, after-cost forward results for each strategy before any real money is involved.

**Included**

- Central Risk Manager (G0–G10), %-of-equity sizing, drawdown size steps, daily-loss breaker with
  force-flatten, absolute equity floor, and a master paper lock.
- Paper and live execution paths that share one code path, with protective stops placed first.
- Order monitor, reconciler against the broker, GTT backup orders, append-only trade ledger.
- Independent LLM review (G9) with a multi-provider failover chain and per-purpose daily call caps.
- News ingestion and classification, market regime, breadth, and sector rotation models.
- Learning engine: expectancy, calibration of LLM confidence, failure attribution, strategy
  health tiers that shrink position size automatically.
- React dashboard with 17 tabs, Telegram alerts, PostgreSQL storage with auto-applied migrations.
- Backtest and research scripts in `scripts/` and `scripts/research/`, run at 0.10 % cost.

**Strategies shipped (all `execution_mode: "paper"`)**

| Strategy | Segment | State |
|---|---|---|
| `breadth_gated_breakout` | Swing | Enabled, paper. Takes no trades while market breadth is weak. |
| `llm_swing_agent` | Swing | Enabled, paper. LLM proposes ideas; CRM and G9 gate them. |
| `llm_intraday_agent` | Intraday | Enabled, paper-locked. |
| `options_seller_desk_v3` | Options | Enabled, paper. Defined-risk credit spreads on NIFTY / BANKNIFTY. |
| `nifty_weekly_condor_simple` | Options | Enabled, paper. Weekly iron condor. |
| `options_expiry_day` | Options | Enabled, paper. NIFTY expiry-day short strangle with per-leg stops. |
| `options_intraday_short_premium` | Options | Enabled, paper. Daily NIFTY strangle with per-leg stop. |
| `minervini_vcp`, `oneil_canslim`, `news_catalyst_momentum` | Swing / intraday | **Disabled.** Kept as reference; `minervini_vcp` failed its hold-out test. |

A strategy being enabled does **not** mean it has a proven edge. Treat every result as
unproven until it clears the promotion bar in [Going live](INSTALL.md#14-before-you-ever-go-live).

**Known limits of Phase 1**

- Windows is the tested platform. Stored broker credentials use the Windows machine-wide
  DPAPI store; Linux / macOS work for paper trading only.
- No container images yet — run it with Python, PostgreSQL and Node as described in
  [INSTALL.md](INSTALL.md).
- Only Kite Connect is run against real accounts. Angel One is unvalidated.
- Option stop-losses are enforced by the system, not by resting broker-side orders.

---

## Roadmap

The items below are plans, not promises, and are not in this release. They are ordered by
how much they reduce risk, not by how exciting they are.

**Phase 2 — safe to run live, anywhere**

- Broker-side resting stop orders for option legs, so a stop survives a crash of this process.
- Docker image and `docker-compose` (app + PostgreSQL + Redis), with a cross-platform secret
  store so the stack runs on Linux without Windows DPAPI.
- Validate the Angel One adapter end to end in paper-vs-broker comparison runs.
- Promotion tooling: a report that checks each strategy against the written bar (trade count,
  profit factor, expectancy, slippage vs model) before `execution_mode` may be changed.

**Phase 3 — broader coverage**

- Upstox adapter, using the capability profile already declared.
- Walk-forward and Monte-Carlo validation built into the backtester, with out-of-sample
  reports saved next to each strategy.
- Better execution quality: slippage model calibrated from real fills, smarter order slicing.

**Phase 4 — usability**

- Guided setup in the dashboard (broker login, keys, Telegram) instead of editing `.env`.
- Strategy builder UI on top of the USS JSON spec.
- Hosted / multi-account mode with per-account risk limits.

Ideas that need data before they are built (for example new options structures or intraday
equity edges) are tested in `scripts/research/` first and only become strategies if they pass
out-of-sample after costs.

---

## Dashboard tour

Sample data throughout. Open `http://localhost:5173` after starting the system.

| Tab | What it shows |
|---|---|
| **Overview** — [screenshot](assets/screenshots/overview.png) | Today's P&L, funds and margin, intraday P&L curve, market regime, breadth, FII / DII, open positions, sector strength. |
| **Order Monitor+** — [screenshot](assets/screenshots/order-monitor.png) | Open and closed positions, swing book, GTT backup orders, monitor health and auto-actions. |
| **Options Trading** — [screenshot](assets/screenshots/options.png) | Each options structure with legs, live P&L, max profit / loss, breakevens, Greeks budget. |
| **Paper Trades** — [screenshot](assets/screenshots/paper-trades.png) | Every simulated trade across segments with LLM verdict and regime. |
| **News Intel** — [screenshot](assets/screenshots/news.png) | Classified headlines with sentiment, impact and the trading action taken. |
| **Signals / Scanner** — [screenshot](assets/screenshots/scanner.png) | Nightly VCP setups and relative-strength leaders. |
| **Strategy Studio** — [screenshot](assets/screenshots/strategy-studio.png) | Strategy toggles, paper / live mode per strategy, backtest results and pass / fail. |
| **Intelligence** — [screenshot](assets/screenshots/intelligence.png) | LLM and agent intelligence status. |
| **Learning** — [screenshot](assets/screenshots/learning.png) | Strategy health tiers, win / loss attribution, LLM calibration, detected patterns. |
| **Agent Status** — [screenshot](assets/screenshots/agent-status.png) | Processes, database, broker and LLM connection health. |
| **System Health** — [screenshot](assets/screenshots/system-health.png) | LLM provider chains, gap risk, edge decay, cost erosion. |
| **Self-Healing+** — [screenshot](assets/screenshots/self-healing.png) | Reconciliation runs and automatic repairs. |
| **Configuration** — [screenshot](assets/screenshots/configuration.png) | Live editor for every `config/*.json` file. |
| **Alerts** — [screenshot](assets/screenshots/alerts.png) | Breaches and system alerts. |
| **Trades & P&L** — [screenshot](assets/screenshots/trades-pnl.png) | After-cost P&L by segment, weekly bars, cost-erosion gate. |
| **Reports** — [screenshot](assets/screenshots/reports.png) | Tax and performance reports. |
| **Logs** | Every component's log file, with paging and download. |

---

## Quick start

```bash
git clone <your-fork-url> AshwaX && cd AshwaX
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                                  # set DB_URL at minimum
createdb ashwax                                       # empty PostgreSQL database
cd dashboard_react && npm install && cd ..
python launcher.py                                    # then open http://localhost:5173
```

The full step-by-step guide, including broker login, LLM keys, Telegram, running as a Windows
service and troubleshooting, is in **[INSTALL.md](INSTALL.md)**.

---

## Configuration at a glance

| File | Controls |
|---|---|
| `config/global.json` | segments (`paper_mode` per intraday / swing / options), LLM chains, feature flags |
| `config/capital.json` | every money limit, as **% of live equity** (shared risk pool, drawdown steps) |
| `config/operator.json` | hard limits and `account.paper_mode_locked` (master paper lock) |
| `config/strategies_registry.json` | which strategies are enabled; `execution_mode` paper/live per strategy |
| `config/options_settings.json` | options desk: structures, stops, Greeks budgets, adjustments |
| `config/market_hours.json` | market / AMO windows |
| `config/charges.json` | brokerage, STT, GST and stamp duty for after-cost P&L |

Every file has `_note` fields that explain each key. `config/examples/` keeps a paper-only
baseline that a unit test checks.

---

## Safety design

- **Paper first.** This repo ships with `paper_mode_locked: true`, every segment set to
  `paper_mode: true` and every strategy set to `execution_mode: "paper"`. Going live means
  changing all three on purpose.
- **Risk before profit.** Position size comes from % of live equity and the distance to the
  stop. A daily-loss breaker force-flattens positions and an absolute equity floor halts trading.
- **Fail closed.** If a required LLM verdict, breadth data or another gate input is missing, the
  trade is skipped. It is never guessed through.
- **Backtests only add confidence.** A weak backtest lowers confidence but cannot hard-block a
  trade by itself, and a good backtest is not enough to promote a strategy to live.
- **Everything is logged.** Signals, gate decisions, LLM verdicts, orders and fills are all
  stored and can be replayed.

---

## What's inside

| Area | Where |
|---|---|
| Orchestrator (all loops) | `src/orchestrator.py` |
| Central Risk Manager (G0–G10) | `src/risk/crm.py`, `src/risk/` |
| Orders, paper engine, brokers | `src/execution/`, `src/paper_trading/`, `src/brokers/` |
| Strategies (USS specs, LLM agents) | `strategies/builtin/`, `src/strategies/` |
| Index-options seller desk | `src/options/` |
| LLM providers / failover | `src/llm/` |
| Market data, regime, breadth | `src/market_data/`, `src/market/` |
| News pipeline | `src/news/` |
| Supervisor / learning | `src/supervisor/`, `src/learning/` |
| Dashboard API / UI | `src/api/`, `dashboard_react/` |
| Config (single source of truth) | `config/*.json` |
| DB schema (auto-applied) | `migrations/*.sql` |
| Backtests / research scripts | `scripts/`, `scripts/research/` |

## Tests

```bash
pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
