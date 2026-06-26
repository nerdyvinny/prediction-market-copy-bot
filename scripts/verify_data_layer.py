"""Phase 1 acceptance check: exercise the data layer against LIVE public APIs.

Run:  .venv/Scripts/python.exe scripts/verify_data_layer.py
Reads only public data — no auth, no orders.
"""

from __future__ import annotations

from pmbot.data import GammaClient, PolymarketDataClient, PriceCache


def main() -> int:
    print("== Gamma: top active markets ==")
    with GammaClient() as gamma:
        markets = gamma.get_markets(limit=5)
        for m in markets:
            days = (
                f"{(m.end_date).date()}" if m.end_date else "?"
            )
            liq = f"${m.liquidity_usd:,.0f}" if m.liquidity_usd else "?"
            print(f"  - {m.question[:55]:55s} liq={liq:>14s} ends={days} tokens={len(m.tokens)}")
        sample = next((m for m in markets if m.tokens), None)

    if not sample:
        print("No market with tokens found; aborting.")
        return 1

    print(f"\n== Selected market ==\n  {sample.question}")
    print(f"  condition_id: {sample.market_id}")
    print(f"  category: {sample.category}  end_date: {sample.end_date}")
    outcome, token_id = next(iter(sample.tokens.items()))
    print(f"  outcome '{outcome}' -> token {token_id[:18]}...")

    print("\n== CLOB: top-of-book quote ==")
    with PriceCache() as pc:
        q = pc.get_quote(token_id)
        print(f"  bid={q.bid}  ask={q.ask}  mid={q.mid}")

    print("\n== Data API: recent trades in this market (harvest a wallet) ==")
    with PolymarketDataClient() as data:
        trades = data.get_trades(market=sample.market_id, limit=3)
        for t in trades:
            print(
                f"  {t.timestamp:%Y-%m-%d %H:%M} {t.side.value:4s} "
                f"{t.outcome:>4s} {t.shares:>10.2f}sh @ {t.price:.3f} "
                f"= ${t.usd_size:,.2f}  by {t.leader[:10]}..."
            )
        if not trades:
            print("  (no recent trades)")
            return 0

        leader = trades[0].leader
        print(f"\n== Data API: that leader's history + portfolio value ==")
        hist = data.get_trades(user=leader, limit=5)
        print(f"  {leader}")
        print(f"  recent trades: {len(hist)} | portfolio value: ${data.get_value(leader):,.2f}")
        for t in hist[:5]:
            print(f"    {t.timestamp:%Y-%m-%d} {t.side.value:4s} {t.outcome:>4s} "
                  f"${t.usd_size:,.2f}  [{t.event_slug}]")

    print("\nOK: data layer works end-to-end against live public endpoints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
