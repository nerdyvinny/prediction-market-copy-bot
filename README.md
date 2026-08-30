# pmbot — Prediction-Market Copy-Trading Bot

Paper-first copy-trading bot for **Polymarket**. It mirrors a hand-picked set
of leader wallets' entries *and* exits in near real time (sells proportional to
the fraction the leader exited), in **simulation (paper mode)**, with live
execution gated behind a compliance check.

Who gets copied is a static list in `pmbot/config/leaders.yaml`, maintained by
hand. The bot used to pick leaders itself — discover, score, vet, re-rank
nightly — and that was removed after 34 days of paper trading measured the
premise it rested on at roughly zero. See [docs/roster.md](docs/roster.md) for
the numbers. Discovery and scoring survive as a research command
(`pmbot leaders`) whose output is advisory only.

> Status: **paper-trading ready** (data layer, ledger, paper executor,
> Strategy #5 exact copy off a static roster, exact-copy backtester,
> settlement, monitoring). Live execution (Phase 6) is intentionally gated.

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
   40% of its copy. Leaders come from the `roster:` list in
   `pmbot/config/leaders.yaml` — edited by hand, applied at startup. Dropping
   a wallet from the list makes positions we already hold from it exit-only:
   its SELLs are still mirrored, its BUYs ignored.
2. **#4 Long-term outcome copy** (legacy, not wired by default) — BUY-only
   copies held to resolution in markets resolving far out.

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
pmbot settle                # realize P&L for positions whose markets resolved
pmbot leaders               # RESEARCH: rank candidate wallets (never auto-followed)
pmbot leaders --vet         # ...and replay each as WE would have copied it
pmbot backtest --lookback 60    # exact-copy backtest of the ranked candidates
pmbot backtest --leaders 0xabc...,0xdef...   # backtest specific wallets
python -m scripts.sweep_copy 0xabc... 0xdef...  # parameter sweep over leader tapes


pmbot live                  # refuses — gated until Phase 6
```


Change who gets copied by editing `roster:` in `pmbot/config/leaders.yaml`
and restarting; everything else in that file feeds `pmbot leaders` only. Tune
sizing/horizon in `.env`. See [docs/roster.md](docs/roster.md).

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
  config/    settings (env-driven), leaders.yaml (the roster we copy)
  data/      Polymarket Data/Gamma/CLOB clients
  leaders/   discovery + wallet scoring — RESEARCH ONLY, not in the live loop
  strategy/  signal generators (exact_copy, longterm_copy)
  risk/      sizing + position/leader caps
  execution/ paper executor, live executor (gated)
  portfolio/ SQLite ledger (venue-aware): positions, fills, fees, P&L; settlement
  engine.py  poll -> strategies -> risk -> executor -> ledger (+ settle)
  cli.py     paper | leaders | backtest | status | settle | live(gated)
```
