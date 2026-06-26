# pmbot — Prediction-Market Copy-Trading Bot

Paper-first copy-trading bot for **Polymarket**. It auto-discovers strong leader
wallets from Polymarket's public leaderboard, scores them, and mirrors their
long-horizon trades — first in **simulation (paper mode)**, with live execution
gated behind a compliance check.

> Status: **paper-trading ready** (Phases 0–5 complete: data layer, ledger,
> paper executor, auto leader selection + Strategy #4, backtester, monitoring).
> Live execution (Phase 6) is intentionally gated. Strategies #1 (arb) and #2
> (selective copy) are next.

## ⚠️ Compliance notice — read before going live

- This bot reads **public, read-only** market data (Polymarket Data/Gamma APIs).
  Reading public data and running **paper mode** (no real orders) is fine.
- **International Polymarket trading/funding is geoblocked for US persons.** The
  regulated **Polymarket US** venue does not expose other traders' individual
  trades, so true copy-of-leaders is not possible there.
- **Live mode is OFF by default.** Only enable it when you are *lawfully able to
  trade and fund the venue from your current location*, and in accordance with
  the venue's residency/KYC terms. This project contains **no** geoblock or KYC
  circumvention and will not add any.
- Trading involves risk of loss. With < $1k, fees + slippage make net profit
  hard — treat early use as learning and validation.

## Strategies (incremental)

1. **#4 Long-term outcome copy** (current) — copy leaders' trades on markets
   resolving far out, where execution lag matters least.
2. **#1 Cross-platform arbitrage** (later) — Polymarket vs. Kalshi price gaps.
3. **#2 Selective copy** (later) — refined leader scoring + manual override.

## Quick start (paper)

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# bash:               source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env        # defaults are fine for paper mode

pmbot paper                 # run the copy loop in simulation (Ctrl+C to stop)
pmbot paper --cycles 1      # single cycle then exit
pmbot status                # portfolio summary + open paper positions
pmbot backtest --lookback 180   # vet auto-selected leaders on resolved history
pmbot backtest --leaders 0xabc...,0xdef...   # backtest specific wallets
pmbot live                  # refuses — gated until Phase 6
```

Tune which/how-many leaders to follow in `pmbot/config/leaders.yaml`
(thresholds + `top_n`); tune sizing/horizon in `.env`.

Run tests:

```bash
pytest
```

Helper scripts (read-only, hit live public APIs):

```bash
python scripts/verify_data_layer.py    # data clients end-to-end
python scripts/verify_engine.py        # selection + one paper cycle
python scripts/run_backtest_demo.py    # backtest on resolved markets
```

## Layout

```
pmbot/
  config/    settings (env-driven) + leaders.yaml (scoring/selection params)
  data/      public Polymarket Data API + Gamma API clients, price cache
  leaders/   leaderboard discovery + wallet scoring (auto-selection)
  strategy/  signal generators (longterm_copy, arbitrage, selective_copy)
  risk/      sizing + position/leader caps + slippage budget
  execution/ paper executor (now), live executor (gated)
  portfolio/ SQLite ledger: positions, fills, P&L
  engine.py  poll -> strategy -> risk -> executor -> ledger
  cli.py     run modes: paper | backtest | live(gated)
```
