"""Score as many wallets as we can individually, on the live path.

`leaderboard_cohorts.py` asks whether a BOARD is worth copying. This asks a
wider question: across every wallet we can find, how many are actually worth
copying at all, and what does the distribution look like? A month of one wallet
is noise ([[docs/roster.md]] puts the minimum detectable effect at +/-15pp per
trade); a few hundred wallets scored the same way is a distribution, and a
distribution can say whether "find a good leader" is a solvable problem.

Every wallet is replayed ALONE with the whole bankroll. In a shared run the
wallets compete for $500 and whoever trades first takes it, which made the same
cohort swing +18.8% -> -27.9% purely on how deep the list was cut. Solo removes
that; what is left is the wallet.

Pool, largest first, all unioned:
  1. LEADERBOARD — profit AND volume, over 1d/7d/30d/all. Caps at 50 a query
     and ignores `offset`, so this is ~400 rows and maybe 250 unique wallets,
     but they are ranked across all of Polymarket.
  2. FEEDS — every wallet visible in the deepest trade feeds of the busiest
     open and recently-resolved markets. This is where the volume comes from.
  3. RECORDS — wallets the local resolved-market ledger has seen win.
  4. ROSTER — whoever we copy now, as the control.

Cost is dominated by two per-wallet things: resolving every market it touched
(Gamma) and quoting every entry (CLOB). So the pool is cut BEFORE any of that,
using only the tape we already hold: a wallet needs `--min-copyable` BUYs that
clear the notional floor and the price band. That check is free and removed
roughly a third of the leaderboard pool.

Interrupt it whenever. Progress is written after every chunk -- results, the
wallets already scored, the resolved markets, the quoted book -- so re-running
the same command picks up where it stopped and costs nothing for what is done.
`--restart` throws that away and scores from the top.

Usage:
    python -m scripts.wallet_census                        # leaderboard only
    python -m scripts.wallet_census --feeds 60 --db        # everything
    python -m scripts.wallet_census --max-wallets 150      # cap the spend
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

import httpx

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from scripts._book import shared_book

# Live VPS settings, read 2026-08-30. The liquidity floor, drift budget and
# poll interval are NOT pinned -- `simulate` reads those from the settings
# object the bot itself reads.
BANKROLL = 500.0
COPY_FRACTION = 0.10
MAX_PER_MARKET_PCT = 0.03
MAX_PER_LEADER = 400.0
BAND = (0.15, 0.85)
MIN_LEADER_NOTIONAL = 150.0
SLIPPAGE_BPS = 60.0
BOOK_STALENESS = 180.0

ROSTER = [
    "0xcae693bcf9696a2ebf0a62de767719b45f354f85",
    "0x5cd5c8d7a17c78d7389d8b87b611aed83322ac33",
    "0xd487f513cfead22d76b6db4567c756b3cf25053e",
    "0x455e565fdaa9c4d106e1aebd557b8443925af7f9",
    "0xc23dc0eca9e1c2e293de8911b9ac254f0bcd82c8",
    "0x9cdda449d7cedf1072d74982a5dc2349df3d3e97",
    "0x61171892acad4064e139ca4fcb6ce3321a362faf",
]

LB_API = "https://lb-api.polymarket.com"
LB_WINDOWS = ("1d", "7d", "30d", "all")
LB_METRICS = ("profit", "volume")

_TMP = os.environ.get("TEMP", "/tmp")
TAPE_CACHE = os.path.join(_TMP, "pmb_lb_tapes.pkl")      # shared with the cohort script
MKT_CACHE = os.path.join(_TMP, "pmb_lb_markets.pkl")
# Output is keyed by window length: the 30/60/90-day runs are separate
# measurements of the same wallets and must never share a results file.
def out_json(days: int) -> str:
    return os.path.join(_TMP, f"pmb_census_{days}d.json")


def done_cache(days: int) -> str:
    return os.path.join(_TMP, f"pmb_census_done_{days}d.json")
# wallet -> copyable count, for every wallet ever screened. Tiny, and it
# stops a re-run refetching the ~90% that will never be worth scoring.
# NOT window-keyed: it only gates the tape FETCH, and the fetch is the same
# tape whatever window we later score it over.
SCREEN_CACHE = os.path.join(_TMP, "pmb_census_screen.json")


def _log(m: str) -> None:
    print(m, flush=True)


# --- pool ------------------------------------------------------------------


def pool_leaderboard() -> dict[str, str]:
    """wallet -> display name, across every ranking the board will serve."""
    found: dict[str, str] = {}
    with httpx.Client(timeout=30) as c:
        for metric in LB_METRICS:
            for win in LB_WINDOWS:
                try:
                    rows = c.get(f"{LB_API}/{metric}",
                                 params={"window": win, "limit": 50}).json()
                except Exception as e:
                    _log(f"  leaderboard {metric}/{win} failed: {type(e).__name__}")
                    continue
                for r in rows if isinstance(rows, list) else []:
                    w = str(r.get("proxyWallet") or "").lower()
                    if w:
                        found.setdefault(w, r.get("name") or "")
    _log(f"  leaderboard: {len(found)} unique across "
         f"{len(LB_METRICS)}x{len(LB_WINDOWS)} rankings")
    return found


def pool_feeds(data, gamma, *, markets: int, per_market: int) -> set[str]:
    from pmbot.leaders.discovery import profile_candidates
    try:
        profiles = profile_candidates(
            data, gamma, top_open_markets=markets, top_closed_markets=markets,
            per_market_trades=per_market,
        )
    except Exception as e:
        _log(f"  feed sweep failed: {type(e).__name__}: {e}")
        return set()
    # profile_candidates returns {wallet: FeedProfile}; the keys are the pool.
    out = {str(w).lower() for w in (profiles or {})}
    _log(f"  feeds: {len(out)} wallets from {markets} open + {markets} resolved markets")
    return out


def pool_db(db_path: str) -> set[str]:
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute("SELECT DISTINCT wallet FROM wallet_records").fetchall()
        con.close()
    except Exception as e:
        _log(f"  db pool failed: {type(e).__name__}")
        return set()
    out = {str(r[0]).lower() for r in rows if r[0]}
    _log(f"  records: {len(out)} wallets from the local ledger")
    return out


# --- cheap pre-filter ------------------------------------------------------


def copyable_count(tape, cutoff) -> int:
    """BUYs in the window that clear the notional floor and the price band.

    Free -- it reads only the tape we already hold. Everything downstream
    (market resolutions, CLOB quotes) costs a request per market and per token,
    so this is where the pool gets cut.
    """
    from pmbot.models import Side
    return sum(
        1 for t in tape
        if t.timestamp >= cutoff and t.side is Side.BUY
        and t.usd_size >= MIN_LEADER_NOTIONAL and BAND[0] <= t.price <= BAND[1]
    )


def tape_span_days(tape) -> float:
    if not tape:
        return 0.0
    return (max(t.timestamp for t in tape)
            - min(t.timestamp for t in tape)).total_seconds() / 86_400.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--feeds", type=int, default=0,
                    help="also sweep this many open + resolved market feeds")
    ap.add_argument("--per-market", type=int, default=200)
    ap.add_argument("--db", action="store_true", help="also pool the local ledger")
    ap.add_argument("--pool-cached", action="store_true",
                    help="also pool every wallet whose tape is already held")
    ap.add_argument("--min-copyable", type=int, default=5)
    ap.add_argument("--max-wallets", type=int, default=0, help="0 = no cap")
    ap.add_argument("--trades-limit", type=int, default=2000)
    ap.add_argument("--restart", action="store_true",
                    help="discard previous scoring progress and start over")
    ap.add_argument("--chunk", type=int, default=25,
                    help="wallets per book/scoring batch; results persist per chunk")
    args = ap.parse_args()

    OUT, DONE = out_json(args.days), done_cache(args.days)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.days)
    s = get_settings().model_copy(update={
        "bankroll_usd": BANKROLL, "copy_fraction": COPY_FRACTION,
        "max_per_market_pct": MAX_PER_MARKET_PCT, "max_per_leader_usd": MAX_PER_LEADER,
        "compound_profits": False, "copy_price_min": BAND[0], "copy_price_max": BAND[1],
        "copy_min_leader_notional_usd": MIN_LEADER_NOTIONAL, "slippage_bps": SLIPPAGE_BPS,
    })

    _log("=" * 112)
    _log(f"WALLET CENSUS -- every wallet scored alone, live path, "
         f"{cutoff:%Y-%m-%d} .. {now:%Y-%m-%d}")
    _log(f"each gets the full ${BANKROLL:,.0f} | copy {COPY_FRACTION:.0%} | "
         f"cap {MAX_PER_MARKET_PCT:.0%} | band {BAND[0]}-{BAND[1]} | "
         f"floor ${MIN_LEADER_NOTIONAL:,.0f} | slip {SLIPPAGE_BPS:.0f}bps")
    _log("=" * 112)

    data, gamma = PolymarketDataClient(), GammaClient()
    bt = ExactCopyBacktester(data, gamma, s, slippage_bps=SLIPPAGE_BPS,
                             trades_limit=args.trades_limit)
    if os.path.exists(MKT_CACHE):
        with open(MKT_CACHE, "rb") as fh:
            bt._mkt_cache.update(pickle.load(fh))
        _log(f"markets: {len(bt._mkt_cache)} resolutions restored")

    _log("\n[1/4] building the pool")
    names = pool_leaderboard()
    pool = set(names)
    if args.feeds:
        pool |= pool_feeds(data, gamma, markets=args.feeds, per_market=args.per_market)
    if args.db:
        pool |= pool_db(s.db_path)
    if args.pool_cached and os.path.exists(TAPE_CACHE):
        # Everything a previous run already fetched AND kept, without paying
        # for another feed sweep. The tape cache holds exactly the wallets that
        # passed the screen, so this is free breadth.
        with open(TAPE_CACHE, "rb") as fh:
            held = set(pickle.load(fh))
        _log(f"  cached tapes: {len(held)} wallets already fetched")
        pool |= held
    pool |= {w.lower() for w in ROSTER}
    pool = sorted(pool)
    _log(f"  pool: {len(pool)} unique wallets")

    _log(f"\n[2+3/4] fetching tapes (limit {args.trades_limit}) and pre-filtering "
         f"as they land")
    # Fetch and screen in the same pass, keeping ONLY tapes with enough copyable
    # tape to be worth scoring. A pool this size cannot be held whole: 7k tapes
    # at 2k trades is several GB, and rewriting that pickle every batch costs
    # more than the fetching does. So the screen runs per batch and the losers
    # are dropped immediately -- their copyable count is remembered in a small
    # side file so a re-run never refetches them.
    tapes: dict = {}
    if os.path.exists(TAPE_CACHE):
        with open(TAPE_CACHE, "rb") as fh:
            tapes = pickle.load(fh)
    screened: dict[str, int] = {}
    if os.path.exists(SCREEN_CACHE):
        try:
            with open(SCREEN_CACHE) as fh:
                screened = json.load(fh)
        except (OSError, ValueError):
            screened = {}
    todo = [w for w in pool if w not in tapes and w not in screened]
    _log(f"  {len(tapes)} tapes held, {len(screened)} already screened out, "
         f"{len(todo)} to fetch")

    t0, kept_new = time.time(), 0
    for i in range(0, len(todo), 50):
        batch = bt.fetch_tapes(todo[i:i + 50])
        for w, tape in batch.items():
            c = copyable_count(tape, cutoff)
            screened[w] = c
            if c >= args.min_copyable:
                tapes[w] = tape                # worth the Gamma + CLOB spend
                kept_new += 1
        if (i // 50) % 10 == 9 or i + 50 >= len(todo):
            with open(TAPE_CACHE, "wb") as fh:
                pickle.dump(tapes, fh)
            with open(SCREEN_CACHE, "w") as fh:
                json.dump(screened, fh)
        done = min(i + 50, len(todo))
        rate = done / max(time.time() - t0, 1e-9)
        _log(f"  {done}/{len(todo)} fetched, {kept_new} kept "
             f"({time.time() - t0:.0f}s, {rate:.1f}/s, "
             f"eta {(len(todo) - done) / max(rate, 1e-9) / 60:.0f}m)")

    for w in pool:
        if w in tapes and w not in screened:
            screened[w] = copyable_count(tapes[w], cutoff)
    scored_pool = sorted(
        ((screened.get(w, 0), w) for w in pool
         if w in tapes and screened.get(w, 0) >= args.min_copyable),
        reverse=True,
    )
    if args.max_wallets:
        scored_pool = scored_pool[:args.max_wallets]
    keep = [w for _, w in scored_pool]
    _log(f"\n  {len(keep)} of {len(pool)} wallets have >= {args.min_copyable} "
         f"copyable BUYs ({len(pool) - len(keep)} dropped before any API spend)")

    results = []
    if os.path.exists(OUT) and not args.restart:
        try:
            with open(OUT) as fh:
                results = json.load(fh)
        except (OSError, ValueError):
            results = []
    done: set[str] = set()
    if os.path.exists(DONE) and not args.restart:
        try:
            with open(DONE) as fh:
                done = set(json.load(fh))
        except (OSError, ValueError):
            done = set()
    if args.restart:
        results, done = [], set()
    pending = [w for w in keep if w not in done]
    _log(f"\n[4/4] scoring {len(pending)} wallets in chunks of {args.chunk}"
         + (f"  ({len(done)} already done, resuming)" if done else ""))
    t0 = time.time()
    for i in range(0, len(pending), args.chunk):
        batch = pending[i:i + args.chunk]
        sub = {w: tapes[w] for w in batch}
        # Resolve this batch's markets in parallel FIRST. Serially this was the
        # whole cost of a chunk (7,247 markets, 49 of 50 minutes); the replay
        # below then finds every one of them already in the memo.
        n_mkt = bt.prefetch_markets(
            [t.market_id for tp in sub.values() for t in tp if t.timestamp >= cutoff]
        )
        # Pin the book's discovery to the SAME window we score. Left to its
        # default it covers each tape's whole span -- correct for the sweep
        # scripts, which replay several windows, but here it quoted markets
        # from months outside the 30 days and cost most of the chunk.
        shared_book(bt, sub, quiet=True, lookback_days=args.days, now=now)
        for w in batch:
            done.add(w)                      # attempted, even if it yields nothing
            rep = bt.simulate({w: tapes[w]}, lookback_days=args.days, now=now,
                              min_leader_notional=MIN_LEADER_NOTIONAL,
                              price_min=BAND[0], price_max=BAND[1], settings=s,
                              slippage_bps=SLIPPAGE_BPS,
                              skip_round_tripped_entries=True,
                              book_max_staleness_seconds=BOOK_STALENESS)
            rs = [dc_replace(r, resolve_ts=max(r.resolve_ts, r.entry_ts))
                  for r in rep.results]
            closed = [r for r in rs if r.resolve_ts <= now]
            if not closed:
                continue
            inv = sum(r.size_usd for r in closed)
            net = sum(r.pnl for r in closed)
            results.append({
                "wallet": w, "name": names.get(w, ""), "n": len(closed),
                "deployed": round(inv, 2), "net": round(net, 2),
                "roi": round(net / inv * 100, 2) if inv else 0.0,
                "win_rate": round(sum(1 for r in closed if r.pnl > 0) / len(closed) * 100, 1),
                "span_days": round(tape_span_days(tapes[w]), 1),
                "copyable": copyable_count(tapes[w], cutoff),
                "on_roster": w in {r.lower() for r in ROSTER},
            })
        bt.attach_book(None)                 # each chunk quotes only its own tape
        with open(OUT, "w") as fh:
            json.dump(results, fh, indent=1)
        with open(DONE, "w") as fh:
            json.dump(sorted(done), fh)
        with open(MKT_CACHE, "wb") as fh:
            pickle.dump(bt._mkt_cache, fh)
        n_done = min(i + args.chunk, len(pending))
        rate = n_done / max(time.time() - t0, 1e-9)
        _log(f"  {n_done}/{len(pending)} scored (+{n_mkt} markets), "
             f"{len(results)} with closed trades "
             f"({time.time() - t0:.0f}s, eta "
             f"{(len(pending) - n_done) / max(rate, 1e-9) / 60:.0f}m)")

    _log("\n" + "=" * 112)
    _log(f"RESULT -- {len(results)} wallets with at least one closed copy")
    _log("=" * 112)
    if not results:
        return 1
    thick = [r for r in results if r["n"] >= 20 and r["span_days"] >= 7]
    _log(f"  {'wallet':<14}{'name':<20}{'trades':>7}{'net$':>9}{'ROI':>9}"
         f"{'win%':>6}{'tape_d':>8}")
    for r in sorted(thick, key=lambda r: -r["roi"])[:25]:
        _log(f"  {r['wallet'][:12]:<14}{r['name'][:19]:<20}{r['n']:>7}"
             f"{r['net']:>+9.0f}{r['roi']:>+8.1f}%{r['win_rate']:>5.0f}%"
             f"{r['span_days']:>8.1f}")
    import statistics as st
    for label, group in (("all scored", results),
                         (">=20 trades", [r for r in results if r["n"] >= 20]),
                         (">=20 trades, >=7d tape", thick)):
        if not group:
            continue
        rois = [r["roi"] for r in group]
        prof = sum(1 for r in group if r["net"] > 0)
        dep = sum(r["deployed"] for r in group)
        net = sum(r["net"] for r in group)
        _log(f"\n  {label}: n={len(group)}  profitable {prof}/{len(group)} "
             f"({prof/len(group)*100:.0f}%)")
        _log(f"    mean ROI {st.mean(rois):+.2f}%   median {st.median(rois):+.2f}%   "
             f"pooled (dollar-weighted) {net/dep*100 if dep else 0:+.2f}%")
    _log(f"\nwritten to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
