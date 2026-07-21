"""Settlement: resolved markets close positions and realize P&L."""

from __future__ import annotations

from datetime import datetime, timezone

from pmbot.models import Fill, KalshiMarket, Quote, Side, Signal, Venue
from pmbot.portfolio.ledger import Ledger
from pmbot.portfolio.settlement import Settler

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


class FakeGamma:
    """market_id -> (closed, winning_token_id)"""

    def __init__(self, resolutions):
        self._r = resolutions

    def get_resolution(self, market_id):
        return self._r.get(market_id, (False, None))


class FakeKalshi:
    """ticker -> KalshiMarket"""

    def __init__(self, markets):
        self._m = markets

    def get_market(self, ticker):
        return self._m.get(ticker)


def buy(led: Ledger, *, market_id, token_id, outcome, price, shares, venue):
    sig = Signal(market_id=market_id, token_id=token_id, outcome=outcome,
                 side=Side.BUY, target_price=price, size_usd=price * shares,
                 reason="test", venue=venue)
    led.record_fill(Fill(signal=sig, fill_price=price, size_usd=price * shares,
                         shares=shares, timestamp=NOW, mode="paper"))


def test_settles_arb_pair_and_realizes_locked_profit():
    """Winning leg pays $1, losing leg $0 — net = 1 - (a+b) per contract."""
    led = Ledger(":memory:")
    # 100 contracts each: PM YES @ 0.40, Kalshi NO @ 0.55 (cost $95 total).
    buy(led, market_id="0xmkt", token_id="tok-yes", outcome="Yes",
        price=0.40, shares=100, venue=Venue.POLYMARKET.value)
    buy(led, market_id="KX-T", token_id="kalshi:KX-T:no", outcome="NO",
        price=0.55, shares=100, venue=Venue.KALSHI.value)

    # Market resolves YES: PM leg wins, Kalshi NO loses.
    gamma = FakeGamma({"0xmkt": (True, "tok-yes")})
    kalshi = FakeKalshi({
        "KX-T": KalshiMarket(ticker="KX-T", event_ticker="KX", title="t",
                             status="settled", result="yes"),
    })
    n = Settler(led, gamma, kalshi).settle_open_positions()
    assert n == 2
    assert led.get_positions() == []          # flat after settlement
    # P&L: PM +0.60*100 = +60; Kalshi -0.55*100 = -55 -> net +5.
    assert abs(led.realized_pnl_total() - 5.0) < 1e-6
    led.close()


def test_unresolved_markets_left_open():
    led = Ledger(":memory:")
    buy(led, market_id="0xmkt", token_id="tok-yes", outcome="Yes",
        price=0.40, shares=10, venue=Venue.POLYMARKET.value)
    n = Settler(led, FakeGamma({}), FakeKalshi({})).settle_open_positions()
    assert n == 0
    assert len(led.get_positions()) == 1
    led.close()


def test_kalshi_unsettled_result_blank_left_open():
    led = Ledger(":memory:")
    buy(led, market_id="KX-T", token_id="kalshi:KX-T:no", outcome="NO",
        price=0.55, shares=10, venue=Venue.KALSHI.value)
    kalshi = FakeKalshi({
        "KX-T": KalshiMarket(ticker="KX-T", event_ticker="KX", title="t",
                             status="closed", result=""),
    })
    assert Settler(led, FakeGamma({}), kalshi).settle_open_positions() == 0
    assert len(led.get_positions()) == 1
    led.close()


def test_missing_kalshi_client_skips_kalshi_positions():
    led = Ledger(":memory:")
    buy(led, market_id="KX-T", token_id="kalshi:KX-T:no", outcome="NO",
        price=0.55, shares=10, venue=Venue.KALSHI.value)
    assert Settler(led, FakeGamma({}), kalshi=None).settle_open_positions() == 0
    led.close()


def test_force_settle_market_escape_hatch():
    """Markets that never resolve decisively (winner below the 0.99 auto
    threshold) can be settled manually so they stop pinning bankroll."""
    led = Ledger(":memory:")
    buy(led, market_id="0xstuck", token_id="tok-yes", outcome="Yes",
        price=0.40, shares=100, venue=Venue.POLYMARKET.value)
    buy(led, market_id="0xother", token_id="tok-b", outcome="Yes",
        price=0.40, shares=10, venue=Venue.POLYMARKET.value)
    n = Settler(led, FakeGamma({}), None).force_settle_market("0xstuck", 0.0)
    assert n == 1
    assert [p.market_id for p in led.get_positions()] == ["0xother"]  # untouched
    assert abs(led.realized_pnl_total() - (-40.0)) < 1e-6
    led.close()


def test_losing_pm_position_settles_to_zero():
    led = Ledger(":memory:")
    buy(led, market_id="0xmkt", token_id="tok-no", outcome="No",
        price=0.30, shares=50, venue=Venue.POLYMARKET.value)
    gamma = FakeGamma({"0xmkt": (True, "tok-yes")})   # other side won
    assert Settler(led, gamma, None).settle_open_positions() == 1
    assert abs(led.realized_pnl_total() - (-15.0)) < 1e-6   # lost the $15 stake
    led.close()
