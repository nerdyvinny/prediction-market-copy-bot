"""Tests for average-cost accounting, the ledger, and the paper executor."""

from __future__ import annotations

import logging

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


def test_ledger_followed_leaders_roundtrip(tmp_path):
    db = str(tmp_path / "led.db")
    led = Ledger(db)
    assert led.followed_leaders() == []
    led.set_followed_leaders({"0xlow": 0.2, "0xhigh": 0.9})
    assert led.followed_leaders() == ["0xhigh", "0xlow"]     # score-ordered
    led.set_followed_leaders({"0xnew": 0.5})                 # replace, not append
    led.close()
    with Ledger(db) as led2:                                 # survives reopen
        assert led2.followed_leaders() == ["0xnew"]


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


def test_settlement_frees_leader_budget():
    """Regression: settled positions must release the leader's exposure cap.

    Settlement fills carry no source_leader, so the old all-fills sum kept
    counting the BUY forever — leaders eventually looked "full" and every
    new copy was skipped. Exposure over open positions only fixes that.
    """
    led = Ledger(":memory:")
    _fill(led, _sig(side=Side.BUY, size_usd=50, uid="b1", leader="0xL1",
                    token="t1", market="m1"), price=0.50)
    assert led.exposure_for_leader("0xL1") == pytest.approx(50.0)

    # Market resolves against us: settlement SELLs all 100 shares at $0
    # (no source_leader on settlement fills, same as Settler writes them).
    from datetime import datetime, timezone
    from pmbot.models import Fill
    settle = Signal(market_id="m1", token_id="t1", outcome="Yes", side=Side.SELL,
                    target_price=0.0, size_usd=0.0, reason="settlement")
    led.record_fill(Fill(signal=settle, fill_price=0.0, size_usd=0.0, shares=100,
                         timestamp=datetime.now(timezone.utc), mode="paper"))

    pos = led.get_position("t1")
    assert pos is not None and pos.shares == pytest.approx(0.0)
    assert led.exposure_for_leader("0xL1") == pytest.approx(0.0)   # budget freed
    assert "0xL1" not in led.leader_exposures()
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
    _fill(led, _sig(side=Side.BUY, size_usd=50.0, uid="entry"), price=0.50)  # 100 sh
    fill = ex.execute(_sig(side=Side.SELL, size_usd=48.0))
    assert fill.fill_price == pytest.approx(0.48 * 0.99)
    led.close()


def test_paper_executor_logs_sell_proceeds_not_cost_basis(caplog):
    # Mirrored exits carry size_shares, so the signal's size_usd stays valued at
    # our avg cost while the fill realises something else entirely. The log line
    # must report the proceeds, or a winner reads as flat.
    from dataclasses import replace as dc_replace
    led = Ledger(":memory:")
    cache = FakeCache(Quote(token_id="tokA", bid=0.90, ask=0.92))
    ex = PaperExecutor(led, price_cache=cache, slippage_bps=0)
    _fill(led, _sig(side=Side.BUY, size_usd=50.0, uid="entry"), price=0.50)  # 100 sh
    sig = dc_replace(_sig(side=Side.SELL, size_usd=50.0, uid="exit"), size_shares=100.0)
    with caplog.at_level(logging.INFO, logger="pmbot.execution.paper_executor"):
        fill = ex.execute(sig)
    assert fill.size_usd == pytest.approx(100 * 0.90)   # 100 sh out at the bid
    assert "($90.00)" in caplog.text                     # proceeds, not the $50 basis
    assert "($50.00)" not in caplog.text
    led.close()


def test_paper_executor_sell_without_position_is_skipped():
    led = Ledger(":memory:")
    cache = FakeCache(Quote(token_id="tokA", bid=0.48, ask=0.52))
    ex = PaperExecutor(led, price_cache=cache, slippage_bps=0)
    assert ex.execute(_sig(side=Side.SELL, size_usd=48.0)) is None
    led.close()


def test_paper_executor_sell_cannot_flip_position_short():
    # Regression: we held 35.5 sh @ 0.845 ($30 at cost); the market collapsed
    # to ~0.001 and a copied "sell 100%" sized at $30 was converted to
    # 30,181 shares at the collapsed bid, flipping us −30k shares short.
    # SELLs must fill in shares, hard-capped at what we hold.
    led = Ledger(":memory:")
    _fill(led, _sig(side=Side.BUY, size_usd=30.0, uid="entry"), price=0.845)  # ~35.5 sh
    held = led.get_position("tokA").shares
    cache = FakeCache(Quote(token_id="tokA", bid=0.001, ask=0.002))
    ex = PaperExecutor(led, price_cache=cache, slippage_bps=0)

    fill = ex.execute(_sig(side=Side.SELL, size_usd=30.0, uid="exit"))
    assert fill is not None
    assert fill.shares == pytest.approx(held)
    assert fill.size_usd == pytest.approx(held * 0.001)
    pos = led.get_position("tokA")
    assert pos.shares == pytest.approx(0.0, abs=1e-9)
    led.close()


def test_paper_executor_sell_honors_size_shares():
    from dataclasses import replace as dc_replace
    led = Ledger(":memory:")
    _fill(led, _sig(side=Side.BUY, size_usd=50.0, uid="entry"), price=0.50)  # 100 sh
    cache = FakeCache(Quote(token_id="tokA", bid=0.40, ask=0.42))
    ex = PaperExecutor(led, price_cache=cache, slippage_bps=0)

    sig = dc_replace(_sig(side=Side.SELL, size_usd=20.0, uid="exit"), size_shares=40.0)
    fill = ex.execute(sig)
    assert fill.shares == pytest.approx(40.0)
    assert fill.size_usd == pytest.approx(40.0 * 0.40)
    assert led.get_position("tokA").shares == pytest.approx(60.0)
    led.close()


def test_paper_executor_copy_buy_skipped_without_book():
    # A copied ENTRY with no live book is skipped, not granted a perfect fill
    # at the leader's own price — that would inflate paper P&L exactly when
    # we're blind (CLOB outage).
    led = Ledger(":memory:")
    cache = FakeCache(Quote(token_id="tokA", bid=None, ask=None))
    ex = PaperExecutor(led, price_cache=cache, slippage_bps=0)
    assert ex.execute(_sig(side=Side.BUY, size_usd=50.0)) is None
    led.close()


def test_paper_executor_noncopy_buy_still_falls_back_to_target():
    # Non-copy signals (arb legs price at scan time) keep the fallback.
    led = Ledger(":memory:")
    cache = FakeCache(Quote(token_id="tokA", bid=None, ask=None))
    ex = PaperExecutor(led, price_cache=cache, slippage_bps=0)
    sig = _sig(side=Side.BUY, size_usd=50.0, leader=None)
    fill = ex.execute(sig)
    assert fill is not None and fill.fill_price == pytest.approx(0.50)
    led.close()


def test_paper_executor_copy_buy_respects_limit_price():
    # Limit semantics: never pay more than leader price + drift budget (0.03).
    # The strategy's drift guard checks the MID; a thin book's ask can sit far
    # above it and must not be chased.
    led = Ledger(":memory:")
    cache = FakeCache(Quote(token_id="tokA", bid=0.50, ask=0.60))
    ex = PaperExecutor(led, price_cache=cache, slippage_bps=0)
    assert ex.execute(_sig(side=Side.BUY, size_usd=50.0)) is None  # 0.60 > 0.53
    ok = FakeCache(Quote(token_id="tokA", bid=0.50, ask=0.52))
    ex2 = PaperExecutor(led, price_cache=ok, slippage_bps=0)
    fill = ex2.execute(_sig(side=Side.BUY, size_usd=50.0))
    assert fill is not None and fill.fill_price == pytest.approx(0.52)
    led.close()


def test_ledger_leader_observation_roundtrip(tmp_path):
    """Leader position tracking + seen uids survive a restart; pruning drops
    only uids old enough to have left every fetch window."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    db = str(tmp_path / "led.db")
    led = Ledger(db)
    led.record_leader_observation("0xl", "tok", 60.0, "u-old", now - timedelta(days=40))
    led.record_leader_observation("0xl", "tok", 100.0, "u-new", now)
    led.close()

    with Ledger(db) as led2:
        assert led2.load_leader_positions() == {("0xl", "tok"): 100.0}
        assert led2.load_seen_uids() == {"u-old", "u-new"}
        led2.prune_seen_trades(older_than_days=30.0)
        assert led2.load_seen_uids() == {"u-new"}


def test_paper_executor_rejects_nonpositive_size():
    led = Ledger(":memory:")
    cache = FakeCache(Quote(token_id="tokA", bid=0.48, ask=0.52))
    ex = PaperExecutor(led, price_cache=cache)
    assert ex.execute(_sig(size_usd=0)) is None
    led.close()
