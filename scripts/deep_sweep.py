"""Deep sweep: build the widest reachable wallet pool, deep-score it, and
walk-forward vet whatever survives.

The engine's nightly rescore is deliberately cheap: it profiles ~30 open + ~30
resolved market feeds and deep-scores at most 1000 wallets. That is enough to
keep a followed set fresh, but it only ever sees the loudest markets of the
moment. This script is the expensive counterpart you run by hand when you want
to re-derive the leader set from as much of Polymarket as the APIs expose.

Four candidate sources, unioned:

  1. LEADERBOARD (new here): lb-api.polymarket.com ranks wallets globally by
     realized profit and by volume. It caps at 50 rows per query and ignores
     `offset`, so it is a seed, not an enumeration — but those 50 are ranked
     across ALL of Polymarket, which no feed sweep can reproduce.
  2. FEEDS, widened: many more open + recently-resolved markets than the
     nightly run, at full trade depth.
  3. RECORDS: every wallet the local resolved-market ledger has ever seen win,
     which survives the feed churn that makes a single night's sample noisy.
  4. INCUMBENTS: whoever we currently follow, so the report always compares
     the candidates against the sitting set.

Deep scoring reuses `LeaderSelector._deep_score` verbatim, so a wallet that
passes here passes for exactly the reasons the live bot would accept it.

Stage 5 is the part that decides anything: a walk-forward exact-copy backtest
that tunes on an older window and validates out-of-sample on the recent one.
Filters describe a wallet; only the backtest says whether copying it made
money. Nothing here writes to the live config — the output is a report.

Usage:
    python -m scripts.deep_sweep                 # full sweep, default budget
    python -m scripts.deep_sweep --max-score 800 # cheaper/faster
    python -m scripts.deep_sweep --no-vet        # skip the backtest stage
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import Settings, get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.data.resolution_cache import ResolutionStore
from pmbot.leaders.config import load_leader_config
from pmbot.leaders.discovery import _shrunk_win_frac, profile_candidates
from pmbot.leaders.scoring import LeaderSelector, failing_filters, rank_wallets

LB_API = "https://lb-api.polymarket.com"
LB_WINDOWS = ("1d", "7d", "30d", "all")
LB_METRICS = ("profit", "volume")

# Walk-forward windows: tune on [now-2W, now-W], validate on [now-W, now].
VET_WINDOW_DAYS = 45
VET_MIN_TRADES = 3          # tune-window evidence floor
VET_MAX_WALLETS = 60        # cap on full 2000-trade tape fetches


def _log(msg: str) -> None:
    print(msg, flush=True)


# --- stage 1: candidate sources -----------------------------------------


def pool_from_leaderboard(client: httpx.Client) -> dict[str, dict]:
    """Globally-ranked wallets from the public leaderboard endpoint."""
    found: dict[str, dict] = {}
    for metric in LB_METRICS:
        for window in LB_WINDOWS:
            try:
                r = client.get(
                    f"{LB_API}/{metric}", params={"window": window, "limit": 50}, timeout=30
                )
                r.raise_for_status()
                rows = r.json()
            except Exception as e:
                _log(f"  leaderboard {metric}/{window} failed: {type(e).__name__}")
                continue
            for row in rows if isinstance(rows, list) else []:
                w = str(row.get("proxyWallet") or "").lower()
                if not w:
                    continue
                rec = found.setdefault(w, {"name": row.get("name") or "", "ranks": {}})
                rec["ranks"][f"{metric}_{window}"] = row.get("amount")
    _log(f"  leaderboard: {len(found)} unique wallets across "
         f"{len(LB_METRICS)}x{len(LB_WINDOWS)} rankings")
    return found


def pool_from_records(db_path: str, since: datetime) -> dict[str, float]:
    """Net-positive wallets from the local resolved-market ledger, mapped to a
    shrunk win fraction so they compete for deep-score budget on evidence
    rather than sitting at a flat prior behind every feed-seen wallet."""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT wallet, "
            "       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), "
            "       SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) "
            "FROM wallet_records WHERE resolved_at >= ? "
            "GROUP BY wallet HAVING SUM(pnl) > 0",
            (since.isoformat(),),
        ).fetchall()
        conn.close()
    except Exception as e:
        _log(f"  records: read failed ({e}); skipping")
        return {}
    out = {str(w).lower(): _shrunk_win_frac(int(wins), int(losses))
           for w, wins, losses in rows}
    _log(f"  records: {len(out)} net-positive wallets in window")
    return out


# --- main ----------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--open-markets", type=int, default=150)
    ap.add_argument("--closed-markets", type=int, default=150)
    ap.add_argument("--per-market-trades", type=int, default=1000)
    ap.add_argument("--max-score", type=int, default=2500,
                    help="cap on wallets that get a deep score")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-vet", action="store_true", help="skip the walk-forward backtest")
    ap.add_argument("--out", type=str, default="deep_sweep_results.json")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    now = datetime.now(timezone.utc)
    cfg = load_leader_config()
    lookback = cfg.filters.lookback_days
    cutoff = now - timedelta(days=lookback)
    settings = get_settings()
    db_path = settings.db_path
    t0 = time.time()

    data, gamma = PolymarketDataClient(), GammaClient()
    lb_client = httpx.Client(headers={"User-Agent": "pmbot-deep-sweep/1.0"})

    # incumbents: whoever the live bot currently follows
    incumbents: list[str] = []
    try:
        conn = sqlite3.connect(db_path)
        incumbents = [str(r[0]).lower()
                      for r in conn.execute("SELECT wallet FROM followed_leaders")]
        conn.close()
    except Exception:
        pass

    _log(f"=== DEEP SWEEP  ({now:%Y-%m-%d %H:%M} UTC, {lookback}d window) ===")
    _log(f"db={db_path}  incumbents={len(incumbents)}")

    # --- stage 1: pool ---------------------------------------------------
    _log("\n[1/5] building candidate pool")
    lb = pool_from_leaderboard(lb_client)
    records = pool_from_records(db_path, cutoff)
    _log(f"  sweeping {args.open_markets} open + {args.closed_markets} resolved "
         f"market feeds x {args.per_market_trades} trades …")
    profiles = profile_candidates(
        data, gamma,
        top_open_markets=args.open_markets,
        top_closed_markets=args.closed_markets,
        per_market_trades=args.per_market_trades,
        lookback_days=lookback,
    )
    _log(f"  feeds: {len(profiles)} wallets  ({time.time()-t0:.0f}s)")

    block = set(cfg.blocklist)
    pool = (set(lb) | set(records) | set(profiles)
            | set(incumbents) | set(cfg.allowlist)) - block
    _log(f"  POOL: {len(pool)} unique wallets")

    # Rank the pool for the deep-score budget. Leaderboard wallets, incumbents
    # and the allowlist are always scored; the rest compete on feed quality
    # (estimated win rate), never on raw activity — sorting by activity would
    # spend the whole budget on market-making bots.
    must = (set(lb) | set(incumbents) | set(cfg.allowlist)) - block

    def budget_key(w: str) -> float:
        """Best available win-quality evidence: today's feeds, the accumulated
        record ledger, or a neutral prior if a source saw the wallet but
        neither has scored it."""
        return max(profiles[w].quality if w in profiles else 0.0,
                   records.get(w, 0.0),
                   0.0) or 0.35

    rest = sorted(pool - must, key=budget_key, reverse=True)
    targets = sorted(must | set(rest[: max(0, args.max_score - len(must))]))
    _log(f"  deep-scoring {len(targets)} wallets ({len(must)} forced, "
         f"{len(targets)-len(must)} by feed quality)")

    # --- stage 2: deep score ---------------------------------------------
    _log("\n[2/5] deep scoring (warm resolution cache, parallel)")
    store = ResolutionStore(db_path)
    selector = LeaderSelector(data, gamma, cfg, resolution_store=store,
                              max_workers=args.workers)
    selector._memo = dict(store.load())
    selector._new_terminal = {}
    _log(f"  resolution cache warm with {len(selector._memo)} markets")

    done = [0]

    def score(w: str):
        try:
            return selector._deep_score(w, cutoff, now)
        finally:
            done[0] += 1
            if done[0] % 100 == 0:
                _log(f"    …{done[0]}/{len(targets)} scored ({time.time()-t0:.0f}s)")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(score, targets))
    if selector._new_terminal:
        store.save_many(selector._new_terminal)
        _log(f"  cached {len(selector._new_terminal)} new resolutions")

    early = Counter(reason for _, _, reason in results if reason)
    stats = [st for _, st, _ in results if st is not None]
    _log(f"  {len(stats)} wallets fully scored, {sum(early.values())} early-rejected "
         f"({time.time()-t0:.0f}s)")
    _log(f"  early rejects: {dict(early)}")

    # --- stage 3: filter -------------------------------------------------
    _log("\n[3/5] applying leaders.yaml filters")
    eligible, near_miss, fails = [], [], Counter()
    for st in stats:
        bad = failing_filters(st, cfg.filters)
        if not bad:
            eligible.append(st)
        else:
            fails.update(bad)
            if len(bad) == 1:
                near_miss.append((st, bad[0]))
    _log(f"  filter rejects: {dict(fails)}")
    _log(f"  ELIGIBLE: {len(eligible)}   near-miss (fail exactly 1): {len(near_miss)}")

    ranked = rank_wallets(eligible, cfg.weights,
                          copyable_target=cfg.selection.copyable_target,
                          win_rate_floor=cfg.filters.min_win_rate)
    _log(f"\n=== ranked eligible wallets (top 25 of {len(ranked)}) ===")
    _log(f"  {'wallet':<44}{'score':>7}{'pnl':>12}{'wr':>7}{'res':>5}{'cpy':>5}  src")
    for r in ranked[:25]:
        s = r.stats
        src = []
        if s.wallet in lb:
            src.append("LB")
        if s.wallet in incumbents:
            src.append("held")
        if s.wallet in records:
            src.append("rec")
        _log(f"  {s.wallet:<44}{r.score:>7.3f}${s.realized_pnl:>11,.0f}"
             f"{s.win_rate*100:>6.0f}%{s.n_resolved_markets:>5}{s.n_copyable_trades:>5}"
             f"  {','.join(src) or 'feed'}")

    # where do the incumbents stand now?
    by_wallet = {s.wallet: s for s in stats}
    _log("\n=== incumbents rescored ===")
    rank_of = {r.wallet: i + 1 for i, r in enumerate(ranked)}
    for w in incumbents:
        st = by_wallet.get(w)
        if st is None:
            reason = next((r for ww, _, r in results if ww == w and r), "not scored")
            _log(f"  {w}  DROPPED — {reason}")
            continue
        bad = failing_filters(st, cfg.filters)
        tag = f"rank #{rank_of[w]}" if w in rank_of else f"FAILS {','.join(bad)}"
        _log(f"  {w}  {tag}  pnl=${st.realized_pnl:>10,.0f} wr={st.win_rate*100:.0f}% "
             f"res={st.n_resolved_markets} cpy={st.n_copyable_trades}")

    payload = {
        "run_at": now.isoformat(),
        "lookback_days": lookback,
        "pool": len(pool),
        "deep_scored": len(targets),
        "early_rejects": dict(early),
        "filter_rejects": dict(fails),
        "incumbents": incumbents,
        "ranked": [
            {"wallet": r.wallet, "score": r.score, **vars(r.stats),
             "on_leaderboard": r.wallet in lb,
             "leaderboard_name": lb.get(r.wallet, {}).get("name", "")}
            for r in ranked
        ],
        "near_miss": [{"wallet": s.wallet, "failed": f, **vars(s)} for s, f in near_miss],
    }

    # --- stage 4/5: walk-forward vet -------------------------------------
    if not args.no_vet and ranked:
        vet = [r.wallet for r in ranked[:VET_MAX_WALLETS]]
        for w in incumbents:
            if w not in vet:
                vet.append(w)
        _log(f"\n[4/5] walk-forward vetting {len(vet)} wallets "
             f"(tune {VET_WINDOW_DAYS}d ending {VET_WINDOW_DAYS}d ago)")
        vet_settings = Settings(
            bankroll_usd=settings.bankroll_usd,
            copy_fraction=settings.copy_fraction,
            max_per_market_usd=settings.max_per_market_usd,
            max_per_leader_usd=settings.max_per_leader_usd,
            compound_profits=False,
        )
        bt = ExactCopyBacktester(data, gamma, settings=vet_settings, trades_limit=2000)
        tapes = bt.fetch_tapes(vet)
        _log(f"  fetched {len(tapes)} tapes ({time.time()-t0:.0f}s)")

        def sim(sub, when, weights=None):
            return bt.simulate(
                sub, lookback_days=VET_WINDOW_DAYS,
                min_leader_notional=settings.copy_min_leader_notional_usd,
                price_min=settings.copy_price_min, price_max=settings.copy_price_max,
                now=when, leader_weights=weights,
            ).metrics()

        tune_now = now - timedelta(days=VET_WINDOW_DAYS)
        tune, val = {}, {}
        for i, w in enumerate(tapes, 1):
            tune[w] = sim({w: tapes[w]}, tune_now)
            val[w] = sim({w: tapes[w]}, now)
            if i % 10 == 0:
                _log(f"    …{i}/{len(tapes)} vetted ({time.time()-t0:.0f}s)")

        _log(f"\n=== per-wallet walk-forward (tune -> validate) ===")
        _log(f"  {'wallet':<44}{'tune n':>7}{'tune pnl':>11}{'val n':>7}"
             f"{'val pnl':>11}{'val roi':>9}")
        order = sorted(tapes, key=lambda w: -tune[w]["net_pnl"])
        for w in order:
            t, v = tune[w], val[w]
            mark = " *held" if w in incumbents else ""
            _log(f"  {w:<44}{t['n_trades']:>7}${t['net_pnl']:>10,.0f}"
                 f"{v['n_trades']:>7}${v['net_pnl']:>10,.0f}{v['roi']*100:>8.1f}%{mark}")

        # Portfolios: does any candidate set beat what we already hold?
        good = [w for w in order
                if tune[w]["n_trades"] >= VET_MIN_TRADES and tune[w]["net_pnl"] > 0
                and w not in incumbents]
        held = [w for w in incumbents if w in tapes]
        portfolios = {
            "incumbents (current)": held,
            "top-8 by tune pnl": order[:8],
            "incumbents + top-4 new": held + good[:4],
            "best-12 tune-positive": [w for w in order if tune[w]["net_pnl"] > 0][:12],
        }
        _log(f"\n[5/5] === OUT-OF-SAMPLE portfolio comparison "
             f"(last {VET_WINDOW_DAYS}d) ===")
        _log(f"  {'portfolio':<26}{'n':>5}{'net':>11}{'roi':>9}{'maxdd':>10}")
        pf_out = {}
        for name, members in portfolios.items():
            sub = {w: tapes[w] for w in dict.fromkeys(members) if w in tapes}
            if not sub:
                continue
            m = sim(sub, now)
            pf_out[name] = {"members": list(sub), **m}
            _log(f"  {name:<26}{m['n_trades']:>5}${m['net_pnl']:>10,.0f}"
                 f"{m['roi']*100:>8.1f}%${m['max_drawdown']:>9,.0f}")
        payload["walkforward"] = {
            "window_days": VET_WINDOW_DAYS,
            "per_wallet": {w: {"tune": tune[w], "validate": val[w]} for w in tapes},
            "portfolios": pf_out,
        }

    out_path = os.path.abspath(args.out)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    _log(f"\nwrote {out_path}  (total {time.time()-t0:.0f}s)")

    data.close()
    gamma.close()
    lb_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
