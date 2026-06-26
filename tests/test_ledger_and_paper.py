"""Tests for average-cost accounting, the ledger, and the paper executor."""

from __future__ import annotations

import pytest

from pmbot.execution.paper_executor import PaperExecutor
from pmbot.models import Quote, Side, Signal
from pmbot.portfolio.ledger import Ledger, apply_fill


# --- pure accounting -----------------------------------------------------
def test_apply_fill_increases_and_averages():
    eff = apply_fill(0, 0, Side.BUY, 100, 0.50)
    assert eff.new_shares == 100 and eff.new_avg == 0.50 and eff.realized_delta == 0
    eff2 = apply_fill(100, 0.50, Side.BUY, 100, 0.60)
    assert eff2.new_shares == 200
    assert eff2.new_avg == pytest.approx(0.55)
    assert eff2.realized_delta == 0


def test_apply_fill_partial_close_realizes_pnl():
    # long 100 @ 0.50, sell 40 @ 0.70 -> realized (0.70-0.50)*40 = 8.0
    eff = apply_fill(100, 0.50, Side.SELL, 40, 0.70)
    assert eff.new_shares == 60
    assert eff.new_avg == 0.50            # unchanged on partial close
    assert eff.realized_delta == pytest.approx(8.0)


def test_apply_fill_full_close_and_flip():
    # long 100 @ 0.50, sell 150 @ 0.40 -> close 100 (loss -10), flip short 50 @ 0.40
    eff = apply_fill(100, 0.50, Side.SELL, 150, 0.40)
    assert eff.new_shares == -50
    assert eff.new_avg == 0.40
    assert eff.realized_delta == pytest.approx((0.40 - 0.50) * 100)


def test_apply_fill_short_then_cover_profit():
    # short 100 @ 0.60, buy 100 @ 0.45 -> realized (0.60-0.45)*100 = 15
    eff = apply_fill(-100, 0.60, Side.BUY, 100, 0.45)
    assert eff.new_shares == 0
    assert eff.realized_delta == pytest.approx(15.0)


# --- ledger --------------------------------------------------------------
def _sig(side=Side.BUY, size_usd=50.0, uid="uid-1", leader="0xLEAD", token="tokA", market="mktA"):
    return Signal(
        market_id=market, token_id=token, outcome="Yes", side=side,
        target_price=0.50, size_usd=size_usd, reason="test",
        source_leader=leader, source_uid=uid,
    )


def _fill(ledger, sig, price=0.50):
    from datetime import datetime, timezone
    from pmbot.models import Fill
    f = Fill(signal=sig, fill_price=price, size_usd=sig.size_usd,
             shares=sig.size_usd / price, timestamp=datetime.now(timezone.utc), mode="paper")
    ledger.record_fill(f)
    return f


def test_ledger_records_and_dedupes():
    led = Ledger(":memory:")
    assert led.has_copied("uid-1") is False
    _fill(led, _sig(uid="uid-1"))
    assert led.has_copied("uid-1") is True
    assert led.fill_count() == 1
    pos = led.get_position("tokA")
    assert pos is not None and pos.shares == pytest.approx(100.0)  # 50 / 0.50
    led.close()


def test_ledger_summary_and_leader_exposures():
    led = Ledger(":memory:")
    _fill(led, _sig(side=Side.BUY, size_usd=50, uid="a", leader="0xL1", token="t1", market="m1"), price=0.50)
    _fill(led, _sig(side=Side.BUY, size_usd=30, uid="b", leader="0xL2", token="t2", market="m2"), price=0.60)
    s = led.summary()
    assert s["fills"] == 2
    assert s["open_positions"] == 2
    assert s["deployed_usd"] == pytest.approx(50.0 + 30.0)  # 100*0.5 + 50*0.6
    assert s["realized_pnl"] == pytest.approx(0.0)
    assert s["leaders"] == 2
    exps = led.leader_exposures()
    assert exps["0xL1"] == pytest.approx(50.0)
    assert exps["0xL2"] == pytest.approx(30.0)
    led.close()


def test_ledger_realized_pnl_and_exposures():
    led = Ledger(":memory:")
    _fill(led, _sig(side=Side.BUY, size_usd=50, uid="b1"), price=0.50)   # +100 sh @0.50
    _fill(led, _sig(side=Side.SELL, size_usd=35, uid="s1"), price=0.70)  # sell 50 sh @0.70
    pos = led.get_position("tokA")
    assert pos.shares == pytest.approx(50.0)
    assert pos.realized_pnl == pytest.approx((0.70 - 0.50) * 50)
    # net USD via leader = 50 (buy) - 35 (sell) = 15
    assert led.exposure_for_leader("0xLEAD") == pytest.approx(15.0)
    assert led.exposure_for_market("mktA") > 0
    led.close()


# --- paper executor ------------------------------------------------------
class FakeCache:
    def __init__(self, quote):
        self._q = quote

    def get_quote(self, token_id, force=False):
        return self._q


def test_paper_executor_buy_fills_at_ask_plus_slippage():
    led = Ledger(":memory:")
    cache = FakeCache(Quote(token_id="tokA", bid=0.48, ask=0.52))
    ex = PaperExecutor(led, price_cache=cache, slippage_bps=100)  # 1%
    fill = ex.execute(_sig(side=Side.BUY, size_usd=52.0))
    assert fill is not None
    assert fill.fill_price == pytest.approx(0.52 * 1.01)
    assert fill.shares == pytest.approx(52.0 / (0.52 * 1.01))
    led.close()


def test_paper_executor_sell_fills_at_bid_minus_slippage():
    led = Ledger(":memory:")
    cache = FakeCache(Quote(token_id="tokA", bid=0.48, ask=0.52))
    ex = PaperExecutor(led, price_cache=cache, slippage_bps=100)
    fill = ex.execute(_sig(side=Side.SELL, size_usd=48.0))
    assert fill.fill_price == pytest.approx(0.48 * 0.99)
    led.close()


def test_paper_executor_falls_back_to_target_when_no_quote():
    led = Ledger(":memory:")
    cache = FakeCache(Quote(token_id="tokA", bid=None, ask=None))
    ex = PaperExecutor(led, price_cache=cache, slippage_bps=0)
    fill = ex.execute(_sig(side=Side.BUY, size_usd=50.0))  # target_price=0.50
    assert fill.fill_price == pytest.approx(0.50)
    led.close()


def test_paper_executor_rejects_nonpositive_size():
    led = Ledger(":memory:")
    cache = FakeCache(Quote(token_id="tokA", bid=0.48, ask=0.52))
    ex = PaperExecutor(led, price_cache=cache)
    assert ex.execute(_sig(size_usd=0)) is None
    led.close()
