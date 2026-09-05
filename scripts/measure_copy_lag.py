"""How far behind the leader do we actually copy, and why?

Two independent measurements of the same number, because each alone is
arguable and together they are not:

  --db PATH   Retrospective. For every fill we made, our fill timestamp minus
              the leader's own trade timestamp (`seen_trades.ts`). This is the
              end-to-end truth: feed lag + poll + decision + execution.

  --probe     Live. Poll the Data API's /trades feed and record how old each
              trade is the first time it becomes visible to us at all. This
              separates "the feed is slow" from "our loop is slow", and prints
              the CDN cache headers that explain most of it.

Both were run 2026-09-03 and agree: the feed is ~150s behind, the loop is not
the bottleneck, and `copy_feed_lag_seconds` is set from this.

    python -m scripts.measure_copy_lag --db pmbot.db
    python -m scripts.measure_copy_lag --probe --minutes 5
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics as st
import sys
import time
import uuid
from datetime import datetime, timezone


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _q(xs: list[float], p: float) -> float:
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def from_db(path: str) -> None:
    """Fill timestamp minus leader trade timestamp, over every copied fill."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    seen = {r["uid"]: r["ts"] for r in con.execute("select uid, ts from seen_trades")}
    rows = [dict(r) for r in con.execute(
        "select * from fills where source_leader is not null order by ts")]
    if not rows:
        print(f"{path}: no copied fills")
        return
    print(f"{path}: {len(rows)} copied fills, "
          f"{rows[0]['ts'][:19]} .. {rows[-1]['ts'][:19]}\n")
    print(f"{'side':5s} {'n':>4s} {'p10':>8s} {'p50':>8s} {'p90':>8s} {'p99':>8s}"
          f"   premium over the leader's own price, by lag")
    for side in ("BUY", "SELL"):
        obs = []
        for f in rows:
            if f["side"] != side or (f["reason"] or "").startswith("settle"):
                continue
            lts = seen.get(f["source_uid"])
            if not lts:
                continue
            lag = (_dt(f["ts"]) - _dt(lts)).total_seconds()
            tp = f["target_price"]
            # + is worse for us on both sides: paying up on a buy, selling low.
            prem = ((f["fill_price"] - tp) / tp * (1 if side == "BUY" else -1) * 10_000
                    if tp else None)
            obs.append((lag, prem, f["size_usd"]))
        if not obs:
            continue
        obs.sort()
        lags = [o[0] for o in obs]
        print(f"{side:5s} {len(obs):4d} {_q(lags, .1):7.0f}s {_q(lags, .5):7.0f}s "
              f"{_q(lags, .9):7.0f}s {_q(lags, .99):7.0f}s")
        for lo, hi, nm in ((0, 20, "<20s"), (20, 60, "20-60s"), (60, 300, "1-5m"),
                           (300, 3600, "5-60m"), (3600, 1e9, ">1h")):
            v = [p for lag, p, _ in obs if lo <= lag < hi and p is not None]
            usd = sum(u for lag, _, u in obs if lo <= lag < hi)
            if not v:
                continue
            print(f"       {nm:8s} n={len(v):4d}  mean {st.mean(v):+8.1f}bps  "
                  f"median {st.median(v):+8.1f}bps  ${usd:9,.0f} filled")
        print()
    print("60bps of every premium above is the paper executor's own slippage model;")
    print("the rest is the market moving away from us while we waited for the feed.")


def probe(minutes: float, interval: float) -> None:
    """How old is a trade the first time the feed will show it to us?"""
    import httpx
    c = httpx.Client(timeout=20.0, headers={"User-Agent": "pmbot/0.1 (+research)"})
    url = "https://data-api.polymarket.com/trades"

    r = c.get(url, params={"limit": 1})
    keys = ("age", "cache-control", "cf-cache-status", "expires")
    print("feed cache headers:", {k: v for k, v in r.headers.items() if k.lower() in keys})
    print("a 300s max-age means polling faster than that mostly re-reads one cached page.\n")

    seen: set[tuple] = set()
    first_ages: list[float] = []
    gaps: list[float] = []
    last_new = None
    end = time.time() + minutes * 60
    while time.time() < end:
        now = datetime.now(timezone.utc)
        try:
            # Cache-buster: without it we measure Cloudflare, not Polymarket.
            rows = c.get(url, params={"limit": 200, "_cb": uuid.uuid4().hex}).json()
        except Exception as e:
            print(f"  {type(e).__name__}")
            time.sleep(interval)
            continue
        new = []
        for x in rows:
            uid = (x.get("transactionHash", ""), x.get("timestamp"), x.get("proxyWallet", ""))
            if uid in seen:
                continue
            seen.add(uid)
            age = (now - datetime.fromtimestamp(x["timestamp"], tz=timezone.utc)).total_seconds()
            first_ages.append(age)
            new.append(age)
        if new:
            if last_new:
                gaps.append((now - last_new).total_seconds())
            last_new = now
            print(f"  {now:%H:%M:%S}  +{len(new):3d} new rows   freshest {min(new):6.1f}s old")
        time.sleep(interval)

    if not first_ages:
        print("\nno new trades observed — run for longer")
        return
    xs = sorted(first_ages)
    print(f"\nage when a trade FIRST became visible, n={len(xs)}: "
          f"min {xs[0]:.0f}s  p10 {_q(xs, .1):.0f}s  p50 {_q(xs, .5):.0f}s")
    if gaps:
        print(f"gap between batches of new data: median {st.median(gaps):.0f}s  "
              f"max {max(gaps):.0f}s")
    print("\nThe floor is the origin's own publishing lag; the batches are its "
          "refresh period.\nNeither is something a faster poll loop can reach.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help="pmbot.db to measure realised lag from")
    ap.add_argument("--probe", action="store_true", help="probe the live feed")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--interval", type=float, default=8.0)
    args = ap.parse_args()
    if not args.db and not args.probe:
        ap.error("pass --db PATH, --probe, or both")
    if args.db:
        from_db(args.db)
    if args.probe:
        if args.db:
            print(f"\n{'=' * 70}\n")
        probe(args.minutes, args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
