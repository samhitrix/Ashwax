# AshwaX

Autonomous, **risk-first** trading system for Indian markets (NSE equities and NIFTY / BANKNIFTY
index options). Every order — from any strategy, paper or live — passes an 11-gate Central
Risk Manager and an independent LLM second opinion before it reaches a broker.

> **Paper trading is the default and this repository ships paper-locked.** Nothing here is
> financial advice. If you switch anything to live, you alone are responsible for the orders
> it places.

## How it works

![How AshwaX works](assets/how-it-works.svg)

1. **Data in** — live prices and option chains from the broker (Zerodha Kite Connect; Angel One
   optional), NSE reference files (bhavcopy, instruments, holidays, expiries), fundamentals
   (yfinance), a news feed tagged by a background LLM, and a market-regime / breadth model.
   Everything is stored in PostgreSQL, which is the single source of truth for state.
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

## Quick start

```bash
git clone <your-fork-url> AshwaX && cd AshwaX
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                                  # set DB_URL at minimum
createdb ashwax                                       # empty PostgreSQL database
cd dashboard_react && npm install && cd ..
python launcher.py                                    # API :3000 + dashboard :5173
```

The full step-by-step guide, including broker login, LLM keys, Telegram, running as a Windows
service and troubleshooting, is in **[INSTALL.md](INSTALL.md)**.

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

## Tests

```bash
pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
