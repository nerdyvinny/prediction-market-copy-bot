"""Command-line entrypoint.

Usage:
    pmbot paper                 # copy + arb loop in simulation
    pmbot paper --cycles 1      # run a single cycle and exit
    pmbot backtest              # replay historical leader trades (Phase 4)
    pmbot arb-scan [--suggest]  # live arb scan / propose cross-venue pairs
    pmbot arb-backtest [--auto] # replay historical cross-venue gaps
    pmbot status                # portfolio summary (both venues)
    pmbot settle                # realize P&L on resolved markets
    pmbot live                  # GATED — refuses unless explicitly, lawfully enabled
"""

from __future__ import annotations

import argparse
import logging
import sys

from pmbot import __version__
from pmbot.config import get_settings

_COMPLIANCE = (
    "Live trading is OFF by default. Only enable it where you are lawfully able "
    "to trade and fund the venue. This tool contains no geoblock/KYC bypass."
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pmbot", description="Prediction-market copy-trading bot.")
    p.add_argument("--version", action="version", version=f"pmbot {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    sub = p.add_subparsers(dest="mode", required=True)

    paper = sub.add_parser("paper", help="run the loop in simulation (no real orders)")
    paper.add_argument("--cycles", type=int, default=None, help="run N cycles then exit (default: forever)")

    bt = sub.add_parser("backtest", help="replay historical leader trades (exact-copy semantics)")
    bt.add_argument("--leaders", type=str, default=None,
                    help="comma-separated wallets to backtest (default: auto-select)")
    bt.add_argument("--lookback", type=int, default=60, help="days of history (default 60)")
    bt.add_argument("--limit", type=int, default=1000, help="max trades per leader (default 1000)")
    bt.add_argument("--min-notional", type=float, default=None,
                    help="only copy leader BUYs at/above this USD size (default: settings)")
    bt.add_argument("--longterm", action="store_true",
                    help="use the old Strategy #4 semantics (BUY-only, hold to resolution)")

    arb = sub.add_parser("arb-scan",
                         help="scan confirmed PM<->Kalshi pairs for live arbitrage")
    arb.add_argument("--suggest", action="store_true",
                     help="fuzzy-match open markets across venues and print candidate pairs")
    arb.add_argument("--pm-limit", type=int, default=200,
                     help="[suggest] how many top-volume Polymarket markets to consider")
    arb.add_argument("--kalshi-limit", type=int, default=600,
                     help="[suggest] how many open Kalshi markets to consider")
    arb.add_argument("--min-sim", type=float, default=None,
                     help="[suggest] override similarity floor (default from settings)")

    abt = sub.add_parser("arb-backtest",
                         help="replay historical PM<->Kalshi gaps on resolved pairs")
    abt.add_argument("--lookback", type=int, default=30, help="days of history (default 30)")
    abt.add_argument("--auto", action="store_true",
                     help="auto-match RESOLVED markets for backtesting (unvetted, analysis only)")
    abt.add_argument("--auto-limit", type=int, default=150,
                     help="[auto] max resolved markets per venue to consider")
    abt.add_argument("--min-sim", type=float, default=0.75,
                     help="[auto] similarity floor for auto-matched pairs (default 0.75)")

    ldr = sub.add_parser("leaders",
                         help="RESEARCH: rank candidate wallets for the roster (never auto-followed)")
    ldr.add_argument("--vet", action="store_true",
                     help="also replay each candidate as WE would have copied it (slow)")
    ldr.add_argument("--lookback", type=int, default=45,
                     help="[--vet] days of history to replay (default 45)")

    sub.add_parser("status", help="show paper portfolio summary + open positions")
    st = sub.add_parser("settle", help="realize P&L for any open positions whose markets resolved")
    st.add_argument("--market", type=str, default=None,
                    help="force-settle every open position in this market (condition id) "
                         "at --price; for markets that never resolve decisively")
    st.add_argument("--price", type=float, default=None,
                    help="[--market] payout per share, 0..1 (e.g. 1.0 winner / 0.0 loser)")
    sub.add_parser("live", help="GATED live trading (disabled by default)")
    return p


def _setup_logging(verbose: bool) -> None:
    # Windows consoles default to a legacy codepage that can't print the
    # symbols in market titles/our output; force UTF-8 (never crash on print).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # HTTP client logs are very chatty at INFO; keep them quiet unless verbose.
    if not verbose:
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _run_leader_research(args, settings) -> int:
    """Rank candidate wallets for a human to choose from.

    This is the old automatic funnel, demoted to a research tool. Nothing it
    prints is followed: the bot copies `roster` in leaders.yaml and only that.
    Selection stopped being automatic because it was measured not to predict --
    see the module docstring in pmbot/engine.py.
    """
    from datetime import datetime, timezone

    from pmbot.data import GammaClient, PolymarketDataClient, ResolutionStore
    from pmbot.leaders import load_leader_config
    from pmbot.leaders.records import RecordStore
    from pmbot.leaders.scoring import LeaderSelector

    cfg = load_leader_config()
    on_roster = {w.lower() for w in cfg.roster}

    data, gamma = PolymarketDataClient(), GammaClient()
    resolutions = ResolutionStore(settings.db_path)
    records = RecordStore(settings.db_path)
    try:
        selector = LeaderSelector(data, gamma, resolution_store=resolutions,
                                  record_store=records)
        print("- scoring candidate wallets (a few minutes)...")
        print()
        ranked = selector.select(incumbents=sorted(on_roster))

        rep = getattr(selector, "last_report", None) or {}
        if rep:
            print(f"  funnel: {rep.get('pool', 0)} in pool -> "
                  f"{rep.get('deep_scored', 0)} deep-scored -> "
                  f"{rep.get('eligible', 0)} eligible")
            rejects = rep.get("rejects") or {}
            if rejects:
                detail = ", ".join(f"{k}={v}" for k, v in
                                   sorted(rejects.items(), key=lambda kv: -kv[1]))
                print(f"  rejected by: {detail}")
            print()

        if not ranked:
            print("No candidates cleared the filters. Loosen them in leaders.yaml.")
            return 0

        print(f"{len(ranked)} candidate(s), best first. [R] = already on your roster.")
        print()
        for r in ranked:
            st = r.stats
            mark = "[R]" if r.wallet.lower() in on_roster else "   "
            print(f"  {mark} {r.wallet}")
            print(f"       score={r.score:.3f}  pnl=${st.realized_pnl:,.0f}  "
                  f"win={st.win_rate * 100:.0f}%  trades={st.n_trades}  "
                  f"resolved_mkts={st.n_resolved_markets}  "
                  f"copyable={st.n_copyable_trades}  "
                  f"last_trade={st.recency_days * 24.0:.1f}h")

        if args.vet:
            from pmbot.backtest import ExactCopyBacktester

            print()
            print(f"- replaying each candidate as we would have copied it "
                  f"({args.lookback}d)...")
            print()
            vetter = ExactCopyBacktester(data, gamma, settings, trades_limit=4000)
            now = datetime.now(timezone.utc)
            for r in ranked:
                try:
                    tapes = vetter.fetch_tapes([r.wallet])
                    m = vetter.simulate(
                        tapes, lookback_days=args.lookback, now=now,
                        min_leader_notional=settings.copy_min_leader_notional_usd,
                        skip_round_tripped_entries=settings.skip_round_tripped_entries,
                    ).metrics()
                except Exception as e:
                    print(f"  {r.wallet[:12]}  vet failed: {e}")
                    continue
                invested = m.get("invested", 0.0) or 0.0
                roi = (m["net_pnl"] / invested * 100) if invested else 0.0
                print(f"  {r.wallet[:12]}  copy-pnl=${m['net_pnl']:>9.2f}  "
                      f"trades={m['n_trades']:<4} deployed=${invested:>9.2f}  "
                      f"roi={roi:>7.2f}%")

        print()
        print("Nothing here is followed automatically. To copy a wallet, add it to "
              "`roster:` in pmbot/config/leaders.yaml and restart the bot.")
        print("A single month of a wallet's history is far too little to judge it on "
              "-- treat these as leads, not verdicts.")
        return 0
    finally:
        for c in (data, gamma):
            try:
                c.close()
            except Exception:
                pass
        for store in (resolutions, records):
            try:
                store.close()
            except Exception:
                pass


def _run_arb_scan(args, settings) -> int:
    from pmbot.arb.matcher import load_pairs_config, suggest_pairs
    from pmbot.arb.scanner import ArbScanner
    from pmbot.data import GammaClient, KalshiClient, PriceCache

    gamma, prices, kalshi = GammaClient(), PriceCache(), KalshiClient()
    try:
        cfg = load_pairs_config(settings.arb_pairs_path)

        if args.suggest:
            min_sim = args.min_sim if args.min_sim is not None else settings.arb_match_min_similarity
            series = [s for s in settings.kalshi_series.split(",") if s.strip()]
            print(f"· fetching top {args.pm_limit} Polymarket markets + Kalshi series "
                  f"({len(series)} series + generic feed)…")
            pm_markets = gamma.get_markets(limit=args.pm_limit)
            k_markets = kalshi.get_markets_for_series(series, status="open",
                                                      limit_per_series=200)
            k_markets += kalshi.get_markets(status="open", limit=args.kalshi_limit, max_pages=6)
            # De-dup (series fetch + generic feed can overlap).
            k_markets = list({k.ticker: k for k in k_markets}.values())
            confirmed = {(p.pm_market_id, p.kalshi_ticker) for p in cfg.pairs}
            cands = suggest_pairs(
                pm_markets, k_markets,
                min_similarity=min_sim,
                max_close_diff_hours=settings.arb_match_max_close_diff_hours,
                exclude=confirmed | cfg.rejected,
            )
            if not cands:
                print("No candidates found. Try --min-sim 0.5 or a larger --kalshi-limit.")
                return 0
            print(f"\n{len(cands)} candidate pair(s) — REVIEW RESOLUTION RULES BEFORE CONFIRMING.")
            print("Paste vetted entries into pmbot/config/arb_pairs.yaml under `pairs:`\n")
            for c in cands:
                dh = f"{c.close_diff_hours:.1f}h" if c.close_diff_hours is not None else "?"
                print(f"- sim={c.similarity:.2f} close_diff={dh}")
                print(f"   PM: {c.pm_market.question}")
                print(f"   K : {c.kalshi_market.title}  [{c.kalshi_market.ticker}]")
                print(c.as_yaml_stub())
            return 0

        if not cfg.pairs:
            print("No confirmed pairs in arb_pairs.yaml — run `pmbot arb-scan --suggest` "
                  "to find candidates, vet their resolution rules, then add them.")
            return 0
        scanner = ArbScanner(
            gamma, prices, kalshi,
            min_edge=settings.arb_min_edge,
            slippage_buffer=settings.arb_slippage_buffer,
            max_per_trade_usd=settings.arb_max_per_trade_usd,
        )
        print(f"· scanning {len(cfg.pairs)} confirmed pair(s)…")
        opps = scanner.scan(cfg.pairs)
        if not opps:
            print("No opportunities clear the edge threshold right now "
                  f"(min_edge={settings.arb_min_edge:.3f}, buffer={settings.arb_slippage_buffer:.3f}).")
            return 0
        print(f"\n{len(opps)} opportunity(ies):")
        for o in opps:
            print(f"  {o.describe()}")
            print(f"     PM: {o.pm_market.question[:70]}")
            print(f"     K : {o.kalshi_market.title[:70]}")
        return 0
    finally:
        for c in (gamma, prices, kalshi):
            c.close()


def _run_arb_backtest(args, settings) -> int:
    from pmbot.arb.backtest import ArbBacktester
    from pmbot.arb.matcher import ConfirmedPair, load_pairs_config, suggest_pairs
    from pmbot.data import GammaClient, KalshiClient, PriceCache

    gamma, prices, kalshi = GammaClient(), PriceCache(), KalshiClient()
    try:
        pairs = load_pairs_config(settings.arb_pairs_path).pairs
        if args.auto:
            print(f"· auto-matching resolved markets (sim >= {args.min_sim:.2f}) — "
                  "UNVETTED pairs, analysis only…")
            pm_markets = gamma.get_markets(
                limit=args.auto_limit, closed=True, active=None, order="volume24hr"
            )
            series = [s for s in settings.kalshi_series.split(",") if s.strip()]
            k_markets = kalshi.get_markets_for_series(
                series, status="settled", limit_per_series=args.auto_limit
            )
            print(f"  considering {len(pm_markets)} PM / {len(k_markets)} Kalshi "
                  f"resolved markets across {len(series)} series")
            cands = suggest_pairs(
                pm_markets, k_markets,
                min_similarity=args.min_sim,
                max_close_diff_hours=settings.arb_match_max_close_diff_hours,
                include_closed=True,
                top_k_per_market=1,
            )
            auto = [
                ConfirmedPair(
                    pm_market_id=c.pm_market.market_id,
                    kalshi_ticker=c.kalshi_market.ticker,
                    note=f"AUTO sim={c.similarity:.2f}",
                )
                for c in cands
            ]
            print(f"  matched {len(auto)} resolved pair(s)")
            pairs = pairs + auto
        if not pairs:
            print("No pairs to backtest. Add confirmed pairs to arb_pairs.yaml "
                  "or run with --auto.")
            return 0
        bt = ArbBacktester(
            gamma, prices, kalshi,
            min_edge=settings.arb_min_edge,
            slippage_buffer=settings.arb_slippage_buffer,
            max_per_trade_usd=settings.arb_max_per_trade_usd,
            lookback_days=args.lookback,
        )
        print(f"· replaying {len(pairs)} pair(s) over {args.lookback}d…\n")
        print(bt.run(pairs).summary_text())
        return 0
    finally:
        for c in (gamma, prices, kalshi):
            c.close()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    settings = get_settings()

    if args.mode == "paper":
        from pmbot.engine import Engine

        engine = Engine(settings=settings)
        try:
            engine.run(max_cycles=args.cycles)
        finally:
            engine.close()
        return 0

    if args.mode == "leaders":
        return _run_leader_research(args, settings)

    if args.mode == "backtest":
        from pmbot.backtest import Backtester, ExactCopyBacktester
        from pmbot.data import GammaClient, PolymarketDataClient
        from pmbot.leaders.scoring import LeaderSelector

        data, gamma = PolymarketDataClient(), GammaClient()
        try:
            if args.leaders:
                leaders = [w.strip().lower() for w in args.leaders.split(",") if w.strip()]
            else:
                print("· auto-selecting leaders to backtest…")
                leaders = [r.wallet for r in LeaderSelector(data, gamma).select()]
            if not leaders:
                print("No leaders to backtest (loosen leaders.yaml or pass --leaders).")
                return 0
            print(f"· backtesting {len(leaders)} leader(s) over {args.lookback}d…\n")
            if args.longterm:
                report = Backtester(data, gamma, settings,
                                    lookback_days=args.lookback, trades_limit=args.limit).run(leaders)
            else:
                min_notional = (settings.copy_min_leader_notional_usd
                                if args.min_notional is None else args.min_notional)
                report = ExactCopyBacktester(data, gamma, settings, trades_limit=args.limit).run(
                    leaders, lookback_days=args.lookback, min_leader_notional=min_notional,
                )
            print(report.summary_text())
        finally:
            data.close()
            gamma.close()
        return 0

    if args.mode == "arb-scan":
        return _run_arb_scan(args, settings)

    if args.mode == "arb-backtest":
        return _run_arb_backtest(args, settings)

    if args.mode == "status":
        from pmbot.portfolio.ledger import Ledger

        led = Ledger(settings.db_path)
        try:
            s = led.summary()
            print(f"=== pmbot status ({settings.db_path}) ===")
            print(f"  fills          : {s['fills']}")
            print(f"  open positions : {s['open_positions']}")
            print(f"  deployed       : ${s['deployed_usd']:,.2f} / ${settings.bankroll_usd:,.0f} bankroll")
            print(f"  realized P&L   : ${s['realized_pnl']:,.2f}")
            print(f"  fees paid      : ${s['fees_usd']:,.2f}")
            print(f"  net P&L        : ${s['net_pnl']:,.2f}")
            print(f"  leaders followed: {s['leaders']}")
            positions = led.get_positions()
            if positions:
                print("  positions:")
                for p in sorted(positions, key=lambda x: -abs(x.shares * x.avg_price)):
                    venue = "K" if p.venue == "kalshi" else "PM"
                    print(f"    [{venue:>2s}] {p.outcome:>4s} {p.shares:>11.2f} sh @ {p.avg_price:.4f} "
                          f"(${p.shares*p.avg_price:,.2f})  mkt={p.market_id[:16]}…")
        finally:
            led.close()
        return 0

    if args.mode == "settle":
        from pmbot.data import GammaClient, KalshiClient
        from pmbot.portfolio.ledger import Ledger
        from pmbot.portfolio.settlement import Settler

        led, gamma, kalshi = Ledger(settings.db_path), GammaClient(), KalshiClient()
        try:
            settler = Settler(led, gamma, kalshi)
            if args.market is not None:
                if args.price is None or not (0.0 <= args.price <= 1.0):
                    print("--market requires --price between 0 and 1 (payout per share).")
                    return 2
                n = settler.force_settle_market(args.market, args.price)
                print(f"Force-settled {n} position(s) in {args.market} at {args.price:.2f}.")
            else:
                n = settler.settle_open_positions()
                print(f"Settled {n} position(s).")
            s = led.summary()
            print(f"realized=${s['realized_pnl']:,.2f} "
                  f"fees=${s['fees_usd']:,.2f} net=${s['net_pnl']:,.2f}")
        finally:
            led.close()
            gamma.close()
            kalshi.close()
        return 0

    if args.mode == "live":
        print("Refusing to run live: live execution is gated until Phase 6.")
        print(_COMPLIANCE)
        return 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
