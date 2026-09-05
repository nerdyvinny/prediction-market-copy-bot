"""30-day backtest of the CURRENTLY DEPLOYED strategy, trade by trade.

Every parameter below is pinned to the live VPS `.env` (read 2026-08-06), not
to `get_settings()` — the laptop `.env` drifts from production and silently
backtests a trade population the bot no longer takes. Pinning them here means
this script keeps reporting the deployed strategy even when the local file is
stale.

Usage:
    python -m scripts.backtest_current [--days 30] [--refresh]
"""

from __future__ import annotations

import argparse
import calendar
import csv
import os
import pickle
import sys
from collections import defaultdict
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.data.price_cache import PriceCache
from pmbot.leaders import load_leader_config
from scripts.sweep_stop import derive_resolutions, fetch_series, load_cache

# --- Live VPS settings, 2026-08-06 -----------------------------------------
# The roster is read from leaders.yaml, not pinned here. Since the static
# roster shipped (2026-08-30) that file IS the deployed lineup, so a hardcoded
# copy could only ever be a stale one — and was: this script still ran the
# 8 wallets of 2026-08-06 three roster changes later.
LEADERS = [w.lower() for w in load_leader_config().roster]
BANKROLL = 500.0
COPY_FRACTION = 0.10
MAX_PER_MARKET_PCT = 0.03    # $15 on $500; live since 2026-08-13, replaces the $ cap
MAX_PER_MARKET = 50.0        # IGNORED while the pct is set, as in the live .env
MAX_PER_LEADER = 400.0
MIN_LIQUIDITY = 5000.0
COMPOUND = False
BAND = (0.15, 0.85)
MIN_LEADER_NOTIONAL = 150.0
SLIPPAGE_BPS = 60.0
MIN_HOURS_TO_RESOLUTION = 0.0
SKIP_ROUND_TRIPPED = True

CACHE = os.path.join(
    os.environ.get("TEMP", "/tmp"), "pmb_tapes_current.pkl"
)


def load_tapes(bt, leaders, *, refresh: bool):
    if not refresh and os.path.exists(CACHE):
        with open(CACHE, "rb") as fh:
            tapes = pickle.load(fh)
        if set(tapes) == {w.lower() for w in leaders}:
            print(f"tapes: loaded from cache ({sum(len(v) for v in tapes.values())} trades)")
            return tapes
        print("tapes: cached leader set differs from live — refetching")
    print(f"tapes: fetching {len(leaders)} leader tapes from the Data API…")
    tapes = bt.fetch_tapes(leaders)
    with open(CACHE, "wb") as fh:
        pickle.dump(tapes, fh)
    return tapes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--csv", default="", help="also write the blotter here")
    ap.add_argument("--feed-lag", type=float, default=None,
                    help="seconds the /trades feed runs behind the leader "
                         "(default: the measured copy_feed_lag_seconds). Pass 0 "
                         "to see the old, optimistic poll-interval-only model.")
    args = ap.parse_args()

    s = get_settings().model_copy(update={
        "bankroll_usd": BANKROLL,
        "copy_fraction": COPY_FRACTION,
        "max_per_market_usd": MAX_PER_MARKET,
        "max_per_market_pct": MAX_PER_MARKET_PCT,
        "max_per_leader_usd": MAX_PER_LEADER,
        "min_market_liquidity_usd": MIN_LIQUIDITY,
        "compound_profits": COMPOUND,
        "copy_price_min": BAND[0],
        "copy_price_max": BAND[1],
        "copy_min_leader_notional_usd": MIN_LEADER_NOTIONAL,
        "slippage_bps": SLIPPAGE_BPS,
        **({} if args.feed_lag is None else {"copy_feed_lag_seconds": args.feed_lag}),
    })
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.days)

    data = PolymarketDataClient()
    gamma = GammaClient()
    bt = ExactCopyBacktester(data, gamma, s, slippage_bps=SLIPPAGE_BPS, trades_limit=3000)

    tapes = load_tapes(bt, LEADERS, refresh=args.refresh)
    in_window = sum(1 for tp in tapes.values() for t in tp if start <= t.timestamp <= now)
    spans = [
        (min(t.timestamp for t in tp), max(t.timestamp for t in tp))
        for tp in tapes.values() if tp
    ]
    print(f"tape span: {min(a for a, _ in spans):%Y-%m-%d} .. {max(b for _, b in spans):%Y-%m-%d}"
          f"   ({in_window} raw trades inside the {args.days}d window)")

    book_cache: dict = {}

    def run(resolve_at=None):
        # simulate_live quotes the book at every entry and exit, the way the
        # paper executor does. Plain simulate fills at the leader's own price,
        # which scored this exact roster ~15pp above what the bot delivered.
        return bt.simulate_live(
            tapes,
            book_cache=book_cache,
            lookback_days=args.days,
            now=now,
            min_leader_notional=MIN_LEADER_NOTIONAL,
            price_min=BAND[0],
            price_max=BAND[1],
            settings=s,
            slippage_bps=SLIPPAGE_BPS,
            min_hours_to_resolution=MIN_HOURS_TO_RESOLUTION,
            skip_round_tripped_entries=SKIP_ROUND_TRIPPED,
            resolve_at=resolve_at,
        )

    # Pass 1 discovers which tokens get entered. Its holding periods are NOT
    # usable: daily-sports markets carry Gamma's end_date at the START of the
    # day, so an in-game entry "resolves" before it was opened and the tranche
    # settles in the same instant it is created. That frees the $50/market cap
    # and the whole bankroll immediately, so the caps never bind and the run
    # deploys many times the $500 it actually has. Pass 2 replays with a real
    # settlement instant recovered from each token's own price tape.
    print("\npass 1/2: discovering entered tokens…", flush=True)
    base = run()
    if not base.results:
        print("No copyable resolved trades in the window.")
        return 1
    prices = PriceCache()
    series = fetch_series(base.results, prices, load_cache())
    resolves = derive_resolutions(series, base.results)
    print(f"recovered a real settlement time for {len(resolves)}/"
          f"{len({r.token_id for r in base.results})} tokens", flush=True)
    print("pass 2/2: replaying with realistic holding periods…", flush=True)
    rep = run(resolve_at=resolves)

    print(f"\n{'='*100}")
    print(f"PMB BOT — 30-DAY BACKTEST OF CURRENT STRATEGY")
    print(f"window: {start:%Y-%m-%d %H:%M} .. {now:%Y-%m-%d %H:%M} UTC")
    print(f"{'='*100}")
    print(f"bankroll ${BANKROLL:,.0f} | copy {COPY_FRACTION:.0%} of leader | "
          f"cap {MAX_PER_MARKET_PCT:.0%} = ${BANKROLL * MAX_PER_MARKET_PCT:,.0f}/market, "
          f"${MAX_PER_LEADER:,.0f}/leader")
    print(f"band {BAND[0]}-{BAND[1]} | leader-notional floor ${MIN_LEADER_NOTIONAL:,.0f} | "
          f"slippage {SLIPPAGE_BPS:.0f}bps | compound {COMPOUND} | "
          f"round-trip filter {SKIP_ROUND_TRIPPED}")
    print(f"copy lag {s.copy_feed_lag_seconds + s.poll_interval_seconds:.0f}s "
          f"(feed {s.copy_feed_lag_seconds:.0f}s + poll {s.poll_interval_seconds:.0f}s) — "
          f"every fill below is quoted that long after the leader's print")
    print(f"skipped by filter: {rep.skipped}")

    rs_all = sorted(rep.results, key=lambda r: r.entry_ts)
    if not rs_all:
        print("\nNo copyable resolved trades in the window.")
        return 1

    # `resolve_ts` needs two corrections before it can date a P&L calendar.
    #
    # 1. Where the price tape never pinned, Gamma's start-of-day end_date
    #    survives pass 2 and still precedes the entry. The payout is right, the
    #    stamp is not — book those on the entry day (they are same-day sports).
    # 2. Anything settling after `now` has NOT happened yet. Its outcome is
    #    known to us only because the market resolved after the window closed,
    #    so counting it as 30-day P&L is look-ahead. Report it as open.
    def closed_on(r):
        return max(r.resolve_ts, r.entry_ts)

    n_inverted = sum(1 for r in rs_all if r.resolve_ts < r.entry_ts)
    # Stamp the correction onto the results themselves so that metrics() — which
    # orders the drawdown walk by resolve_ts — sees the same sequence the blotter
    # and the calendar do. Left raw, a trade realizing "before" its own entry
    # reorders the equity curve and moved max drawdown by ~18%.
    rs_all = [dc_replace(r, resolve_ts=closed_on(r)) for r in rs_all]
    rs = [r for r in rs_all if r.resolve_ts <= now]
    still_open = [r for r in rs_all if r.resolve_ts > now]

    # ---- blotter ----------------------------------------------------------
    print(f"\n{'-'*100}")
    print(f"EVERY TRADE THAT CLOSED IN THE WINDOW  ({len(rs)} trades)")
    print(f"{'-'*100}")
    hdr = (f"{'#':>3} {'entry (UTC)':<17} {'closed (UTC)':<17} {'leader':<10} "
           f"{'outcome':<22} {'entry$':>7} {'size$':>7} {'P&L$':>8} {'ret%':>7} "
           f"{'cum P&L':>9} {'how':<12}")
    print(hdr)
    run = 0.0
    for i, r in enumerate(sorted(rs, key=closed_on), 1):
        run += r.pnl
        ret = (r.pnl / r.size_usd * 100) if r.size_usd else 0.0
        print(f"{i:>3} {r.entry_ts:%m-%d %H:%M:%S}     {closed_on(r):%m-%d %H:%M:%S}     "
              f"{r.leader[:8]:<10} {r.outcome[:22]:<22} {r.entry_price:>7.4f} "
              f"{r.size_usd:>7.2f} {r.pnl:>8.2f} {ret:>6.1f}% {run:>9.2f} {r.closed_by:<12}")

    if still_open:
        print(f"\n{'-'*100}")
        print(f"STILL OPEN AT THE END OF THE WINDOW — excluded from the P&L above "
              f"({len(still_open)} trades)")
        print("these settle after 2026-08-06; we only 'know' the outcome because the")
        print("market resolved later, so counting them as 30-day P&L would be look-ahead")
        print(f"{'-'*100}")
        print(f"{'#':>3} {'entry (UTC)':<17} {'settles':<17} {'leader':<10} "
              f"{'outcome':<22} {'entry$':>7} {'size$':>7} {'would-be P&L':>13}")
        for i, r in enumerate(sorted(still_open, key=lambda r: r.entry_ts), 1):
            print(f"{i:>3} {r.entry_ts:%m-%d %H:%M:%S}     {r.resolve_ts:%Y-%m-%d}          "
                  f"{r.leader[:8]:<10} {r.outcome[:22]:<22} {r.entry_price:>7.4f} "
                  f"{r.size_usd:>7.2f} {r.pnl:>13.2f}")
        print(f"    open capital tied up: ${sum(r.size_usd for r in still_open):,.2f}   "
              f"unrealized (if they land as they eventually did): "
              f"${sum(r.pnl for r in still_open):+,.2f}")

    rep.results = rs
    m = rep.metrics()
    print(f"\n{'-'*100}")
    print("SUMMARY")
    print(f"{'-'*100}")
    wins = [r for r in rs if r.pnl > 0]
    losses = [r for r in rs if r.pnl < 0]
    print(f"  trades closed   : {m['n_trades']}   ({len(wins)}W / {len(losses)}L / "
          f"{m['n_trades']-len(wins)-len(losses)} flat)")
    print(f"  capital deployed: ${m['invested']:,.2f}  (cumulative turnover, not "
          f"concurrent — peak concurrent is capped at the ${BANKROLL:,.0f} bankroll)")
    print(f"  net P&L         : ${m['net_pnl']:,.2f}")
    print(f"  return on bankroll: {m['net_pnl']/BANKROLL*100:,.2f}%  (on ${BANKROLL:,.0f})")
    print(f"  ROI on deployed : {m['roi']*100:,.2f}%")
    print(f"  win rate        : {m['win_rate']*100:,.1f}%")
    print(f"  avg P&L/trade   : ${m['net_pnl']/m['n_trades']:,.2f}")
    if wins:
        print(f"  avg winner      : ${sum(r.pnl for r in wins)/len(wins):,.2f}   "
              f"best ${max(r.pnl for r in rs):,.2f}")
    if losses:
        print(f"  avg loser       : ${sum(r.pnl for r in losses)/len(losses):,.2f}   "
              f"worst ${min(r.pnl for r in rs):,.2f}")
    print(f"  max drawdown    : ${m['max_drawdown']:,.2f}")
    if n_inverted:
        print(f"\n  data note: {n_inverted} trade(s) kept a Gamma end_date that precedes "
              f"their own entry\n  (start-of-day stamp, price tape never pinned) — P&L is "
              f"right, dated on the entry day.")

    print("\n  by how the trade ended:")
    by_close: dict[str, list[float]] = defaultdict(list)
    for r in rs:
        by_close[r.closed_by].append(r.pnl)
    for k, v in sorted(by_close.items(), key=lambda x: -sum(x[1])):
        print(f"    {k:<12} {len(v):>3} trades   net ${sum(v):>9,.2f}   "
              f"win {sum(1 for x in v if x > 0)/len(v)*100:>5.1f}%")

    print("\n  by leader:")
    for leader, (cnt, pnl) in sorted(m["by_leader"].items(), key=lambda x: -x[1][1]):
        lw = [r for r in rs if r.leader == leader]
        wr = sum(1 for r in lw if r.pnl > 0) / len(lw) * 100
        print(f"    {leader[:10]}…  {cnt:>3} trades   net ${pnl:>9,.2f}   win {wr:>5.1f}%")

    # ---- P&L calendar -----------------------------------------------------
    # Booked on the day the position CLOSED — that is when the cash moves.
    daily: dict[datetime.date, float] = defaultdict(float)
    dcount: dict[datetime.date, int] = defaultdict(int)
    for r in rs:
        daily[closed_on(r).date()] += r.pnl
        dcount[closed_on(r).date()] += 1

    print(f"\n{'-'*100}")
    print("P&L CALENDAR  (P&L booked on the day the trade closed/settled)")
    print(f"{'-'*100}")

    months: list[tuple[int, int]] = []
    d = start.date().replace(day=1)
    last = now.date()
    while d <= last:
        months.append((d.year, d.month))
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)

    for year, month in months:
        print(f"\n  {calendar.month_name[month]} {year}")
        print("      Mon      Tue      Wed      Thu      Fri      Sat      Sun")
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
            top, bot = "  ", "  "
            for day in week:
                if day.month != month or not (start.date() <= day <= last):
                    top += f"{'':>9}"
                    bot += f"{'':>9}"
                    continue
                pnl = daily.get(day, None)
                top += f"{day.day:>4}     "
                bot += f"{(f'{pnl:+.2f}' if pnl is not None else '·'):>8} "
            print(top.rstrip())
            print(bot.rstrip())

    active = {d: v for d, v in daily.items()}
    if active:
        up = sum(1 for v in active.values() if v > 0)
        dn = sum(1 for v in active.values() if v < 0)
        best = max(active.items(), key=lambda x: x[1])
        worst = min(active.items(), key=lambda x: x[1])
        print(f"\n  active days: {len(active)}   ({up} up / {dn} down)")
        print(f"  best  day  : {best[0]}  ${best[1]:+,.2f}  ({dcount[best[0]]} trades)")
        print(f"  worst day  : {worst[0]}  ${worst[1]:+,.2f}  ({dcount[worst[0]]} trades)")

        print("\n  equity curve (cumulative realized P&L, by close date):")
        cum, curve = 0.0, []
        for day in sorted(active):
            cum += active[day]
            curve.append((day, active[day], cum))
        scale = max(abs(c) for _, _, c in curve) or 1.0
        for day, pnl, c in curve:
            bar = ("#" if c >= 0 else "-") * max(1, round(abs(c) / scale * 55))
            print(f"    {day}  {pnl:>+9.2f}  cum {c:>+9.2f}  {bar}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["n", "state", "entry_ts", "close_ts", "leader", "market_id", "outcome",
                        "entry_price", "size_usd", "shares", "pnl", "return_pct", "closed_by"])
            for i, r in enumerate(sorted(rs, key=closed_on) + sorted(
                    still_open, key=lambda r: r.entry_ts), 1):
                w.writerow([i, "closed" if closed_on(r) <= now else "open",
                            r.entry_ts.isoformat(), closed_on(r).isoformat(), r.leader,
                            r.market_id, r.outcome, f"{r.entry_price:.6f}", f"{r.size_usd:.2f}",
                            f"{r.shares:.4f}", f"{r.pnl:.2f}",
                            f"{(r.pnl/r.size_usd*100) if r.size_usd else 0:.2f}", r.closed_by])
        print(f"\nblotter written to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
