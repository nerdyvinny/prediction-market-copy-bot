"""Offline tests for ExactCopyStrategy.generate(), using fakes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pmbot.models import Fill, LeaderTrade, Market, Side, Signal
from pmbot.portfolio.ledger import Ledger
from pmbot.strategy import ExactCopyStrategy

NOW = datetime.now(timezone.utc)


def _mkt(cid, *, closed=False, liq=10_000.0):
    return Market(market_id=cid, question=cid, end_date=None, liquidity_usd=liq, closed=closed)


def _trade(cid, token, side, price, shares, uid, ts=None):
    return LeaderTrade(
        leader="0xlead", market_id=cid, token_id=token, outcome="Yes", side=side,
        price=price, shares=shares, usd_size=shares * price, timestamp=ts or NOW, tx_hash=uid,
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


def _strategy(trades, markets, ledger, *, price_cache=None, min_leader_notional=0.0):
    return ExactCopyStrategy(
        FakeData(trades), FakeGamma(markets), ledger, leaders=["0xLEAD"],
        price_cache=price_cache, min_liquidity=5000, price_min=0.05, price_max=0.95,
        min_leader_notional=min_leader_notional,
    )


def test_buy_mirrored_and_filtered():
    markets = {
        "m_ok": _mkt("m_ok"),
        "m_closed": _mkt("m_closed", closed=True),
        "m_thin": _mkt("m_thin", liq=1000),
    }
    trades = [
        _trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "u1"),
        _trade("m_ok", "tokHI", Side.BUY, 0.98, 100, "u2"),      # skip: price band
        _trade("m_thin", "tokTHIN", Side.BUY, 0.50, 100, "u3"),  # skip: liquidity
        _trade("m_closed", "tokCL", Side.BUY, 0.50, 100, "u4"),  # skip: closed
    ]
    led = Ledger(":memory:")
    sigs = list(_strategy(trades, markets, led).generate())
    assert len(sigs) == 1
    s = sigs[0]
    assert isinstance(s, Signal)
    assert s.token_id == "tokOK" and s.side is Side.BUY
    assert s.source_uid == "u1" and s.source_leader == "0xlead"
    assert s.size_usd == 50.0
    led.close()


def test_dedupes_already_copied():
    markets = {"m_ok": _mkt("m_ok")}
    trades = [_trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "dup-uid")]
    led = Ledger(":memory:")
    sig = Signal("m_ok", "tokOK", "Yes", Side.BUY, 0.50, 50, "x",
                 source_leader="0xlead", source_uid="dup-uid")
    led.record_fill(Fill(signal=sig, fill_price=0.50, size_usd=50, shares=100,
                         timestamp=NOW, mode="paper"))
    assert list(_strategy(trades, markets, led).generate()) == []
    led.close()


def test_sell_mirrored_proportionally():
    markets = {"m_ok": _mkt("m_ok")}
    t0 = NOW - timedelta(minutes=10)
    t1 = NOW
    buy = _trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "u-buy", ts=t0)
    sell = _trade("m_ok", "tokOK", Side.SELL, 0.55, 40, "u-sell", ts=t1)  # 40% exit

    led = Ledger(":memory:")
    # We hold a smaller size than the leader (10 sh @ 0.50 = $5), simulating an
    # earlier cycle where the risk manager copy_fraction'd their entry down.
    our_buy_sig = Signal("m_ok", "tokOK", "Yes", Side.BUY, 0.50, 5, "copy",
                         source_leader="0xlead", source_uid="u-buy")
    led.record_fill(Fill(signal=our_buy_sig, fill_price=0.50, size_usd=5, shares=10,
                         timestamp=t0, mode="paper"))

    strat = _strategy([buy, sell], markets, led)
    # Seed leader-position tracking as if the buy was already observed in a
    # prior cycle (so this cycle only reacts to the new sell).
    strat._leader_shares[("0xlead", "tokOK")] = 100.0
    strat._seen_uids.add("u-buy")
    sigs = list(strat.generate())
    assert len(sigs) == 1
    s = sigs[0]
    assert s.side is Side.SELL
    # Leader sold 40% of their position -> mirror 40% of our $5 position.
    assert s.size_usd == 5 * 0.4
    assert s.size_shares == pytest.approx(10 * 0.4)  # 40% of our 10 shares
    led.close()


def test_sell_with_unknown_prior_leader_position_defaults_to_full_exit():
    markets = {"m_ok": _mkt("m_ok")}
    sell = _trade("m_ok", "tokOK", Side.SELL, 0.55, 40, "u-sell")
    led = Ledger(":memory:")
    our_buy_sig = Signal("m_ok", "tokOK", "Yes", Side.BUY, 0.50, 5, "copy",
                         source_leader="0xlead", source_uid="u-preexisting")
    led.record_fill(Fill(signal=our_buy_sig, fill_price=0.50, size_usd=5, shares=10,
                         timestamp=NOW, mode="paper"))
    sigs = list(_strategy([sell], markets, led).generate())
    assert len(sigs) == 1
    assert sigs[0].side is Side.SELL
    assert sigs[0].size_usd == 5.0   # full exit of our $5 position
    assert sigs[0].size_shares == pytest.approx(10.0)  # all our shares, in shares
    led.close()


def test_min_leader_notional_skips_small_buys_but_not_sells():
    markets = {"m_ok": _mkt("m_ok")}
    small_buy = _trade("m_ok", "tokA", Side.BUY, 0.50, 100, "u1")     # $50 notional
    small_sell = _trade("m_ok", "tokB", Side.SELL, 0.50, 100, "u2")   # $50 notional
    led = Ledger(":memory:")
    # We hold tokB, so the small sell should still be mirrored.
    our_buy = Signal("m_ok", "tokB", "Yes", Side.BUY, 0.50, 5, "copy",
                     source_leader="0xlead", source_uid="u-old")
    led.record_fill(Fill(signal=our_buy, fill_price=0.50, size_usd=5, shares=10,
                         timestamp=NOW, mode="paper"))
    sigs = list(_strategy([small_buy, small_sell], markets, led,
                          min_leader_notional=100.0).generate())
    assert len(sigs) == 1 and sigs[0].side is Side.SELL and sigs[0].token_id == "tokB"
    led.close()


def test_extreme_price_sell_still_mirrored():
    # Leader exits at 0.97 (outside the entry price band) — we must follow.
    markets = {"m_ok": _mkt("m_ok")}
    sell = _trade("m_ok", "tokOK", Side.SELL, 0.97, 100, "u-sell")
    led = Ledger(":memory:")
    our_buy = Signal("m_ok", "tokOK", "Yes", Side.BUY, 0.50, 5, "copy",
                     source_leader="0xlead", source_uid="u-old")
    led.record_fill(Fill(signal=our_buy, fill_price=0.50, size_usd=5, shares=10,
                         timestamp=NOW, mode="paper"))
    sigs = list(_strategy([sell], markets, led).generate())
    assert len(sigs) == 1 and sigs[0].side is Side.SELL
    led.close()


def test_round_tripped_entry_not_copied():
    # Both halves of a completed round-trip land in one tape. Copying the entry
    # would open and close at the same current price — pure spread loss.
    markets = {"m_ok": _mkt("m_ok")}
    t0, t1 = NOW - timedelta(minutes=20), NOW - timedelta(minutes=5)
    buy = _trade("m_ok", "tokRT", Side.BUY, 0.50, 100, "u-buy", ts=t0)
    sell = _trade("m_ok", "tokRT", Side.SELL, 0.48, 100, "u-sell", ts=t1)
    led = Ledger(":memory:")
    # No BUY signal, and no SELL either: we never opened, so there's nothing
    # of ours to mirror out of.
    assert list(_strategy([buy, sell], markets, led).generate()) == []
    led.close()


def test_round_trip_filter_ignores_partial_exit():
    # The leader trimmed but still holds — that's conviction, still copyable.
    markets = {"m_ok": _mkt("m_ok")}
    t0, t1 = NOW - timedelta(minutes=20), NOW - timedelta(minutes=5)
    buy = _trade("m_ok", "tokPT", Side.BUY, 0.50, 100, "u-buy", ts=t0)
    sell = _trade("m_ok", "tokPT", Side.SELL, 0.55, 40, "u-sell", ts=t1)
    led = Ledger(":memory:")
    sigs = list(_strategy([buy, sell], markets, led).generate())
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY and sigs[0].source_uid == "u-buy"
    led.close()


def test_round_trip_filter_tolerates_dust_crumb():
    # A "full" exit that undershoots by a rounding crumb is still a full exit.
    markets = {"m_ok": _mkt("m_ok")}
    t0, t1 = NOW - timedelta(minutes=20), NOW - timedelta(minutes=5)
    buy = _trade("m_ok", "tokRT", Side.BUY, 0.50, 100, "u-buy", ts=t0)
    sell = _trade("m_ok", "tokRT", Side.SELL, 0.48, 99.99999, "u-sell", ts=t1)
    led = Ledger(":memory:")
    assert list(_strategy([buy, sell], markets, led).generate()) == []
    led.close()


def test_round_trip_filter_does_not_retire_unrelated_entry():
    # A sell whose position predates the window must not retire a LATER buy of
    # the same token — that buy is a fresh, live signal.
    markets = {"m_ok": _mkt("m_ok")}
    t0, t1 = NOW - timedelta(minutes=20), NOW - timedelta(minutes=5)
    sell = _trade("m_ok", "tokOK", Side.SELL, 0.55, 500, "u-sell", ts=t0)
    buy = _trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "u-buy", ts=t1)
    led = Ledger(":memory:")
    sigs = list(_strategy([sell, buy], markets, led).generate())
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY and sigs[0].source_uid == "u-buy"
    led.close()


def test_sell_skipped_when_we_hold_nothing():
    markets = {"m_ok": _mkt("m_ok")}
    sell = _trade("m_ok", "tokOK", Side.SELL, 0.55, 40, "u-sell")
    led = Ledger(":memory:")
    assert list(_strategy([sell], markets, led).generate()) == []
    led.close()


def test_exit_only_leader_sell_mirrored_buy_ignored():
    # Leader dropped off the followed list while we still hold their copied
    # position: SELLs must still be mirrored, BUYs must never be copied.
    markets = {"m_ok": _mkt("m_ok")}
    t0, t1, t2 = NOW - timedelta(minutes=20), NOW - timedelta(minutes=10), NOW
    # Would pass every entry filter (cf. test_buy_mirrored_and_filtered)…
    buy_new = _trade("m_ok", "tokNEW", Side.BUY, 0.50, 100, "u-buy-new", ts=t0)
    # …and a buy adding to the token we hold: no signal, but it must still
    # feed leader-position tracking so the later sell mirrors proportionally.
    buy_held = _trade("m_ok", "tokHELD", Side.BUY, 0.50, 100, "u-buy-held", ts=t1)
    sell = _trade("m_ok", "tokHELD", Side.SELL, 0.55, 40, "u-sell", ts=t2)  # 40% exit

    led = Ledger(":memory:")
    our_buy = Signal("m_ok", "tokHELD", "Yes", Side.BUY, 0.50, 5, "copy",
                     source_leader="0xlead", source_uid="u-old")
    led.record_fill(Fill(signal=our_buy, fill_price=0.50, size_usd=5, shares=10,
                         timestamp=t1, mode="paper"))

    strat = _strategy([buy_new, buy_held, sell], markets, led)
    strat.set_leaders([], exit_only=["0xLEAD"])
    sigs = list(strat.generate())
    assert len(sigs) == 1
    s = sigs[0]
    assert s.side is Side.SELL and s.token_id == "tokHELD"
    # Leader sold 40 of their tracked 100 shares -> mirror 40% of ours.
    assert s.size_usd == pytest.approx(5 * 0.4)
    assert s.size_shares == pytest.approx(10 * 0.4)
    led.close()


class FakeQuote:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask


class FakePriceCache:
    def __init__(self, quote):
        self._quote = quote

    def get_quote(self, token_id):
        return self._quote


def test_old_buys_skipped_but_old_sells_mirrored():
    """The age guard (default 60 min) keeps a NEW leader's replayed history
    from being copied as fresh entries; exits are never age-filtered."""
    markets = {"m_ok": _mkt("m_ok")}
    old = NOW - timedelta(hours=3)
    led = Ledger(":memory:")
    buy = _trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "u-old-buy", ts=old)
    assert list(_strategy([buy], markets, led).generate()) == []
    led.close()

    led2 = Ledger(":memory:")
    our_buy = Signal("m_ok", "tokOK", "Yes", Side.BUY, 0.50, 5, "copy",
                     source_leader="0xlead", source_uid="u-old2")
    led2.record_fill(Fill(signal=our_buy, fill_price=0.50, size_usd=5, shares=10,
                          timestamp=old, mode="paper"))
    sell = _trade("m_ok", "tokOK", Side.SELL, 0.55, 100, "u-old-sell", ts=old)
    sigs = list(_strategy([sell], markets, led2).generate())
    assert len(sigs) == 1 and sigs[0].side is Side.SELL
    led2.close()


class FlakyGamma:
    """Raises for the first `fail_n` lookups, then serves markets."""

    def __init__(self, markets, fail_n=1):
        self._markets = markets
        self.calls = 0
        self.fail_n = fail_n

    def get_market(self, condition_id):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise RuntimeError("rate limited")
        return self._markets.get(condition_id)


def test_transient_market_lookup_failure_retries_next_cycle():
    """A rate-limited Gamma call must not consume the trade: the entry is
    retried on the next cycle once the lookup succeeds (errors are neither
    cached nor treated as a final decision)."""
    markets = {"m_ok": _mkt("m_ok")}
    trades = [_trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "u1")]
    led = Ledger(":memory:")
    strat = ExactCopyStrategy(
        FakeData(trades), FlakyGamma(markets), led, leaders=["0xlead"],
        min_liquidity=5000, price_min=0.05, price_max=0.95, min_leader_notional=0.0,
    )
    assert list(strat.generate()) == []          # cycle 1: lookup failed
    sigs = list(strat.generate())                # cycle 2: fresh lookup works
    assert len(sigs) == 1 and sigs[0].side is Side.BUY
    led.close()


def test_sell_mirrored_even_when_market_lookup_fails():
    """Exits are risk-reducing and must never be blocked by a dead API."""
    markets = {}                                  # every lookup fails
    sell = _trade("m_ok", "tokOK", Side.SELL, 0.55, 100, "u-sell")
    led = Ledger(":memory:")
    our_buy = Signal("m_ok", "tokOK", "Yes", Side.BUY, 0.50, 5, "copy",
                     source_leader="0xlead", source_uid="u-old")
    led.record_fill(Fill(signal=our_buy, fill_price=0.50, size_usd=5, shares=10,
                         timestamp=NOW, mode="paper"))
    strat = ExactCopyStrategy(
        FakeData([sell]), FlakyGamma({}, fail_n=10**9), led, leaders=["0xlead"],
        min_liquidity=5000, price_min=0.05, price_max=0.95, min_leader_notional=0.0,
    )
    sigs = list(strat.generate())
    assert len(sigs) == 1 and sigs[0].side is Side.SELL
    led.close()


class RaisingPriceCache:
    def get_quote(self, token_id):
        raise RuntimeError("CLOB down")


def test_drift_guard_fails_closed_on_quote_errors():
    """No verifiable quote (error or empty book) -> no copied entry. The old
    fail-open copied blind during CLOB outages, at fills no live order gets."""
    markets = {"m_ok": _mkt("m_ok")}
    trades = [_trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "u1")]

    led = Ledger(":memory:")
    strat = _strategy(trades, markets, led, price_cache=RaisingPriceCache())
    assert list(strat.generate()) == []
    led.close()

    led2 = Ledger(":memory:")
    empty = FakePriceCache(FakeQuote(bid=None, ask=None))
    strat2 = _strategy(trades, markets, led2, price_cache=empty)
    assert list(strat2.generate()) == []
    led2.close()


def test_leader_tracking_survives_restart(tmp_path):
    """Persisted leader positions keep exits PROPORTIONAL across restarts:
    without them, an unknown prior position degrades a leader's 40% trim
    into a full exit of ours."""
    db = str(tmp_path / "led.db")
    markets = {"m_ok": _mkt("m_ok")}
    t0, t1 = NOW - timedelta(minutes=30), NOW
    buy = _trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "u-buy", ts=t0)
    sell = _trade("m_ok", "tokOK", Side.SELL, 0.55, 40, "u-sell", ts=t1)  # 40% exit

    led = Ledger(db)
    strat = _strategy([buy], markets, led)
    list(strat.generate())                        # observes + persists the buy
    our_buy = Signal("m_ok", "tokOK", "Yes", Side.BUY, 0.50, 5, "copy",
                     source_leader="0xlead", source_uid="u-buy")
    led.record_fill(Fill(signal=our_buy, fill_price=0.50, size_usd=5, shares=10,
                         timestamp=t0, mode="paper"))
    led.close()

    led2 = Ledger(db)                             # "restart"
    strat2 = _strategy([buy, sell], markets, led2)
    sigs = list(strat2.generate())
    assert len(sigs) == 1 and sigs[0].side is Side.SELL
    assert sigs[0].size_shares == pytest.approx(10 * 0.4)   # proportional, not full
    led2.close()


def test_staleness_guard_skips_drifted_price():
    markets = {"m_ok": _mkt("m_ok")}
    trades = [_trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "u1")]
    led = Ledger(":memory:")
    # Current mid (0.90) has drifted way past the leader's 0.50 fill.
    stale_cache = FakePriceCache(FakeQuote(bid=0.89, ask=0.91))
    strat = _strategy(trades, markets, led, price_cache=stale_cache)
    assert list(strat.generate()) == []
    led.close()


def test_staleness_guard_allows_close_price():
    markets = {"m_ok": _mkt("m_ok")}
    trades = [_trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "u1")]
    led = Ledger(":memory:")
    close_cache = FakePriceCache(FakeQuote(bid=0.49, ask=0.51))
    strat = _strategy(trades, markets, led, price_cache=close_cache)
    assert len(list(strat.generate())) == 1
    led.close()


class FlakyPriceCache:
    """Raises on the first N calls, then serves a good quote."""

    def __init__(self, quote, failures=1):
        self._quote = quote
        self._left = failures
        self.calls = 0

    def get_quote(self, token_id):
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise RuntimeError("transient CLOB timeout")
        return self._quote


def test_unverifiable_quote_is_retried_not_retired():
    """A quote we could not FETCH is a transient skip, never a decision.

    Regression: the uid was retired before the drift check ran, so one CLOB
    hiccup dropped a copyable entry for the life of the process — it was
    never re-quoted again.
    """
    markets = {"m_ok": _mkt("m_ok")}
    trades = [_trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "u1")]
    led = Ledger(":memory:")
    cache = FlakyPriceCache(FakeQuote(bid=0.49, ask=0.51), failures=1)
    strat = _strategy(trades, markets, led, price_cache=cache)

    assert list(strat.generate()) == []          # blip: skipped, not decided
    assert len(list(strat.generate())) == 1      # recovered: copied
    assert cache.calls == 2                      # it actually re-quoted
    assert list(strat.generate()) == []          # now settled: no double-copy
    led.close()


def test_empty_book_is_retried_not_retired():
    """An empty book verifies nothing either — same transient treatment."""
    markets = {"m_ok": _mkt("m_ok")}
    trades = [_trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "u1")]
    led = Ledger(":memory:")

    class EmptyThenReal:
        def __init__(self):
            self.calls = 0

        def get_quote(self, token_id):
            self.calls += 1
            if self.calls == 1:
                return FakeQuote(bid=None, ask=None)
            return FakeQuote(bid=0.49, ask=0.51)

    cache = EmptyThenReal()
    strat = _strategy(trades, markets, led, price_cache=cache)
    assert list(strat.generate()) == []
    assert len(list(strat.generate())) == 1
    led.close()


def test_verified_drift_is_decided_once():
    """A drift we DID verify stays decided — no re-quoting every cycle."""
    markets = {"m_ok": _mkt("m_ok")}
    trades = [_trade("m_ok", "tokOK", Side.BUY, 0.50, 100, "u1")]
    led = Ledger(":memory:")
    cache = FakePriceCache(FakeQuote(bid=0.89, ask=0.91))
    strat = _strategy(trades, markets, led, price_cache=cache)
    assert list(strat.generate()) == []
    assert list(strat.generate()) == []
    assert "u1" in strat._processed_uids
    led.close()


def test_permanent_entry_rejects_are_retired():
    """Settled reasons (band, notional, closed, thin) retire the uid so the
    strategy doesn't re-decide them every cycle."""
    markets = {"m_ok": _mkt("m_ok"), "m_thin": _mkt("m_thin", liq=1000)}
    led = Ledger(":memory:")
    trades = [
        _trade("m_ok", "tokHI", Side.BUY, 0.98, 100, "band"),
        _trade("m_thin", "tokTHIN", Side.BUY, 0.50, 100, "thin"),
    ]
    strat = _strategy(trades, markets, led, min_leader_notional=0.0)
    assert list(strat.generate()) == []
    assert {"band", "thin"} <= strat._processed_uids
    led.close()


def test_unfilled_mirror_exit_is_retried():
    """A mirror-exit the RiskManager rejects (dust) must not be retired by the
    strategy — otherwise the leader's exit is dropped for good and our copied
    position rides unmanaged to resolution."""
    markets = {"m_ok": _mkt("m_ok")}
    led = Ledger(":memory:")
    buy = Signal("m_ok", "tokOK", "Yes", Side.BUY, 0.50, 30, "seed",
                 source_leader="0xlead", source_uid="seed-uid")
    led.record_fill(Fill(signal=buy, fill_price=0.50, size_usd=30, shares=60,
                         timestamp=NOW, mode="paper"))
    sell = _trade("m_ok", "tokOK", Side.SELL, 0.50, 10, "sell-uid")
    strat = _strategy([sell], markets, led)

    first = list(strat.generate())
    assert len(first) == 1 and first[0].side is Side.SELL
    # Nothing filled it, so the very next cycle offers it again.
    second = list(strat.generate())
    assert len(second) == 1 and second[0].side is Side.SELL
    assert "sell-uid" not in strat._processed_uids
    led.close()
