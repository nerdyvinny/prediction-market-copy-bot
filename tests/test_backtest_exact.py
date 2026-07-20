"""Offline tests for ExactCopyBacktester.simulate(), using fakes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pmbot.backtest import ExactCopyBacktester, vet_weights
from pmbot.config import Settings
from pmbot.models import LeaderTrade, Market, Side

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _mkt(cid, *, end_days_ago=1.0, closed=True):
    return Market(market_id=cid, question=cid, closed=closed,
                  end_date=NOW - timedelta(days=end_days_ago), liquidity_usd=10_000.0)


def _trade(cid, token, side, price, shares, days_ago, uid, leader="0xlead"):
    return LeaderTrade(
        leader=leader, market_id=cid, token_id=token, outcome="Yes", side=side,
        price=price, shares=shares, usd_size=shares * price,
        timestamp=NOW - timedelta(days=days_ago), tx_hash=uid, event_slug="grp",
    )


class FakeGamma:
    def __init__(self, resolutions):
        # resolutions: cid -> (Market, winner_token_id)
        self._res = resolutions

    def get_market_with_resolution(self, cid):
        return self._res.get(cid, (None, None))


def _settings():
    return Settings(bankroll_usd=500.0, copy_fraction=0.05, max_per_market_usd=50.0,
                    max_per_leader_usd=150.0, slippage_bps=0.0)


def _bt(resolutions, **kw):
    return ExactCopyBacktester(data=None, gamma=FakeGamma(resolutions),
                               settings=_settings(), **kw)


def test_market_lookup_error_not_cached():
    """A transient Gamma failure (rate limit) must not poison the memo: the
    next call for the same market retries and succeeds. A poisoned entry
    silently drops every trade in that market for the whole run."""
    good = (_mkt("m1"), "tokW")

    class FlakyGamma(FakeGamma):
        calls = 0

        def get_market_with_resolution(self, cid):
            FlakyGamma.calls += 1
            if FlakyGamma.calls == 1:
                raise RuntimeError("429 rate limited")
            return good

    bt = ExactCopyBacktester(data=None, gamma=FlakyGamma({}), settings=_settings())
    assert bt._market("m1") == (None, None)      # failed call: skipped, not stored
    assert bt._market("m1") == good              # retry succeeds
    assert bt._market("m1") == good and FlakyGamma.calls == 2   # now memoized


def test_buy_and_hold_settles_at_payout():
    res = {"m1": (_mkt("m1"), "tokW")}
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.50, 1000, 10, "u1")]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    assert len(rep.results) == 1
    r = rep.results[0]
    # Leader notional 1000*0.5=$500 -> 0.05 copy = $25 at 0.50 = 50 sh -> payout $50.
    assert r.size_usd == pytest.approx(25.0)
    assert r.won and r.pnl == pytest.approx(25.0)


def test_losing_hold_realizes_full_loss():
    res = {"m1": (_mkt("m1"), "tokOTHER")}
    tapes = {"0xlead": [_trade("m1", "tokL", Side.BUY, 0.50, 1000, 10, "u1")]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    r = rep.results[0]
    assert not r.won and r.pnl == pytest.approx(-25.0)


def test_partial_sell_mirrored_proportionally():
    # Leader buys 1000 sh @0.50, sells 400 (40%) @0.80, market later loses.
    res = {"m1": (_mkt("m1"), "tokOTHER")}
    tapes = {"0xlead": [
        _trade("m1", "tok", Side.BUY, 0.50, 1000, 10, "u1"),
        _trade("m1", "tok", Side.SELL, 0.80, 400, 5, "u2"),
    ]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    r = rep.results[0]
    # Ours: $25 -> 50 sh @0.50. Sell 40% (20 sh) @0.80 -> +6.00 realized.
    # Remaining 30 sh settle at 0 -> -15.00. Net -9.00 (vs -25 without the exit).
    assert r.pnl == pytest.approx(20 * 0.30 - 30 * 0.50)
    assert not r.won


def test_full_exit_before_resolution_locks_profit():
    res = {"m1": (_mkt("m1"), "tokOTHER")}
    tapes = {"0xlead": [
        _trade("m1", "tok", Side.BUY, 0.50, 1000, 10, "u1"),
        _trade("m1", "tok", Side.SELL, 0.80, 1000, 5, "u2"),
    ]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    r = rep.results[0]
    # Full exit @0.80: 50 sh * 0.30 = +15, nothing left to lose at resolution.
    assert r.pnl == pytest.approx(15.0)
    assert r.won


def test_min_leader_notional_skips_small_buys():
    res = {"m1": (_mkt("m1"), "tokW")}
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.50, 100, 10, "u1")]}  # $50 notional
    rep = _bt(res).simulate(tapes, lookback_days=30, min_leader_notional=100.0, now=NOW)
    assert rep.results == []


def test_simulate_upper_bounds_at_now():
    # A trade after `now` must not enter the walk-forward tune window.
    res = {"m1": (_mkt("m1"), "tokW")}
    future = _trade("m1", "tokW", Side.BUY, 0.50, 1000, -2, "u1")  # 2 days AFTER now
    rep = _bt(res).simulate({"0xlead": [future]}, lookback_days=30, now=NOW)
    assert rep.results == []


def test_simulate_settings_override_changes_sizing():
    res = {"m1": (_mkt("m1"), "tokW")}
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.50, 1000, 10, "u1")]}
    big = Settings(bankroll_usd=500.0, copy_fraction=0.10, max_per_market_usd=100.0,
                   max_per_leader_usd=300.0, slippage_bps=0.0)
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW, settings=big)
    assert rep.results[0].size_usd == pytest.approx(50.0)   # 10% of $500 leader notional


def test_lookback_excludes_old_trades():
    res = {"m1": (_mkt("m1"), "tokW")}
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.50, 1000, 45, "u1")]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    assert rep.results == []


def test_unresolved_market_not_copied():
    res = {"m1": (_mkt("m1", closed=False), None)}
    tapes = {"0xlead": [_trade("m1", "tok", Side.BUY, 0.50, 1000, 10, "u1")]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    assert rep.results == []


def test_sell_with_unknown_prior_position_is_full_exit():
    # Leader's buy predates the lookback window; only the sell is visible.
    # We hold nothing (never copied the entry), so nothing should be mirrored.
    res = {"m1": (_mkt("m1"), "tokW")}
    tapes = {"0xlead": [_trade("m1", "tok", Side.SELL, 0.80, 400, 5, "u2")]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    assert rep.results == []


def test_leader_weights_scale_sizing():
    res = {"m1": (_mkt("m1"), "tokW")}
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.50, 1000, 10, "u1")]}
    # 2x weight doubles the 5% base fraction -> $50 instead of $25.
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW,
                            leader_weights={"0xlead": 2.0})
    assert rep.results[0].size_usd == pytest.approx(50.0)
    # Weights are clamped: 10x requested -> 2x applied.
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW,
                            leader_weights={"0xlead": 10.0})
    assert rep.results[0].size_usd == pytest.approx(50.0)


def test_vet_weights_mean_normalized():
    w = vet_weights({"a": 0.30, "b": 0.10})   # mean 0.20
    assert w["a"] == pytest.approx(1.5)
    assert w["b"] == pytest.approx(0.5)
    assert vet_weights({}) == {}
    assert vet_weights({"a": -0.1}) == {}


def test_min_hours_to_resolution_skips_late_entries():
    # Market ends 1 day after NOW-10d entry... make entry 2h before close.
    m = _mkt("m1", end_days_ago=10.0)   # closes NOW-10d
    res = {"m1": (m, "tokW")}
    # Entry 2 hours before the close (days_ago = 10 + 2/24).
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.50, 1000, 10 + 2 / 24, "u1")]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW, min_hours_to_resolution=6.0)
    assert rep.results == []
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW, min_hours_to_resolution=1.0)
    assert len(rep.results) == 1


def test_bankroll_frees_after_resolution():
    # Two sequential markets, each demanding the full $50/market cap; bankroll
    # never blocks because the first resolves before the second entry.
    res = {
        "m1": (_mkt("m1", end_days_ago=8), "tok1"),
        "m2": (_mkt("m2", end_days_ago=1), "tok2"),
    }
    tapes = {"0xlead": [
        _trade("m1", "tok1", Side.BUY, 0.50, 2000, 10, "u1"),   # $1000 -> capped $50
        _trade("m2", "tok2", Side.BUY, 0.50, 2000, 5, "u2"),    # after m1 resolved
    ]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    assert len(rep.results) == 2
    assert all(r.size_usd == pytest.approx(50.0) for r in rep.results)
    assert all(r.won for r in rep.results)
