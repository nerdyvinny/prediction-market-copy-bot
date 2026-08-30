"""Settlement: resolved markets close positions and realize P&L."""

from __future__ import annotations

from datetime import datetime, timezone

from pmbot.models import Fill, Side, Signal, Venue
from pmbot.portfolio.ledger import Ledger
from pmbot.portfolio.settlement import Settler

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


class FakeGamma:
    """market_id -> (closed, winning_token_id)"""

    def __init__(self, resolutions):
        self._r = resolutions

    def get_resolution(self, market_id):
        return self._r.get(market_id, (False, None))


def buy(led: Ledger, *, market_id, token_id, outcome, price, shares,
        venue=Venue.POLYMARKET.value):
    sig = Signal(market_id=market_id, token_id=token_id, outcome=outcome,
                 side=Side.BUY, target_price=price, size_usd=price * shares,
                 reason="test", venue=venue)
    led.record_fill(Fill(signal=sig, fill_price=price, size_usd=price * shares,
                         shares=shares, timestamp=NOW, mode="paper"))


def test_winning_position_settles_at_one():
    led = Ledger(":memory:")
    buy(led, market_id="0xmkt", token_id="tok-yes", outcome="Yes",
        price=0.40, shares=100)
    gamma = FakeGamma({"0xmkt": (True, "tok-yes")})
    assert Settler(led, gamma).settle_open_positions() == 1
    assert led.get_positions() == []                        # flat after settlement
    assert abs(led.realized_pnl_total() - 60.0) < 1e-6      # (1.00 - 0.40) * 100
    led.close()


def test_unresolved_markets_left_open():
    led = Ledger(":memory:")
    buy(led, market_id="0xmkt", token_id="tok-yes", outcome="Yes",
        price=0.40, shares=10)
    n = Settler(led, FakeGamma({})).settle_open_positions()
    assert n == 0
    assert len(led.get_positions()) == 1
    led.close()


def test_closed_market_with_no_winner_yet_is_left_open():
    """Settlement lag is normal: a market reports closed with the winner still
    unpublished for days. Waiting is correct; guessing is not."""
    led = Ledger(":memory:")
    buy(led, market_id="0xmkt", token_id="tok-yes", outcome="Yes",
        price=0.40, shares=10)
    gamma = FakeGamma({"0xmkt": (True, None)})              # closed, winner unknown
    assert Settler(led, gamma).settle_open_positions() == 0
    assert len(led.get_positions()) == 1
    led.close()


def test_force_settle_market_escape_hatch():
    """Markets that never resolve decisively (winner below the 0.99 auto
    threshold) can be settled manually so they stop pinning bankroll."""
    led = Ledger(":memory:")
    buy(led, market_id="0xstuck", token_id="tok-yes", outcome="Yes",
        price=0.40, shares=100)
    buy(led, market_id="0xother", token_id="tok-b", outcome="Yes",
        price=0.40, shares=10)
    n = Settler(led, FakeGamma({})).force_settle_market("0xstuck", 0.0)
    assert n == 1
    assert [p.market_id for p in led.get_positions()] == ["0xother"]  # untouched
    assert abs(led.realized_pnl_total() - (-40.0)) < 1e-6
    led.close()


def test_losing_pm_position_settles_to_zero():
    led = Ledger(":memory:")
    buy(led, market_id="0xmkt", token_id="tok-no", outcome="No",
        price=0.30, shares=50)
    gamma = FakeGamma({"0xmkt": (True, "tok-yes")})   # other side won
    assert Settler(led, gamma).settle_open_positions() == 1
    assert abs(led.realized_pnl_total() - (-15.0)) < 1e-6   # lost the $15 stake
    led.close()
