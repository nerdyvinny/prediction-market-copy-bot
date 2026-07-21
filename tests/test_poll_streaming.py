"""Engine.poll_once streaming: a leader's BUY + SELL arriving in one batch
must BOTH be mirrored.

Regression: the engine used to materialize the whole signal batch before
executing anything, so a SELL generated right after its BUY found no position
to mirror, was consumed, and the copied position rode unmanaged to resolution.
Streaming execution records each fill before the generator resumes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pmbot.config import Settings
from pmbot.engine import Engine
from pmbot.execution.paper_executor import PaperExecutor
from pmbot.models import LeaderTrade, Market, Quote, Side
from pmbot.portfolio.ledger import Ledger
from pmbot.risk import RiskManager
from pmbot.strategy import ExactCopyStrategy

NOW = datetime.now(timezone.utc)


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


class FakeCache:
    def __init__(self, quote):
        self._q = quote

    def get_quote(self, token_id, force=False):
        return self._q


class NoopSelector:
    def select(self, now=None, incumbents=None, **kw):
        return []


def test_same_batch_buy_then_sell_is_fully_mirrored():
    markets = {"m": Market(market_id="m", question="m", end_date=None,
                           liquidity_usd=10_000, closed=False)}
    t0, t1 = NOW - timedelta(minutes=5), NOW
    buy = LeaderTrade(leader="0xlead", market_id="m", token_id="tok", outcome="Yes",
                      side=Side.BUY, price=0.50, shares=100, usd_size=50.0,
                      timestamp=t0, tx_hash="u-buy")
    sell = LeaderTrade(leader="0xlead", market_id="m", token_id="tok", outcome="Yes",
                       side=Side.SELL, price=0.55, shares=100, usd_size=55.0,
                       timestamp=t1, tx_hash="u-sell")

    s = Settings(db_path=":memory:", arb_enabled=False)
    ledger = Ledger(":memory:")
    quote = FakeCache(Quote(token_id="tok", bid=0.49, ask=0.51))
    strategy = ExactCopyStrategy(
        FakeData([buy, sell]), FakeGamma(markets), ledger, leaders=["0xlead"],
        price_cache=quote, min_liquidity=0, price_min=0.0, price_max=1.0,
        min_leader_notional=0.0,
    )
    eng = Engine(
        settings=s, data=FakeData([]), gamma=FakeGamma(markets), price_cache=quote,
        ledger=ledger, executor=PaperExecutor(ledger, price_cache=quote, slippage_bps=0),
        risk=RiskManager(ledger, s), selector=NoopSelector(), strategy=strategy,
    )

    fills, n_signals = eng.poll_once()

    assert n_signals == 2
    assert fills == 2                              # BUY copied AND SELL mirrored
    pos = ledger.get_position("tok")
    assert pos is not None and pos.shares == pytest.approx(0.0, abs=1e-9)
    assert pos.realized_pnl == pytest.approx((0.49 - 0.51) * (5.0 / 0.51))
    # (bought $5 at the 0.51 ask, exited at the 0.49 bid — paid the spread,
    # but the position is MANAGED instead of riding to resolution)
    eng.close()


def test_sell_only_batch_still_skips_cleanly_when_nothing_held():
    """A leader SELL with no position of ours stays a clean no-op."""
    markets = {"m": Market(market_id="m", question="m", end_date=None,
                           liquidity_usd=10_000, closed=False)}
    sell = LeaderTrade(leader="0xlead", market_id="m", token_id="tok", outcome="Yes",
                       side=Side.SELL, price=0.55, shares=100, usd_size=55.0,
                       timestamp=NOW, tx_hash="u-sell")
    s = Settings(db_path=":memory:", arb_enabled=False)
    ledger = Ledger(":memory:")
    quote = FakeCache(Quote(token_id="tok", bid=0.49, ask=0.51))
    strategy = ExactCopyStrategy(
        FakeData([sell]), FakeGamma(markets), ledger, leaders=["0xlead"],
        price_cache=quote, min_liquidity=0, price_min=0.0, price_max=1.0,
        min_leader_notional=0.0,
    )
    eng = Engine(
        settings=s, data=FakeData([]), gamma=FakeGamma(markets), price_cache=quote,
        ledger=ledger, executor=PaperExecutor(ledger, price_cache=quote, slippage_bps=0),
        risk=RiskManager(ledger, s), selector=NoopSelector(), strategy=strategy,
    )
    fills, n_signals = eng.poll_once()
    assert (fills, n_signals) == (0, 0)
    eng.close()
