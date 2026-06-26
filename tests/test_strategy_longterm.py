"""Offline tests for LongTermCopyStrategy.generate() filtering, using fakes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pmbot.models import Fill, LeaderTrade, Market, Side, Signal
from pmbot.portfolio.ledger import Ledger
from pmbot.strategy import LongTermCopyStrategy

NOW = datetime.now(timezone.utc)


def _mkt(cid, *, days, closed=False, liq=10_000.0):
    return Market(
        market_id=cid, question=cid, end_date=None if days is None else NOW + timedelta(days=days),
        liquidity_usd=liq, closed=closed,
    )


def _trade(cid, token, side, price, uid):
    return LeaderTrade(
        leader="0xlead", market_id=cid, token_id=token, outcome="Yes", side=side,
        price=price, shares=100, usd_size=100 * price, timestamp=NOW, tx_hash=uid,
        event_slug="grp",
    )


class FakeData:
    def __init__(self, trades):
        self._trades = trades

    def get_trades(self, *, user=None, market=None, limit=100):
        return list(self._trades)


class FakeGamma:
    def __init__(self, markets):
        self._markets = markets

    def get_market(self, condition_id):
        return self._markets.get(condition_id)


def _strategy(trades, markets, ledger):
    return LongTermCopyStrategy(
        FakeData(trades), FakeGamma(markets), ledger, leaders=["0xLEAD"],
        min_days_to_resolution=7, min_liquidity=5000, price_min=0.05, price_max=0.95,
    )


def test_generate_applies_all_filters():
    markets = {
        "m_ok": _mkt("m_ok", days=30),
        "m_closed": _mkt("m_closed", days=30, closed=True),
        "m_soon": _mkt("m_soon", days=2),
        "m_thin": _mkt("m_thin", days=30, liq=1000),
        "m_nodate": _mkt("m_nodate", days=None),
    }
    trades = [
        _trade("m_ok", "tokOK", Side.BUY, 0.50, "u1"),       # YIELD
        _trade("m_ok", "tokOK", Side.SELL, 0.50, "u2"),      # skip: sell
        _trade("m_ok", "tokHI", Side.BUY, 0.98, "u3"),       # skip: price band
        _trade("m_ok", "tokLO", Side.BUY, 0.02, "u4"),       # skip: price band
        _trade("m_soon", "tokSOON", Side.BUY, 0.50, "u5"),   # skip: horizon
        _trade("m_thin", "tokTHIN", Side.BUY, 0.50, "u6"),   # skip: liquidity
        _trade("m_closed", "tokCL", Side.BUY, 0.50, "u7"),   # skip: closed
        _trade("m_nodate", "tokND", Side.BUY, 0.50, "u8"),   # skip: no end date
    ]
    led = Ledger(":memory:")
    sigs = list(_strategy(trades, markets, led).generate())
    assert len(sigs) == 1
    s = sigs[0]
    assert isinstance(s, Signal)
    assert s.token_id == "tokOK" and s.side is Side.BUY
    assert s.source_uid == "u1" and s.source_leader == "0xlead"
    assert s.size_usd == 50.0      # leader notional carried as copy target
    led.close()


def test_generate_dedupes_already_copied():
    markets = {"m_ok": _mkt("m_ok", days=30)}
    trades = [_trade("m_ok", "tokOK", Side.BUY, 0.50, "dup-uid")]
    led = Ledger(":memory:")
    # Pre-record a fill for that uid -> should be skipped next time.
    sig = Signal("m_ok", "tokOK", "Yes", Side.BUY, 0.50, 50, "x",
                 source_leader="0xlead", source_uid="dup-uid")
    led.record_fill(Fill(signal=sig, fill_price=0.50, size_usd=50, shares=100,
                         timestamp=NOW, mode="paper"))
    assert list(_strategy(trades, markets, led).generate()) == []
    led.close()
