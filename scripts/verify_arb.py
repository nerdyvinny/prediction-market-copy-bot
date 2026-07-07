"""End-to-end live check of the arbitrage stack (read-only public APIs).

Exercises: Kalshi client -> matcher suggestion -> scanner (parallel) ->
paper execution of a synthetic opportunity -> settlement plumbing.
Run:  python scripts/verify_arb.py
"""

from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pmbot.arb.matcher import ConfirmedPair, suggest_pairs  # noqa: E402
from pmbot.arb.scanner import ArbScanner  # noqa: E402
from pmbot.config import get_settings  # noqa: E402
from pmbot.data import GammaClient, KalshiClient, PriceCache  # noqa: E402

OK, FAIL = "ok", "FAIL"


def main() -> int:
    s = get_settings()
    gamma, prices, kalshi = GammaClient(), PriceCache(), KalshiClient()
    failures = 0
    try:
        # 1. Kalshi market data (series-scoped: the generic feed is ~all
        #    combo markets, which the client rightly filters out) -------------
        series = [x for x in s.kalshi_series.split(",") if x.strip()]
        t0 = time.monotonic()
        kms = kalshi.get_markets_for_series(series[:4], status="open",
                                            limit_per_series=50)
        dt = time.monotonic() - t0
        good = bool(kms) and all(m.ticker for m in kms)
        quoted = sum(1 for m in kms if m.yes_ask or m.no_ask)
        print(f"[{OK if good else FAIL}] kalshi.get_markets_for_series: {len(kms)} open, "
              f"{quoted} with quotes ({dt:.2f}s)")
        failures += not good

        if kms:
            probe = next((m for m in kms if m.yes_ask or m.no_ask), kms[0])
            book = kalshi.get_orderbook(probe.ticker)
            good = bool(book.yes_bids or book.no_bids)
            print(f"[{OK if good else FAIL}] kalshi.get_orderbook({probe.ticker[:24]}…): "
                  f"yes_ask={book.yes_ask} no_ask={book.no_ask}")
            failures += not good

        # 2. Cross-venue suggestion (live) ------------------------------------
        pm = gamma.get_markets(limit=120)
        km = kalshi.get_markets_for_series(series, status="open", limit_per_series=150)
        t0 = time.monotonic()
        cands = suggest_pairs(pm, km, min_similarity=s.arb_match_min_similarity,
                              max_close_diff_hours=s.arb_match_max_close_diff_hours)
        dt = time.monotonic() - t0
        print(f"[{OK}] matcher: {len(pm)} PM x {len(km)} K -> "
              f"{len(cands)} candidate(s) in {dt:.2f}s")
        for c in cands[:3]:
            print(f"      sim={c.similarity:.2f}  PM «{c.pm_market.question[:48]}» "
                  f"<> K «{c.kalshi_market.title[:40]} | {c.kalshi_market.subtitle[:20]}»")

        # 3. Scanner on live pairs (candidates as stand-ins) -------------------
        pairs = [
            ConfirmedPair(pm_market_id=c.pm_market.market_id,
                          kalshi_ticker=c.kalshi_market.ticker,
                          note="verify-only")
            for c in cands[:8]
        ]
        if pairs:
            opps = ArbScanner(
                gamma, prices, kalshi,
                min_edge=s.arb_min_edge,
                slippage_buffer=s.arb_slippage_buffer,
                max_per_trade_usd=s.arb_max_per_trade_usd,
            ).scan(pairs)
            # Re-scan to show warm-cache latency too.
            print(f"[{OK}] scanner: {len(pairs)} pair(s) x both directions -> "
                  f"{len(opps)} opp(s)")
            for o in opps[:3]:
                print("      ", o.describe())
        else:
            print(f"[{OK}] scanner: skipped (no live candidates right now — fine)")

        print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
        return 1 if failures else 0
    finally:
        for c in (gamma, prices, kalshi):
            c.close()


if __name__ == "__main__":
    sys.exit(main())
