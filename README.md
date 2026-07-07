# pmbot — Prediction-Market Copy-Trading Bot

Paper-first copy-trading bot for **Polymarket** (+ **Kalshi** market data). It
auto-discovers the most profitable recently-active leader wallets from public
trade data, **backtest-vets each one as a copy target**, then mirrors their
entries *and* exits in near real time (sells proportional to the fraction the
leader exited). A cross-venue Polymarket↔Kalshi arbitrage strategy is also
included (off by default) — all in **simulation (paper mode)**, with live
execution gated behind a compliance check.

> Status: **paper-trading ready** (Phases 0–5 complete: data layer, ledger,
> paper executor, auto leader selection + vetting, Strategy #5 exact copy,
> exact-copy backtester, monitoring; plus Strategy #1 cross-platform
> arbitrage: Kalshi client, pair matcher, parallel scanner, atomic two-leg
> paper fills, settlement, arb backtester).
> Live execution (Phase 6) is intentionally gated.

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

## Strategies

1. **#5 Exact copy** (default) — mirror vetted leaders' BUYs *and* SELLs.
   Entries are risk-sized (a fraction of the leader's notional, capped per
   market/leader/bankroll) and filtered: no extreme prices, no thin markets,
   no small low-conviction probes, and no *stale* copies (skipped when the
   quote already drifted past the leader's fill — the edge is gone). Exits
   are never filtered: when a leader sells 40% of a position, the bot sells
   40% of its copy. Leaders are auto-selected by realized-P&L scoring, then
   **vetted**: each candidate's recent tape is backtested as a copy (our
   sizing + slippage) and wallets that don't copy profitably are dropped —
   a leader can be profitable yet not copyable.
2. **#1 Cross-platform arbitrage** (off by default: `PMBOT_ARB_ENABLED=true`)
   — buy YES on one venue and NO on the other when the pair costs < $1 after
   Kalshi fees (`ceil(0.07·C·P·(1−P))` per order) and a slippage buffer.
   Exactly one leg pays $1 at resolution, locking the gap in as profit.
   **Only human-confirmed pairs in `pmbot/config/arb_pairs.yaml` are traded**
   — fuzzy matching only *suggests* candidates, because two similar-sounding
   markets can resolve differently.
3. **#4 Long-term outcome copy** (legacy, not wired by default) — BUY-only
   copies held to resolution in markets resolving far out.

## Quick start (paper)

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# bash:               source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env        # defaults are fine for paper mode

pmbot paper                 # run copy + arb loop in simulation (Ctrl+C to stop)
pmbot paper --cycles 1      # single cycle then exit
pmbot status                # portfolio summary + open paper positions (both venues)
pmbot settle                # realize P&L for positions whose markets resolved
pmbot backtest --lookback 60    # exact-copy backtest of auto-selected leaders
pmbot backtest --leaders 0xabc...,0xdef...   # backtest specific wallets
python -m scripts.sweep_copy 0xabc... 0xdef...  # parameter sweep over leader tapes

pmbot arb-scan --suggest    # fuzzy-match open PM<->Kalshi markets -> candidate pairs
pmbot arb-scan              # scan CONFIRMED pairs (arb_pairs.yaml) for live edge
pmbot arb-backtest --auto   # replay resolved auto-matched pairs (analysis only)
pmbot arb-backtest          # replay confirmed pairs' history

pmbot live                  # refuses — gated until Phase 6
```

Arbitrage workflow: `arb-scan --suggest` → **read both markets' resolution
rules** → paste vetted stubs into `pmbot/config/arb_pairs.yaml` → the paper
loop trades them automatically (atomic two-leg fills, Kalshi fees modeled).

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
python scripts/verify_arb.py           # Kalshi client + matcher + arb scanner
python scripts/run_backtest_demo.py    # backtest on resolved markets
```

## Layout

```
pmbot/
  config/    settings (env-driven), leaders.yaml, arb_pairs.yaml (vetted pairs)
  data/      Polymarket Data/Gamma/CLOB clients + Kalshi public client
  leaders/   leaderboard discovery + wallet scoring (auto-selection)
  arb/       Strategy #1 internals: fee math, pair matcher, scanner, backtest
  strategy/  signal generators (exact_copy, longterm_copy, arbitrage)
  risk/      sizing + position/leader caps + arb group gate
  execution/ paper executor (atomic two-leg arb fills), live executor (gated)
  portfolio/ SQLite ledger (venue-aware): positions, fills, fees, P&L; settlement
  engine.py  poll -> strategies -> risk -> executor -> ledger (+ settle)
  cli.py     paper | backtest | arb-scan | arb-backtest | status | settle | live(gated)
```
