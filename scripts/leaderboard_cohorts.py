"""Would copying the top of Polymarket's public leaderboard have paid?

Question this answers: our own leader funnel measured to zero
(corr(leader's trailing ROI, next trade) = -0.018), so the roster is static and
hand-picked. Before designing a new way to *score* leaders, check the cheapest
external ranking that already exists -- `lb-api.polymarket.com` ranks every
wallet on Polymarket by realized profit over `all`, `30d` and `7d`. If the top
of those boards is copyable and profitable under our settings, scoring is a
solved problem; if not, no in-house scheme is likely to beat it either.

Method: take the top `--top` wallets of each board, fetch their tapes, and
replay the last `--days` through `ExactCopyBacktester` with the LIVE VPS
parameters (read 2026-08-30, pinned below -- the laptop `.env` drifts). Two
passes, exactly as `backtest_current.py`: pass 1 learns which tokens get
entered, pass 2 replays with a settlement instant recovered from each token's
own price tape, so the $15/market cap and the $500 bankroll bind the way they
do live.

There is one arm, and it is the live one. The liquidity floor, the drift budget
and the poll interval come from the settings object the bot itself reads, and
every fill is priced off the token's own book at the instant we would have
quoted it -- no quote, no trade, exactly as PaperExecutor behaves.

Four things this CANNOT tell you, all of them load-bearing:

  1. The `30d` and `7d` boards are ranked BY PROFIT IN THE WINDOW WE SCORE.
     Those cohorts are in-sample by construction: we are asking "did the
     wallets that made the most money last month make money last month".
     Read them as an upper bound, never as an expectation. The `all` board is
     the only near-out-of-sample one, and even it is conditioned on the wallet
     not having blown up before today.
  2. `--oos` is the honest arm: rank the SAME universe on how *we* would have
     copied them over [-2W, -W], take the best, and score them over [-W, now].
     Scoring a lineup only on the window after it was chosen is the only
     method this repo trusts (see docs/roster.md).
  3. The tune window of `--oos` is quoted only where its tokens overlap the
     scoring window's book, so its ranking is the optimistic one. The forward
     score -- the number that decides anything -- is fully quoted.
  4. Leaderboard whales are far more HFT than our roster. Treat any cohort
     whose tape spans hours rather than weeks as unscoreable, not as good.

Usage:
    python -m scripts.leaderboard_cohorts                  # top 10 of each board
    python -m scripts.leaderboard_cohorts --top 25 --oos
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from collections import defaultdict
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

import httpx

from pmbot.backtest import ExactCopyBacktester, fetch_book
from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.data.price_cache import PriceCache
from pmbot.models import Side
from scripts import sweep_stop
from scripts.sweep_stop import derive_resolutions, fetch_series

# --- Live VPS settings, read 2026-08-30 ------------------------------------
BANKROLL = 500.0
COPY_FRACTION = 0.10
MAX_PER_MARKET_PCT = 0.03          # $15 on $500 -- replaces the _USD cap
MAX_PER_LEADER = 400.0
COMPOUND = False
BAND = (0.15, 0.85)
MIN_LEADER_NOTIONAL = 150.0
SLIPPAGE_BPS = 60.0
MIN_HOURS_TO_RESOLUTION = 0.0
SKIP_ROUND_TRIPPED = True
# The liquidity floor, drift budget and poll interval are NOT pinned here --
# `simulate` reads them from the settings object, which is the same one the bot
# reads, so they cannot drift apart from the deployed values.
#
# How far after the poll instant a quote may sit and still stand in for the book
# we would have hit. Loose enough to cover 1-minute bars with a gap, tight
# enough that a sample ten minutes downstream is not called a fill.
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
BOARDS = ("all", "30d", "7d")

_TMP = os.environ.get("TEMP", "/tmp")
TAPE_CACHE = os.path.join(_TMP, "pmb_lb_tapes.pkl")
MKT_CACHE = os.path.join(_TMP, "pmb_lb_markets.pkl")
# sweep_stop binds its cache path at import, and its default is a POSIX /tmp
# that does not exist on Windows -- point both its reader and its writer here.
PRICE_CACHE = os.environ.get("PMBOT_LB_PRICE_CACHE",
                             os.path.join(_TMP, "pmb_lb_prices.json"))
sweep_stop.CACHE = PRICE_CACHE
BOOK_CACHE = os.path.join(_TMP, "pmb_lb_book.json")


def _log(m: str) -> None:
    print(m, flush=True)


# --- data ------------------------------------------------------------------


def fetch_boards(top: int) -> dict[str, list[tuple[str, str, float]]]:
    """(wallet, display name, profit) rows per leaderboard window."""
    out: dict[str, list[tuple[str, str, float]]] = {}
    with httpx.Client(timeout=30) as c:
        for w in BOARDS:
            r = c.get(f"{LB_API}/profit", params={"window": w, "limit": 50})
            r.raise_for_status()
            rows = r.json()
            out[w] = [
                (str(x["proxyWallet"]).lower(), x.get("name") or "",
                 float(x.get("amount") or 0))
                for x in rows
            ][:top]
    return out


def load_tapes(bt: ExactCopyBacktester, wallets: list[str], *, refresh: bool) -> dict:
    cached: dict = {}
    if not refresh and os.path.exists(TAPE_CACHE):
        with open(TAPE_CACHE, "rb") as fh:
            cached = pickle.load(fh)
    want = [w for w in wallets if w not in cached]
    if want:
        _log(f"tapes: fetching {len(want)} of {len(wallets)} (rest cached)...")
        t0 = time.time()
        cached.update(bt.fetch_tapes(want))
        _log(f"tapes: fetched in {time.time() - t0:.0f}s")
        with open(TAPE_CACHE, "wb") as fh:
            pickle.dump(cached, fh)
    else:
        _log(f"tapes: all {len(wallets)} cached")
    return {w: cached.get(w, []) for w in wallets}


def load_book(moments) -> dict:
    """The library's `fetch_book`, wrapped in this script's disk cache."""
    try:
        with open(BOOK_CACHE) as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    before = len(cache)
    book = fetch_book(moments, PriceCache, cache=cache,
                      progress=lambda d, n: _log(f"  {d}/{n}..."))
    _log(f"book: {len(book)} tokens ({len(cache) - before} newly fetched), "
         f"{sum(1 for v in book.values() if v)} with usable quotes")
    with open(BOOK_CACHE, "w") as fh:
        json.dump(cache, fh)
    return book


def seed_market_cache(bt: ExactCopyBacktester) -> None:
    if os.path.exists(MKT_CACHE):
        with open(MKT_CACHE, "rb") as fh:
            bt._mkt_cache.update(pickle.load(fh))
        _log(f"markets: {len(bt._mkt_cache)} resolutions restored from disk")


def save_market_cache(bt: ExactCopyBacktester) -> None:
    with open(MKT_CACHE, "wb") as fh:
        pickle.dump(bt._mkt_cache, fh)


# --- reporting -------------------------------------------------------------


def peak_concurrent(results) -> float:
    """Largest capital simultaneously at risk -- what the $500 must cover.

    `invested` is cumulative turnover and says nothing about whether the
    bankroll was ever big enough; a cohort can look fine on ROI while needing
    many times the money it has.
    """
    events: list[tuple[datetime, float]] = []
    for r in results:
        close = max(r.resolve_ts, r.entry_ts)
        events.append((r.entry_ts, r.size_usd))
        events.append((close, -r.size_usd))
    events.sort(key=lambda e: (e[0], e[1]))
    cur = peak = 0.0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    return peak


def tape_diag(wallet: str, tape: list, now: datetime, days: int) -> dict:
    if not tape:
        return {"n": 0, "span": 0.0, "stale_h": 999.0, "buys": 0,
                "copyable": 0, "capped": False}
    lo = min(t.timestamp for t in tape)
    hi = max(t.timestamp for t in tape)
    cutoff = now - timedelta(days=days)
    win = [t for t in tape if t.timestamp >= cutoff]
    buys = [t for t in win if t.side is Side.BUY]
    copyable = [
        t for t in buys
        if t.usd_size >= MIN_LEADER_NOTIONAL and BAND[0] <= t.price <= BAND[1]
    ]
    return {
        "n": len(tape),
        "span": (hi - lo).total_seconds() / 86400,
        "stale_h": (now - hi).total_seconds() / 3600,
        "buys": len(buys),
        "copyable": len(copyable),
        "capped": len(tape) >= 3000,
    }


def run_cohort(bt, tapes, wallets, *, days, now, settings, resolves=None, book=None,
               quiet=False):
    """One cohort's replay. The liquidity floor, the drift budget and the poll
    interval all come from `settings` -- the same object the live bot reads --
    so no arrangement of arguments here reproduces the old optimistic run."""
    return bt.simulate(
        {w: tapes.get(w, []) for w in wallets},
        lookback_days=days,
        now=now,
        min_leader_notional=MIN_LEADER_NOTIONAL,
        price_min=BAND[0],
        price_max=BAND[1],
        settings=settings,
        slippage_bps=SLIPPAGE_BPS,
        min_hours_to_resolution=MIN_HOURS_TO_RESOLUTION,
        skip_round_tripped_entries=SKIP_ROUND_TRIPPED,
        resolve_at=resolves,
        book=book,
        book_max_staleness_seconds=BOOK_STALENESS,
        warn_no_book=book is None and not quiet,
    )


def split_results(rep, now):
    """Realized-in-window vs still-open, with the inverted-stamp correction.

    Anything settling after `now` has not happened yet; counting it is
    look-ahead. Anything stamped before its own entry is a start-of-day sports
    market -- right payout, wrong date -- booked on the entry day.
    """
    rs = [dc_replace(r, resolve_ts=max(r.resolve_ts, r.entry_ts)) for r in rep.results]
    return [r for r in rs if r.resolve_ts <= now], [r for r in rs if r.resolve_ts > now]


def summarize(name, closed, still_open):
    if not closed:
        _log(f"  {name:<26} no copyable resolved trades")
        return None
    inv = sum(r.size_usd for r in closed)
    net = sum(r.pnl for r in closed)
    wins = [r for r in closed if r.pnl > 0]
    losses = [r for r in closed if r.pnl < 0]
    avg_w = sum(r.pnl for r in wins) / len(wins) if wins else 0.0
    avg_l = sum(r.pnl for r in losses) / len(losses) if losses else 0.0
    return {
        "name": name, "n": len(closed), "invested": inv, "net": net,
        "roi": net / inv if inv else 0.0, "win_rate": len(wins) / len(closed),
        "peak": peak_concurrent(closed),
        "payoff": (avg_w / abs(avg_l)) if avg_l else float("inf"),
        "open_n": len(still_open),
        "open_cost": sum(r.size_usd for r in still_open),
        "leaders": len({r.leader for r in closed}),
        "results": closed,
    }


def print_row(s):
    if s is None:
        return
    _log(f"  {s['name']:<26} {s['n']:>5} {s['leaders']:>4} {s['invested']:>11,.0f} "
         f"{s['net']:>+10,.2f} {s['roi']*100:>+8.2f}% {s['win_rate']*100:>7.1f}% "
         f"{s['payoff']:>7.2f} {s['peak']:>10,.0f} {s['open_n']:>6}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10, help="wallets per board")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--per-wallet", action="store_true",
                    help="also score every wallet on its own, with the whole "
                         "bankroll to itself")
    ap.add_argument("--oos", action="store_true",
                    help="also run the walk-forward arm (rank on the prior window)")
    ap.add_argument("--trades-limit", type=int, default=3000)
    ap.add_argument("--json", default="", help="write the summary rows here")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.days)

    s = get_settings().model_copy(update={
        "bankroll_usd": BANKROLL,
        "copy_fraction": COPY_FRACTION,
        "max_per_market_pct": MAX_PER_MARKET_PCT,
        "max_per_leader_usd": MAX_PER_LEADER,
        "compound_profits": COMPOUND,
        "copy_price_min": BAND[0],
        "copy_price_max": BAND[1],
        "copy_min_leader_notional_usd": MIN_LEADER_NOTIONAL,
        "slippage_bps": SLIPPAGE_BPS,
    })

    _log("=" * 118)
    _log("POLYMARKET LEADERBOARD COHORTS -- would copying the top wallets have paid?")
    _log(f"window {start:%Y-%m-%d %H:%M} .. {now:%Y-%m-%d %H:%M} UTC   ({args.days}d)")
    _log(f"live settings: bankroll ${BANKROLL:,.0f} | copy {COPY_FRACTION:.0%} | "
         f"cap {MAX_PER_MARKET_PCT:.0%} = ${BANKROLL*MAX_PER_MARKET_PCT:,.0f}/market, "
         f"${MAX_PER_LEADER:,.0f}/leader | band {BAND[0]}-{BAND[1]} | "
         f"notional floor ${MIN_LEADER_NOTIONAL:,.0f} | slip {SLIPPAGE_BPS:.0f}bps")
    _log("=" * 118)

    boards = fetch_boards(args.top)
    cohorts: dict[str, list[str]] = {f"lb-{w}": [a for a, _, _ in rows]
                                     for w, rows in boards.items()}
    cohorts["roster (live)"] = [w.lower() for w in ROSTER]
    universe = sorted({w for ws in cohorts.values() for w in ws})

    _log("\ncohorts: " + ", ".join(f"{k}={len(v)}" for k, v in cohorts.items())
         + f"   union={len(universe)} wallets")
    for w, rows in boards.items():
        overlap = {a for a, _, _ in rows} & {r.lower() for r in ROSTER}
        _log(f"  profit/{w:<4} top{args.top}: "
             f"${rows[-1][2]:,.0f}..${rows[0][2]:,.0f} profit"
             f"   roster overlap: {len(overlap)}")

    data = PolymarketDataClient()
    gamma = GammaClient()
    bt = ExactCopyBacktester(data, gamma, s, slippage_bps=SLIPPAGE_BPS,
                             trades_limit=args.trades_limit)
    seed_market_cache(bt)
    tapes = load_tapes(bt, universe, refresh=args.refresh)

    # --- tape diagnostics --------------------------------------------------
    _log("\n" + "-" * 118)
    _log("TAPE COVERAGE -- a wallet we cannot see the window of, or cannot copy, "
         "scores nothing regardless of its rank")
    _log("-" * 118)
    _log(f"  {'wallet':<14} {'boards':<14} {'name':<20} {'trades':>7} {'span_d':>7} "
         f"{'stale_h':>8} {'buys':>6} {'copyable':>9}  note")
    names = {a: n for rows in boards.values() for a, n, _ in rows}
    member = defaultdict(list)
    for cname, ws in cohorts.items():
        for w in ws:
            member[w].append(cname.replace("lb-", "").replace(" (live)", ""))
    diags = {}
    for w in universe:
        d = tape_diag(w, tapes.get(w, []), now, args.days)
        diags[w] = d
        note = []
        if d["capped"] and d["span"] < 7:
            note.append(f"HFT: {d['n']} trades in {d['span']:.1f}d")
        if d["stale_h"] > 96:
            note.append(f"stale {d['stale_h']/24:.0f}d")
        if d["copyable"] == 0:
            note.append("nothing copyable")
        _log(f"  {w[:12]:<14} {','.join(member[w]):<14} {names.get(w, '')[:20]:<20} "
             f"{d['n']:>7} {d['span']:>7.1f} {d['stale_h']:>8.0f} {d['buys']:>6} "
             f"{d['copyable']:>9}  {'; '.join(note)}")

    # --- pass 1: which tokens get entered ----------------------------------
    _log("\npass 1/2: discovering entered tokens across the union...")
    base = run_cohort(bt, tapes, universe, days=args.days, now=now, settings=s,
                      quiet=True)   # discovery only; its fills are discarded
    save_market_cache(bt)
    if not base.results:
        _log("No copyable resolved trades in the window for any cohort.")
        return 1
    _log(f"  {len(base.results)} tranches over "
         f"{len({r.token_id for r in base.results})} tokens")

    prices = PriceCache()
    try:
        with open(PRICE_CACHE) as fh:
            pcache = json.load(fh)
    except (OSError, ValueError):
        pcache = {}
    series = fetch_series(base.results, prices, pcache)
    resolves = derive_resolutions(series, base.results)
    _log(f"  recovered a real settlement time for {len(resolves)}/"
         f"{len({r.token_id for r in base.results})} tokens")
    sells = [(t.token_id, t.timestamp)
             for tp in tapes.values() for t in tp
             if t.side is Side.SELL and t.timestamp >= start]
    book = load_book(base.candidate_entries + sells)

    # --- pass 2: per cohort, quoting the book at every fill ----------------
    _log("\npass 2/2: replaying each cohort on the live path...")
    rows, skipstats = [], {}
    for cname, ws in cohorts.items():
        rep = run_cohort(bt, tapes, ws, days=args.days, now=now, settings=s,
                         resolves=resolves, book=book)
        closed, still_open = split_results(rep, now)
        rows.append(summarize(cname, closed, still_open))
        skipstats[cname] = rep.skipped
    save_market_cache(bt)

    _log("\n" + "=" * 118)
    _log(f"RESULT -- {args.days} days, live settings, P&L realized inside the window")
    _log("=" * 118)
    _log(f"  {'cohort':<26} {'trades':>5} {'ldrs':>4} {'deployed$':>11} "
         f"{'net P&L$':>10} {'ROI/depl':>9} {'win%':>8} {'payoff':>7} "
         f"{'peak conc$':>10} {'open':>6}")
    for r in rows:
        print_row(r)

    _log("\n  what the live path refused, per cohort:")
    keys = ("no_book", "price_drift", "entry_limit", "exit_no_book", "liquidity",
            "liquidity_unknown", "bankroll_or_caps", "round_tripped", "price_band",
            "leader_notional", "unresolved_market")
    _log("    " + f"{'cohort':<20}" + "".join(f"{k[:13]:>15}" for k in keys))
    for cname in cohorts:
        sk = skipstats.get(cname, {})
        _log("    " + f"{cname[:20]:<20}" + "".join(f"{sk.get(k, 0):>15,}" for k in keys))
    _log("\n  no_book / exit_no_book = no quote within "
         f"{BOOK_STALENESS:.0f}s of the poll instant, so the trade did not happen "
         "-- exactly what")
    _log("  PaperExecutor does when the CLOB has no book. entry_limit = the fill "
         "landed above the leader's")
    _log("  price + the drift budget, refused as a limit order. liquidity_unknown "
         "is NOT a rejection: Gamma")
    _log("  drops the liquidity field when a market closes, so the $5,000 floor is "
         "unenforceable on most of a")
    _log("  resolved-market backtest.")
    _log(f"\n  peak conc$ = most capital at risk at one instant. Anything above "
         f"${BANKROLL:,.0f} is money the bot does not have --")
    _log("  the sim frees exposure as tranches settle, so a cohort can post a "
         "fine ROI on a bankroll it could never fund.")

    # --- per-leader detail --------------------------------------------------
    for r in rows:
        if not r or not r["results"]:
            continue
        _log(f"\n  {r['name']} -- by leader:")
        by: dict[str, list] = defaultdict(list)
        for x in r["results"]:
            by[x.leader].append(x)
        for ldr, xs in sorted(by.items(), key=lambda kv: -sum(x.pnl for x in kv[1])):
            pnl = sum(x.pnl for x in xs)
            inv = sum(x.size_usd for x in xs)
            wr = sum(1 for x in xs if x.pnl > 0) / len(xs) * 100
            _log(f"    {ldr[:12]:<14} {names.get(ldr, '')[:18]:<20} {len(xs):>4} trades  "
                 f"${inv:>8,.0f} deployed  ${pnl:>+9,.2f}  "
                 f"{pnl/inv*100 if inv else 0:>+7.2f}%  win {wr:>5.1f}%")

    # --- every wallet on its own -------------------------------------------
    # A cohort run makes wallets compete for one $500 bankroll, and whoever
    # trades first takes it -- which is why the 7-day board swung from +18.8%
    # to -27.9% purely on how deep the cut went. Running each wallet ALONE with
    # the whole bankroll removes that confound: what is left is the wallet.
    if args.per_wallet:
        _log("\n" + "=" * 118)
        _log(f"EVERY WALLET ON ITS OWN -- {args.days}d, live path, each with the "
             f"full ${BANKROLL:,.0f} to itself")
        _log("=" * 118)
        _log(f"  {'wallet':<14} {'boards':<12} {'name':<18} {'trades':>6} "
             f"{'deployed$':>10} {'net P&L$':>10} {'ROI/depl':>9} {'win%':>7} "
             f"{'peak$':>7} {'span_d':>7} {'copyable':>9}")
        solo = []
        for w in universe:
            rep = run_cohort(bt, tapes, [w], days=args.days, now=now, settings=s,
                             resolves=resolves, book=book)
            closed, so = split_results(rep, now)
            d = diags.get(w, {})
            if not closed:
                _log(f"  {w[:12]:<14} {','.join(member[w])[:12]:<12} "
                     f"{names.get(w, '')[:18]:<18} {'--':>6}   "
                     f"(nothing copyable survived the live path)")
                solo.append((0.0, 0, w, None))
                continue
            inv = sum(r.size_usd for r in closed)
            net = sum(r.pnl for r in closed)
            wr = sum(1 for r in closed if r.pnl > 0) / len(closed) * 100
            roi = net / inv if inv else 0.0
            _log(f"  {w[:12]:<14} {','.join(member[w])[:12]:<12} "
                 f"{names.get(w, '')[:18]:<18} {len(closed):>6} {inv:>10,.0f} "
                 f"{net:>+10,.2f} {roi*100:>+8.2f}% {wr:>6.1f}% "
                 f"{peak_concurrent(closed):>7,.0f} {d.get('span', 0):>7.1f} "
                 f"{d.get('copyable', 0):>9}")
            solo.append((roi, len(closed), w, net))
        scored = [x for x in solo if x[3] is not None]
        _log(f"\n  {len(scored)} of {len(universe)} wallets produced a copyable trade.")
        if scored:
            wins = [x for x in scored if x[3] > 0]
            _log(f"  profitable: {len(wins)}/{len(scored)} "
                 f"({len(wins)/len(scored)*100:.0f}%)")
            # Weight by trades: a +80% wallet on 2 trades is not evidence.
            thick = [x for x in scored if x[1] >= 10]
            if thick:
                tw = sum(1 for x in thick if x[3] > 0)
                _log(f"  with >=10 closed trades: {tw}/{len(thick)} profitable "
                     f"({tw/len(thick)*100:.0f}%)")
            _log("\n  best and worst by ROI (>=10 trades only -- the rest is noise):")
            for roi, n, w, net in sorted(thick, reverse=True)[:5]:
                _log(f"    {w[:12]:<14} {names.get(w, '')[:18]:<18} {n:>4} trades  "
                     f"${net:>+9,.2f}  {roi*100:>+7.2f}%")
            _log("    ...")
            for roi, n, w, net in sorted(thick)[:5]:
                _log(f"    {w[:12]:<14} {names.get(w, '')[:18]:<18} {n:>4} trades  "
                     f"${net:>+9,.2f}  {roi*100:>+7.2f}%")

    # --- honest arm: rank on the prior window, score on this one -----------
    if args.oos:
        w_days = args.days
        mid = now - timedelta(days=w_days)
        _log("\n" + "=" * 118)
        _log("WALK-FORWARD ARM -- rank the same universe on how WE would have copied "
             f"them over\n  [{(mid - timedelta(days=w_days)):%Y-%m-%d} .. {mid:%Y-%m-%d}], "
             f"then score that lineup ONLY on [{mid:%Y-%m-%d} .. {now:%Y-%m-%d}].")
        _log("=" * 118)
        tune = run_cohort(bt, tapes, universe, days=w_days, now=mid, settings=s,
                          quiet=True)   # ranking only, and flagged as optimistic above
        save_market_cache(bt)
        tclosed, _ = split_results(tune, mid)
        per: dict[str, list] = defaultdict(list)
        for x in tclosed:
            per[x.leader].append(x)
        ranked = []
        for ldr, xs in per.items():
            inv = sum(x.size_usd for x in xs)
            if len(xs) < 5 or inv <= 0:
                continue                       # too thin to rank on
            ranked.append((sum(x.pnl for x in xs) / inv, len(xs), ldr))
        ranked.sort(reverse=True)
        _log(f"  {len(per)} wallets traded in the tune window; {len(ranked)} had >=5 "
             f"closed copies to rank on")
        for roi, n, ldr in ranked[:12]:
            _log(f"    {ldr[:12]:<14} {names.get(ldr, '')[:18]:<20} {n:>4} copies  "
                 f"tune ROI {roi*100:>+7.2f}%")
        _log("")
        for k in (5, 8):
            pick = [ldr for _, _, ldr in ranked[:k]]
            if not pick:
                continue
            rep = run_cohort(bt, tapes, pick, days=w_days, now=now, settings=s,
                             resolves=resolves, book=book)
            closed, so = split_results(rep, now)
            print_row(summarize(f"walk-fwd top{k}", closed, so))
        save_market_cache(bt)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump([{k: v for k, v in r.items() if k != "results"}
                       for r in rows if r], fh, indent=2)
        _log(f"\nsummary written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
