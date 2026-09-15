"""Score candidate rosters as COHORTS, the way the bot actually trades them.

`wallet_census.py` scores each wallet alone with the whole bankroll, which is
the right way to judge a wallet but the wrong way to judge a ROSTER. Live, the
leaders share one $500: whoever trades first takes the capital, and a wallet
whose signal arrives when the bankroll is committed simply does not get copied.
That is why the same leaderboard cohort swung +18.8% -> -27.9% purely on how
deep the list was cut.

So roster size is not a free parameter, and it cannot be reasoned about from
per-wallet numbers. This runs whole candidate lineups through the same
quoted-book backtest, competing for the same bankroll, and reports what each
would have returned.

Read the output with one caveat firmly in mind: every arm here is built from
wallets chosen on this same data, so the LEVELS are in-sample and optimistic.
The comparison between arms -- especially the effect of adding more names --
is the part worth trusting, because all arms share that bias equally.

Usage:
    python -m scripts.roster_arms --days 90
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.leaders import load_leader_config
from scripts._book import shared_book

BANKROLL = 500.0
BAND = (0.15, 0.85)
MIN_LEADER_NOTIONAL = 150.0
SLIPPAGE_BPS = 60.0
BOOK_STALENESS = 180.0

_TMP = os.environ.get("TEMP", "/tmp")
TAPE_CACHE = os.path.join(_TMP, "pmb_lb_tapes.pkl")
MKT_CACHE = os.path.join(_TMP, "pmb_lb_markets.pkl")

# The control arm must be the lineup the bot is actually running, so it is read
# from leaders.yaml rather than pinned. It used to be pinned, and by 2026-09-13
# the pin was the 8 wallets of the morning of 2026-09-02 -- the arm labelled
# "current" was a lineup that had been replaced the same day it was written.
CURRENT = [w.lower() for w in load_leader_config().roster]


def peak_concurrent(results) -> float:
    ev = []
    for r in results:
        ev.append((r.entry_ts, r.size_usd))
        ev.append((max(r.resolve_ts, r.entry_ts), -r.size_usd))
    ev.sort(key=lambda e: (e[0], e[1]))
    cur = peak = 0.0
    for _, d in ev:
        cur += d
        peak = max(peak, cur)
    return peak


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--candidates", default="",
                    help="JSON from scripts.census_strict (default: the file "
                         "it writes). These are the wallets that cleared the "
                         "strict bar; arms are built from them.")
    ap.add_argument("--per-candidate", type=int, default=4,
                    help="also run LIVE+<wallet> for this many top candidates, "
                         "which is the only arm that isolates one wallet's "
                         "contribution to the lineup we actually run")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    s = get_settings().model_copy(update={
        "bankroll_usd": BANKROLL, "copy_fraction": 0.10, "max_per_market_pct": 0.03,
        "max_per_leader_usd": 400.0, "compound_profits": False,
        "copy_price_min": BAND[0], "copy_price_max": BAND[1],
        "copy_min_leader_notional_usd": MIN_LEADER_NOTIONAL, "slippage_bps": SLIPPAGE_BPS,
    })
    cand_path = args.candidates or os.path.join(_TMP, "pmb_strict.json")
    try:
        cands = json.load(open(cand_path))
    except (OSError, ValueError):
        print(f"no candidate file at {cand_path} — run scripts.census_strict first")
        return 1
    tapes = pickle.load(open(TAPE_CACHE, "rb"))
    kept = {c["w"]: c for c in cands}

    # `census_strict` already applied the bar and ordered by the shrunk score,
    # so this reads the list rather than re-deriving it. Anything that needs a
    # different bar belongs in that script, where the bar is documented.
    strict = sorted(kept.values(), key=lambda c: -c["shrunk"])
    strict_w = [c["w"] for c in strict]
    evidence_w = [c["w"] for c in strict if c["n90"] >= 100]
    print(f"{len(strict)} candidates cleared the bar; "
          f"{len(evidence_w)} of them on 100+ closed copies")

    def dedupe(ws):
        return list(dict.fromkeys(ws))

    # The arms answer three separate questions and are labelled by which:
    #   SIZE      -- how many leaders can the one bankroll carry (the 2026-09-02
    #                finding was monotone: fewer is better, and peak concurrent
    #                capital climbs past the $500 well before ROI stops falling)
    #   SWAP      -- does any candidate improve the lineup we actually run
    #   ISOLATE   -- what does ONE candidate add to the live roster, which is
    #                the only arm that separates roster contribution from solo
    #                quality (a wallet with the best solo estimate we had cost
    #                6.8pp of roster ROI by crowding the bankroll)
    arms = {
        f"LIVE roster ({len(CURRENT)})": CURRENT,                      # control
    }
    for k in (1, 2, 3, 5, 8):
        if len(strict_w) >= k:
            arms[f"SIZE: strict top {k}"] = dedupe(strict_w[:k])
    for k in (3, 5):
        if len(evidence_w) >= k:
            arms[f"SIZE: high-evidence top {k}"] = dedupe(evidence_w[:k])
    new_w = [w for w in strict_w if w not in set(CURRENT)]
    for k in (1, 2, 3):
        if len(new_w) >= k:
            arms[f"SWAP: LIVE + top {k} new"] = dedupe(CURRENT + new_w[:k])
    for w in new_w[:max(args.per_candidate, 0)]:
        c = kept[w]
        label = f"ISOLATE: LIVE + {w[:10]} ({c['r90']:+.0f}%/n{c['n90']})"
        arms[label] = dedupe(CURRENT + [w])
    # Dropping each incumbent in turn: a seat is only worth holding if the
    # lineup is worse without it.
    for w in CURRENT:
        if len(CURRENT) > 1:
            arms[f"DROP: LIVE minus {w[:10]}"] = [x for x in CURRENT if x != w]

    # Research reads HISTORY, so the CDN's cached copy is exactly as good as
    # the origin's and costs them nothing. `copy_bypass_feed_cache` is a live
    # copy-path setting (it buys the bot seconds on a trade it is about to
    # mirror); inherited here it would only push a whole sweep -- thousands of
    # wallets -- through someone else's origin for no gain.
    data, gamma = PolymarketDataClient(bypass_cache=False), GammaClient()
    bt = ExactCopyBacktester(data, gamma, s, slippage_bps=SLIPPAGE_BPS, trades_limit=3000)
    if os.path.exists(MKT_CACHE):
        with open(MKT_CACHE, "rb") as fh:
            bt._mkt_cache.update(pickle.load(fh))

    universe = dedupe([w for ws in arms.values() for w in ws])
    sub = {w: tapes.get(w, []) for w in universe}
    print(f"warming: {len(universe)} wallets across {len(arms)} arms, "
          f"{args.days}-day window", flush=True)
    bt.prefetch_markets([t.market_id for tp in sub.values() for t in tp
                         if t.timestamp >= now - timedelta(days=args.days)])
    shared_book(bt, sub, quiet=True, lookback_days=args.days, now=now)

    print(f"\n{'arm':<26}{'wallets':>8}{'trades':>8}{'deployed$':>11}{'net$':>10}"
          f"{'ROI':>9}{'win%':>7}{'peak$':>8}")
    rows = []
    for name, ws in arms.items():
        rep = bt.simulate({w: tapes.get(w, []) for w in ws}, lookback_days=args.days,
                          now=now, min_leader_notional=MIN_LEADER_NOTIONAL,
                          price_min=BAND[0], price_max=BAND[1], settings=s,
                          slippage_bps=SLIPPAGE_BPS, skip_round_tripped_entries=True,
                          book_max_staleness_seconds=BOOK_STALENESS)
        rs = [dc_replace(r, resolve_ts=max(r.resolve_ts, r.entry_ts)) for r in rep.results]
        closed = [r for r in rs if r.resolve_ts <= now]
        if not closed:
            print(f"{name:<26}{len(ws):>8}   no closed trades")
            continue
        inv = sum(r.size_usd for r in closed)
        net = sum(r.pnl for r in closed)
        wr = sum(1 for r in closed if r.pnl > 0) / len(closed) * 100
        print(f"{name:<26}{len(ws):>8}{len(closed):>8}{inv:>11,.0f}{net:>+10,.0f}"
              f"{net / inv * 100:>+8.2f}%{wr:>6.1f}%{peak_concurrent(closed):>8,.0f}",
              flush=True)
        rows.append((name, ws, net / inv * 100, net, len(closed)))
    with open(MKT_CACHE, "wb") as fh:
        pickle.dump(bt._mkt_cache, fh)

    if rows:
        best = max(rows, key=lambda r: r[2])
        print(f"\nbest ROI: {best[0]} ({best[2]:+.2f}% on {best[4]} trades)")
        print("members:")
        for w in best[1]:
            c = kept.get(w)
            tag = "  (live roster)" if w in CURRENT else ""
            extra = (f"  90d {c['r90']:+.1f}% n={c['n90']}" if c else "")
            print(f"  {w}{extra}{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
