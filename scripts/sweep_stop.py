"""Sweep a protective stop-loss over real leader tapes.

Question this answers: the live bot has no exit of its own — it either mirrors a
leader's sell or rides the position into binary resolution. Would cutting losers
at a fixed drawdown have helped, or does it just bank the noise before winners
recover?

Two passes. The first runs the normal backtest to learn which tokens actually
get entered, then pulls each one's CLOB price history for its holding window
(cached to disk, so re-sweeps are free). The second replays the same tapes at
every stop level.

Caveat baked into the result: CLOB history is sampled, so a stop fires at the
first *observed* price at/below the trigger. Real fills would be worse — gaps
through the level are invisible here. Treat the stop rows as optimistic.

Usage:
    python -m scripts.sweep_stop --leaders 0xWALLET [0xWALLET ...]
    python -m scripts.sweep_stop            # uses the bot's followed leaders
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.data.price_cache import PriceCache
from scripts._book import shared_book

STOPS = [None, 0.50, 0.40, 0.30, 0.25, 0.20, 0.15, 0.10]
CACHE = os.environ.get("PMBOT_STOP_CACHE", "/tmp/pmb_stop_prices.json")


def followed_leaders(db_path: str) -> list[str]:
    con = sqlite3.connect(db_path)
    try:
        return [r[0] for r in con.execute("select wallet from followed_leaders")]
    finally:
        con.close()


def load_cache() -> dict:
    try:
        with open(CACHE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(c: dict) -> None:
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(c, fh)
    os.replace(tmp, CACHE)


MAX_HOLD_DAYS = 14          # forward window to look for the real resolution
PINNED_LO, PINNED_HI = 0.02, 0.98


def fetch_series(results, prices: PriceCache, cache: dict) -> dict:
    """CLOB price history per entered token, forward from the FIRST entry.

    Deliberately not anchored on Gamma's end_date: for daily-sports markets
    that timestamp is the start of the day, i.e. before the entry, which
    yields an inverted window the API rejects with a 400. Walking forward
    from the entry is always well-formed.
    """
    windows: dict[str, tuple[int, int]] = {}
    now_u = int(time.time())
    for r in results:
        if not r.token_id:
            continue
        lo = int((r.entry_ts - timedelta(hours=1)).timestamp())
        # Cover the entry, whatever Gamma claims, and up to MAX_HOLD_DAYS after.
        hi = int(max(r.resolve_ts, r.entry_ts + timedelta(days=MAX_HOLD_DAYS)).timestamp())
        hi = min(hi, now_u)                   # a future endTs is also a 400
        if r.token_id in windows:
            plo, phi = windows[r.token_id]
            lo, hi = min(lo, plo), max(hi, phi)
        if hi > lo:
            windows[r.token_id] = (lo, hi)

    todo = [(t, w) for t, w in windows.items() if t not in cache]
    print(f"price history: {len(windows)} tokens, {len(todo)} to fetch", flush=True)
    fails = 0
    for i, (token, (lo, hi)) in enumerate(todo, 1):
        span_days = (hi - lo) / 86400
        fidelity = 1 if span_days <= 3 else (10 if span_days <= 14 else 60)
        try:
            hist = prices.get_price_history(
                token, start_ts=lo, end_ts=hi, fidelity_minutes=fidelity
            )
        except Exception as e:
            fails += 1
            if fails <= 5:
                print(f"  !! {token[:12]}… fetch failed: {e}", flush=True)
            hist = []
        cache[token] = hist
        if i % 50 == 0:
            print(f"  {i}/{len(todo)} ({fails} failed)…", flush=True)
            save_cache(cache)
        time.sleep(0.05)                      # be polite to the CLOB
    save_cache(cache)
    if fails:
        print(f"  {fails}/{len(todo)} fetches failed", flush=True)
    return {t: cache.get(t) or [] for t in windows}


def derive_resolutions(series: dict, results) -> dict:
    """Infer each token's true settlement instant from its own price tape.

    A resolved binary token pins at ~0 or ~1 and stays there; the first such
    point after entry is the resolution. Only used where it beats Gamma's
    end_date, so nothing regresses for markets Gamma stamps correctly.
    """
    first_entry: dict[str, datetime] = {}
    gamma_end: dict[str, datetime] = {}
    for r in results:
        if not r.token_id:
            continue
        if r.token_id not in first_entry or r.entry_ts < first_entry[r.token_id]:
            first_entry[r.token_id] = r.entry_ts
        gamma_end[r.token_id] = r.resolve_ts

    out: dict[str, datetime] = {}
    for token, hist in series.items():
        entry = first_entry.get(token)
        if not hist or entry is None:
            continue
        entry_u = entry.timestamp()
        pinned_from = None
        for t, p in hist:                     # ascending
            if t <= entry_u:
                continue
            if p <= PINNED_LO or p >= PINNED_HI:
                if pinned_from is None:
                    pinned_from = t
            else:
                pinned_from = None            # came back off the peg: not resolved
        if pinned_from is None:
            continue
        derived = datetime.fromtimestamp(pinned_from, tz=timezone.utc)
        # Only override when Gamma's stamp is unusable (at//before the entry).
        if gamma_end.get(token) and gamma_end[token] > entry:
            continue
        out[token] = derived
    return out


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leaders", nargs="*", default=None)
    ap.add_argument("--lookback", type=int, default=45)
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args(argv)

    s = get_settings()
    leaders = [w.lower() for w in (args.leaders or followed_leaders(s.db_path))]
    if not leaders:
        print("no leaders (pass --leaders, or run where the bot's db lives)")
        sys.exit(2)

    data, gamma, prices = PolymarketDataClient(), GammaClient(), PriceCache()
    bt = ExactCopyBacktester(data, gamma, trades_limit=args.limit)

    t0 = time.time()
    print(f"fetching tapes for {len(leaders)} leaders…", flush=True)
    tapes = bt.fetch_tapes(leaders)
    # Quote the book once for the whole tape: `simulate` prices every fill
    # off it and skips what it cannot quote, exactly as the live executor
    # does. Without this the run fills at the leader's own price.
    shared_book(bt, tapes)
    for w, tape in tapes.items():
        print(f"  {w[:12]}… {len(tape)} trades", flush=True)

    base = bt.simulate(tapes, lookback_days=args.lookback)
    bm = base.metrics()
    print(f"\nbaseline: {bm['n_trades']} trades, net ${bm['net_pnl']:,.2f} "
          f"({time.time()-t0:.0f}s)", flush=True)
    if bm["n_trades"] == 0:
        print("nothing copyable in this window — cannot test a stop.")
        return

    series = fetch_series(base.results, prices, load_cache())
    covered = sum(1 for v in series.values() if v)
    print(f"got history for {covered}/{len(series)} tokens", flush=True)

    resolves = derive_resolutions(series, base.results)
    print(f"derived a real resolution time for {len(resolves)} tokens whose "
          f"Gamma end_date precedes the entry\n", flush=True)

    # Re-baseline WITH the corrected holding periods, so the stop rows are
    # compared against the same world they run in.
    base = bt.simulate(tapes, lookback_days=args.lookback, resolve_at=resolves)
    held = sum(
        (r.resolve_ts - r.entry_ts).total_seconds() / 3600 for r in base.results
    ) / max(1, len(base.results))
    print(f"mean modelled hold: {held:.1f}h across {len(base.results)} trades\n", flush=True)

    print(f"{'stop':>6} {'n':>4} {'stopped':>8} {'invested':>11} {'net_pnl':>10} "
          f"{'roi%':>7} {'win%':>6} {'maxDD':>9}")
    rows = []
    for stop in STOPS:
        rep = bt.simulate(
            tapes, lookback_days=args.lookback, resolve_at=resolves,
            stop_loss_frac=stop, price_series=series if stop else None,
        )
        m = rep.metrics()
        n_stopped = sum(1 for r in rep.results if r.closed_by == "stop-loss")
        rows.append((stop, m, n_stopped))
        label = "none" if stop is None else f"-{stop*100:.0f}%"
        print(f"{label:>6} {m['n_trades']:>4} {n_stopped:>8} "
              f"${m['invested']:>10,.0f} ${m['net_pnl']:>9,.2f} "
              f"{m['roi']*100:>6.1f} {m['win_rate']*100:>5.1f} "
              f"${m['max_drawdown']:>8,.2f}", flush=True)

    # --- controlled re-run -------------------------------------------------
    # Above, a stop frees bankroll and per-leader caps early, so tighter stops
    # admit MORE trades and the P&L columns compare different trade sets. Rerun
    # with the caps effectively removed: every candidate is taken in every
    # config, so trade count is constant and the stop is the only variable.
    uncapped = s.model_copy(update={              # Settings is pydantic, not a dataclass
        "bankroll_usd": 1e9, "max_per_market_usd": 1e9, "max_per_leader_usd": 1e9,
    })
    print("\ncontrolled (caps removed, so every config takes the same trades):")
    print(f"{'stop':>6} {'n':>4} {'stopped':>8} {'invested':>11} {'net_pnl':>10} "
          f"{'roi%':>7} {'win%':>6} {'maxDD':>9}")
    for stop in STOPS:
        rep = bt.simulate(
            tapes, lookback_days=args.lookback, resolve_at=resolves, settings=uncapped,
            stop_loss_frac=stop, price_series=series if stop else None,
        )
        m = rep.metrics()
        n_stopped = sum(1 for r in rep.results if r.closed_by == "stop-loss")
        label = "none" if stop is None else f"-{stop*100:.0f}%"
        print(f"{label:>6} {m['n_trades']:>4} {n_stopped:>8} "
              f"${m['invested']:>10,.0f} ${m['net_pnl']:>9,.2f} "
              f"{m['roi']*100:>6.1f} {m['win_rate']*100:>5.1f} "
              f"${m['max_drawdown']:>8,.2f}", flush=True)

    print("\nhow trades ended, by stop level:")
    for stop, _m, _n in rows:
        rep = bt.simulate(
            tapes, lookback_days=args.lookback, resolve_at=resolves,
            stop_loss_frac=stop, price_series=series if stop else None,
        )
        by: dict[str, list[float]] = {}
        for r in rep.results:
            by.setdefault(r.closed_by, []).append(r.pnl)
        label = "none" if stop is None else f"-{stop*100:.0f}%"
        parts = [f"{k}: n={len(v)} ${sum(v):,.0f}" for k, v in sorted(by.items())]
        print(f"  {label:>6}  " + "   ".join(parts))

    data.close()
    gamma.close()
    prices.close()


if __name__ == "__main__":
    main(sys.argv[1:])
