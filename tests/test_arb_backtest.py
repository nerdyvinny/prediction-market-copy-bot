"""ArbBacktester replay logic with faked history endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from pmbot.arb.backtest import ArbBacktester
from pmbot.arb.matcher import ConfirmedPair
from pmbot.models import KalshiMarket, Market

CLOSE = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
CLOSE_TS = int(CLOSE.timestamp())
PAIR = ConfirmedPair(pm_market_id="0xmkt", kalshi_ticker="KX-T", pm_outcome="Yes")


def candle(hour_ts: int, yes_ask: float, yes_bid: float) -> dict:
    return {
        "end_period_ts": hour_ts,
        "yes_ask": {"close_dollars": f"{yes_ask:.4f}"},
        "yes_bid": {"close_dollars": f"{yes_bid:.4f}"},
    }


class FakeGamma:
    def __init__(self, winner_token: str):
        self._winner = winner_token

    def get_market_with_resolution(self, condition_id):
        m = Market(
            market_id="0xmkt", question="Will X happen?", closed=True,
            tokens={"Yes": "tok-yes", "No": "tok-no"},
        )
        return m, self._winner


class FakeKalshi:
    def __init__(self, result: str, candles: list[dict]):
        self._result = result
        self._candles = candles

    def get_market(self, ticker):
        return KalshiMarket(
            ticker="KX-T", event_ticker="KX", title="Will X happen?",
            status="settled", result=self._result, close_time=CLOSE,
        )

    def get_candlesticks(self, series, ticker, *, start_ts, end_ts, period_interval=60):
        return [c for c in self._candles if start_ts <= c["end_period_ts"] <= end_ts]


class FakePrices:
    def __init__(self, points: list[tuple[int, float]]):
        self._pts = points

    def get_price_history(self, token_id, *, start_ts, end_ts, fidelity_minutes=60):
        return [(t, p) for t, p in self._pts if start_ts <= t <= end_ts]


def backtester(gamma, prices, kalshi, **kw):
    kw.setdefault("min_edge", 0.01)
    kw.setdefault("slippage_buffer", 0.005)
    kw.setdefault("max_per_trade_usd", 100.0)
    kw.setdefault("pm_half_spread", 0.01)
    kw.setdefault("lookback_days", 7)
    return ArbBacktester(gamma, prices, kalshi, **kw)


def hours_before_close(n: int) -> int:
    return CLOSE_TS - n * 3600


def test_gap_hour_produces_profitable_entry():
    h1, h2 = hours_before_close(30), hours_before_close(29)
    # Hour 1: PM mid 0.40 (ask 0.41) + Kalshi no_ask = 1-0.55 = 0.45 -> gap.
    # Hour 2: both venues at fair value, no gap in either direction.
    kalshi = FakeKalshi("yes", [candle(h1, 0.58, 0.55), candle(h2, 0.52, 0.48)])
    prices = FakePrices([(h1, 0.40), (h2, 0.50)])
    # PM YES wins (resolution consistent: kalshi result yes == equiv side yes).
    bt = backtester(FakeGamma("tok-yes"), prices, kalshi)
    report = bt.run([PAIR])
    r = report.results[0]
    assert r.skipped_reason is None
    assert not r.resolution_mismatch
    assert r.hours_observed == 2
    assert r.hours_with_gap >= 1
    assert len(r.entries) == 1
    e = r.entries[0]
    # Bought PM Yes @0.41 + Kalshi NO @0.45; YES resolved -> PM leg pays.
    assert e.pm_outcome == "Yes" and e.kalshi_side == "no"
    assert e.payout_usd == e.contracts       # exactly one leg paid
    assert e.pnl_usd > 0


def test_resolution_mismatch_flagged_and_pnl_honest():
    h1 = hours_before_close(30)
    kalshi = FakeKalshi("no", [candle(h1, 0.58, 0.55)])   # Kalshi said NO...
    prices = FakePrices([(h1, 0.40)])
    bt = backtester(FakeGamma("tok-yes"), prices, kalshi)  # ...but PM said YES
    r = bt.run([PAIR]).results[0]
    assert r.resolution_mismatch is True
    # Held PM Yes + Kalshi NO -> BOTH legs paid: windfall, not arb. The point
    # is the mismatch flag; P&L math must still follow actual results.
    assert r.entries and r.entries[0].payout_usd == 2 * r.entries[0].contracts


def test_no_entries_when_gap_below_costs():
    h1 = hours_before_close(30)
    # PM ask 0.49 + Kalshi no_ask 0.50 = 0.99: gross 1c < fee+buffer+min_edge.
    kalshi = FakeKalshi("yes", [candle(h1, 0.53, 0.50)])
    prices = FakePrices([(h1, 0.48)])
    r = backtester(FakeGamma("tok-yes"), prices, kalshi).run([PAIR]).results[0]
    assert r.entries == []
    assert r.hours_observed == 1


def test_one_entry_per_direction_max():
    hs = [hours_before_close(30 - i) for i in range(5)]
    # Persistent gap for 5 hours -> still only one entry in that direction.
    kalshi = FakeKalshi("yes", [candle(h, 0.58, 0.55) for h in hs])
    prices = FakePrices([(h, 0.40) for h in hs])
    r = backtester(FakeGamma("tok-yes"), prices, kalshi).run([PAIR]).results[0]
    assert len(r.entries) == 1


def test_unresolved_pair_skipped():
    h1 = hours_before_close(30)
    kalshi = FakeKalshi("", [candle(h1, 0.58, 0.55)])     # result blank
    prices = FakePrices([(h1, 0.40)])
    r = backtester(FakeGamma("tok-yes"), prices, kalshi).run([PAIR]).results[0]
    assert r.skipped_reason is not None
