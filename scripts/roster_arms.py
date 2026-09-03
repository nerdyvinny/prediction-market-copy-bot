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
from scripts._book import shared_book

BANKROLL = 500.0
BAND = (0.15, 0.85)
MIN_LEADER_NOTIONAL = 150.0
SLIPPAGE_BPS = 60.0
BOOK_STALENESS = 180.0

_TMP = os.environ.get("TEMP", "/tmp")
TAPE_CACHE = os.path.join(_TMP, "pmb_lb_tapes.pkl")
MKT_CACHE = os.path.join(_TMP, "pmb_lb_markets.pkl")

CURRENT = [
    "0xcae693bcf9696a2ebf0a62de767719b45f354f85",
    "0x5cd5c8d7a17c78d7389d8b87b611aed83322ac33",
    "0xd487f513cfead22d76b6db4567c756b3cf25053e",
    "0x455e565fdaa9c4d106e1aebd557b8443925af7f9",
    "0x9cdda449d7cedf1072d74982a5dc2349df3d3e97",
    "0x61171892acad4064e139ca4fcb6ce3321a362faf",
    "0xc72d7dcdb23597d143a83536fb97b1d7db7efc21",
    "0xff81cc85838ce8f91a7d4ae2eeddadfa3f8444c9",
]


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
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    s = get_settings().model_copy(update={
        "bankroll_usd": BANKROLL, "copy_fraction": 0.10, "max_per_market_pct": 0.03,
        "max_per_leader_usd": 400.0, "compound_profits": False,
        "copy_price_min": BAND[0], "copy_price_max": BAND[1],
        "copy_min_leader_notional_usd": MIN_LEADER_NOTIONAL, "slippage_bps": SLIPPAGE_BPS,
    })
    tapes = pickle.load(open(TAPE_CACHE, "rb"))
    kept = {c["w"]: c for c in json.load(open(os.path.join(_TMP, "pmb_top25.json")))}

    # Every wallet that cleared the strict bar: positive in all three windows AND
    # in both non-overlapping slices. Sorted by the shrunk score so "top k" means
    # something consistent.
    strict = sorted(
        (c for c in kept.values()
         if c["mid"] is not None and c["old"] is not None
         and c["mid"] > 0 and c["old"] > 0 and c["n90"] >= 50),
        key=lambda c: -c["shrunk"],
    )
    strict_w = [c["w"] for c in strict]
    keepers = ["0xcae693bcf9696a2ebf0a62de767719b45f354f85",
               "0x61171892acad4064e139ca4fcb6ce3321a362faf",
               "0xd487f513cfead22d76b6db4567c756b3cf25053e",
               "0x9cdda449d7cedf1072d74982a5dc2349df3d3e97"]

    def dedupe(ws):
        return list(dict.fromkeys(ws))

    arms = {
        "current 8": CURRENT,
        "proposed 7 (2 out, 1 in)": dedupe(
            [w for w in CURRENT
             if w not in ("0x5cd5c8d7a17c78d7389d8b87b611aed83322ac33",
                          "0x455e565fdaa9c4d106e1aebd557b8443925af7f9")]
            + ["0xf28e42d20e4826f2b10a24bc952001697947cab2"]),
        "strict top 3": dedupe(strict_w[:3]),
        "strict top 5": dedupe(strict_w[:5]),
        "strict top 8": dedupe(strict_w[:8]),
        "strict top 12": dedupe(strict_w[:12]),
        "strict ALL": dedupe(strict_w),
        "strict ALL + keepers": dedupe(strict_w + keepers),
        # Built on EVIDENCE rather than score: the strict-bar wallets whose
        # estimates rest on 100+ closed copies. Same bar, but every member's
        # number is one you can believe.
        "high-evidence top 3": dedupe([c["w"] for c in strict if c["n90"] >= 100][:3]),
        "high-evidence top 5": dedupe([c["w"] for c in strict if c["n90"] >= 100][:5]),
        "high-evidence top 8": dedupe([c["w"] for c in strict if c["n90"] >= 100][:8]),
        # The recommendation: small (the size effect above is monotone under two
        # independent selection rules), every member on 100+ closed copies so
        # the estimate is believable, and every member positive in BOTH
        # non-overlapping slices rather than on one hot streak.
        "FINAL 4": [
            "0xc72d7dcdb23597d143a83536fb97b1d7db7efc21",   # HongYunX
            "0xf28e42d20e4826f2b10a24bc952001697947cab2",
            "0xe015b5a2a299167be835a2fd1e86f09c49e06ffd",   # TAIWANNUMBERONE
            "0xff81cc85838ce8f91a7d4ae2eeddadfa3f8444c9",
        ],
        "FINAL 4 minus ff81cc85": [
            "0xc72d7dcdb23597d143a83536fb97b1d7db7efc21",
            "0xf28e42d20e4826f2b10a24bc952001697947cab2",
            "0xe015b5a2a299167be835a2fd1e86f09c49e06ffd",
        ],
        "RECOMMENDED 5": [
            "0xc72d7dcdb23597d143a83536fb97b1d7db7efc21",   # HongYunX
            "0xe015b5a2a299167be835a2fd1e86f09c49e06ffd",   # TAIWANNUMBERONE
            "0x0353aaf82abbd3e69c00059df0a825bc198fc2ff",   # AGUGava
            "0xf28e42d20e4826f2b10a24bc952001697947cab2",
            "0xff81cc85838ce8f91a7d4ae2eeddadfa3f8444c9",
        ],
        "RECOMMENDED 5 + cae693bc": [
            "0xc72d7dcdb23597d143a83536fb97b1d7db7efc21",
            "0xe015b5a2a299167be835a2fd1e86f09c49e06ffd",
            "0x0353aaf82abbd3e69c00059df0a825bc198fc2ff",
            "0xf28e42d20e4826f2b10a24bc952001697947cab2",
            "0xff81cc85838ce8f91a7d4ae2eeddadfa3f8444c9",
            "0xcae693bcf9696a2ebf0a62de767719b45f354f85",
        ],
        "top3 score + top3 evidence": dedupe(
            strict_w[:3] + [c["w"] for c in strict if c["n90"] >= 100][:3]),
    }

    data, gamma = PolymarketDataClient(), GammaClient()
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
            tag = "  (current roster)" if w in CURRENT else ""
            extra = (f"  90d {c['r90']:+.1f}% n={c['n90']}" if c else "")
            print(f"  {w}{extra}{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
