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


def test_in_game_entry_copied_when_hours_filter_off():
    """PM daily-sports markets put end_date at START of day, so in-game trades
    sit 'after close'. With min_hours_to_resolution at its default 0 they must
    still be copied (the live strategy copies them); only a positive knob may
    exclude them."""
    res = {"m1": (_mkt("m1", end_days_ago=2.0), "tokW")}
    # Trade 1 day ago = a full day AFTER the market's end_date.
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.50, 1000, 1, "u1")]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    assert len(rep.results) == 1                 # copied and settled at payout
    rep2 = _bt(res).simulate(tapes, lookback_days=30, now=NOW,
                             min_hours_to_resolution=6.0)
    assert len(rep2.results) == 0                # knob ON excludes it


def test_skip_price_band_punches_out_the_middle_only():
    """The band knob must remove the middle and leave both tails copied.

    A plain `price_min`/`price_max` narrowing cannot express this, which is the
    whole reason the knob exists — if it ever collapsed to an edge move, a
    "the middle is where we bleed" test would silently become "we stopped
    buying anything dear", a different rule with a different P&L.
    """
    res = {c: (_mkt(c), f"tok{c}") for c in ("m1", "m2", "m3")}
    tapes = {"0xlead": [
        _trade("m1", "tokm1", Side.BUY, 0.30, 1000, 10, "u1"),   # below the hole
        _trade("m2", "tokm2", Side.BUY, 0.70, 1000, 10, "u2"),   # inside it
        _trade("m3", "tokm3", Side.BUY, 0.82, 1000, 10, "u3"),   # above it
    ]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    assert {r.token_id for r in rep.results} == {"tokm1", "tokm2", "tokm3"}

    held = _bt(res).simulate(tapes, lookback_days=30, now=NOW,
                             skip_price_band=(0.60, 0.80))
    assert {r.token_id for r in held.results} == {"tokm1", "tokm3"}


def test_skip_price_band_is_half_open_at_the_top():
    """Adjacent bands must tile: 0.80 belongs to the next band up, not this one.
    A closed upper bound would double-count a boundary price whenever two bands
    are compared side by side."""
    res = {c: (_mkt(c), f"tok{c}") for c in ("m1", "m2")}
    tapes = {"0xlead": [
        _trade("m1", "tokm1", Side.BUY, 0.60, 1000, 10, "u1"),   # lower edge: excluded
        _trade("m2", "tokm2", Side.BUY, 0.80, 1000, 10, "u2"),   # upper edge: kept
    ]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW,
                            skip_price_band=(0.60, 0.80))
    assert {r.token_id for r in rep.results} == {"tokm2"}


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


# --- protective stop-loss (opt-in; off by default) -------------------------

def _series(*points):
    """(hours_before_NOW, price) -> the (unix_ts, price) shape simulate() wants."""
    return [(int((NOW - timedelta(hours=h)).timestamp()), p) for h, p in points]


def test_stop_fires_before_resolution_and_caps_the_loss():
    """A loser that drifts down must be cut at the stop instead of settling at 0."""
    res = {"m1": (_mkt("m1", end_days_ago=0.0), "tokLOSE")}   # our token loses
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.80, 1000, 2, "u1")]}
    # entry 48h before NOW at 0.80; price slides to 0.40 (a -50% drawdown) at 24h.
    prices = {"tokW": _series((47, 0.78), (24, 0.40), (2, 0.05))}

    held = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    assert held.results[0].closed_by == "resolution"

    stopped = _bt(res).simulate(tapes, lookback_days=30, now=NOW,
                                stop_loss_frac=0.30, price_series=prices)
    r = stopped.results[0]
    assert r.closed_by == "stop-loss"
    assert r.pnl > held.results[0].pnl           # cutting beat riding it to zero
    assert r.resolve_ts == NOW - timedelta(hours=24)   # fired at the first breach


def test_stop_does_not_fire_when_price_holds_above_trigger():
    res = {"m1": (_mkt("m1", end_days_ago=0.0), "tokW")}      # our token wins
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.80, 1000, 2, "u1")]}
    prices = {"tokW": _series((47, 0.75), (24, 0.70), (2, 0.95))}   # dips to -12% only
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW,
                            stop_loss_frac=0.30, price_series=prices)
    assert rep.results[0].closed_by == "resolution"


def test_stop_is_off_by_default_and_needs_a_price_series():
    """Live leader-vetting calls simulate() with neither arg — behaviour must
    be untouched, and a stop without prices must not silently half-apply."""
    res = {"m1": (_mkt("m1", end_days_ago=0.0), "tokLOSE")}
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.80, 1000, 2, "u1")]}
    prices = {"tokW": _series((24, 0.10))}

    plain = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    no_prices = _bt(res).simulate(tapes, lookback_days=30, now=NOW, stop_loss_frac=0.30)
    assert [r.pnl for r in plain.results] == [r.pnl for r in no_prices.results]
    armed = _bt(res).simulate(tapes, lookback_days=30, now=NOW,
                              stop_loss_frac=0.30, price_series=prices)
    assert armed.results[0].pnl != plain.results[0].pnl   # prices supplied: it bites


def test_leader_exit_before_the_stop_wins():
    """If the leader bails first, that's the exit we copy — no later stop fires."""
    res = {"m1": (_mkt("m1", end_days_ago=0.0), "tokLOSE")}
    tapes = {"0xlead": [
        _trade("m1", "tokW", Side.BUY, 0.80, 1000, 3, "u1"),
        _trade("m1", "tokW", Side.SELL, 0.70, 1000, 2, "u2"),   # full exit at 48h
    ]}
    prices = {"tokW": _series((24, 0.10))}                       # breach comes later
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW,
                            stop_loss_frac=0.30, price_series=prices)
    assert len(rep.results) == 1
    assert rep.results[0].closed_by == "leader-exit"


def test_resolve_at_override_gives_in_game_trades_a_real_holding_window():
    """Sports end_date precedes an in-game entry, so the tranche settles at once
    and no stop can ever bite. An override restores a real window."""
    res = {"m1": (_mkt("m1", end_days_ago=2.0), "tokLOSE")}   # end_date BEFORE entry
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.80, 1000, 1, "u1")]}
    prices = {"tokW": _series((12, 0.30))}                    # -62% twelve hours out

    # Without the override the position is already "resolved" at entry.
    blind = _bt(res).simulate(tapes, lookback_days=30, now=NOW,
                              stop_loss_frac=0.30, price_series=prices)
    assert blind.results[0].closed_by == "resolution"

    fixed = _bt(res).simulate(
        tapes, lookback_days=30, now=NOW, stop_loss_frac=0.30, price_series=prices,
        resolve_at={"tokW": NOW - timedelta(hours=6)},
    )
    assert fixed.results[0].closed_by == "stop-loss"
    assert fixed.results[0].pnl > blind.results[0].pnl


# --- the live path ---------------------------------------------------------
#
# These exist because the sim used to copy a bot that does not exist: it scored
# the live roster at +11.9% over a month the bot lost 2.9% on. Each test pins
# one rule of the real path, and the rules are NOT opt-in — they come from the
# same settings object the live bot reads.


def _thin(cid, liq):
    return Market(market_id=cid, question=cid, closed=True,
                  end_date=NOW - timedelta(days=1), liquidity_usd=liq)


def _at(t, offset=10):
    return int(t.timestamp.timestamp()) + offset


def test_thin_markets_are_skipped_without_being_asked():
    """The floor comes from settings, so a caller cannot forget it."""
    res = {"m1": (_thin("m1", 900.0), "tokW")}
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.50, 1000, 1, "u1")]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    assert rep.results == []
    assert rep.skipped["liquidity"] == 1
    # Only an explicit zero turns it off, for a deliberate sweep.
    assert len(_bt(res).simulate(tapes, lookback_days=30, now=NOW,
                                 min_liquidity=0.0).results) == 1


def test_unknown_liquidity_passes_like_live_but_is_counted():
    """Gamma drops liquidityNum on close, so most resolved markets read None.
    Live lets those through; the counter is what stops a caller reading
    'not filtered' as 'checked and fine'."""
    res = {"m1": (_thin("m1", None), "tokW")}
    tapes = {"0xlead": [_trade("m1", "tokW", Side.BUY, 0.50, 1000, 1, "u1")]}
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW)
    assert len(rep.results) == 1
    assert rep.skipped["liquidity_unknown"] == 1
    assert "liquidity" not in rep.skipped


def test_entry_fills_on_the_book_not_the_leaders_print():
    """The copy lands one poll late, so it pays what the book then shows."""
    res = {"m1": (_mkt("m1"), "tokW")}
    t = _trade("m1", "tokW", Side.BUY, 0.50, 1000, 1, "u1")
    rep = _bt(res).simulate({"0xlead": [t]}, lookback_days=30, now=NOW,
                            book={"tokW": [(_at(t), 0.52)]}, entry_lag_seconds=10.0)
    assert rep.results[0].entry_price == pytest.approx(0.52)


def test_default_lag_is_the_measured_feed_lag_not_the_poll_interval():
    """The feed we poll is minutes behind the leader, so the poll interval is
    not the lag. Every arm of every sweep inherits this default, and at 10s it
    quoted fills 15x closer to the leader than the bot can reach — so pin it.

    Book has one sample the poll-interval model would have taken (0.52, 10s in)
    and one the measured model takes (0.58, 150s in)."""
    res = {"m1": (_mkt("m1"), "tokW")}
    t = _trade("m1", "tokW", Side.BUY, 0.50, 1000, 1, "u1")
    book = {"tokW": [(_at(t) + 10, 0.52), (_at(t) + 150, 0.58)]}
    s = _settings()
    assert s.copy_feed_lag_seconds + s.poll_interval_seconds == 150.0
    rep = _bt(res).simulate({"0xlead": [t]}, lookback_days=30, now=NOW, book=book,
                            max_price_drift=0.0)     # off, so only the lag moves
    assert rep.results[0].entry_price == pytest.approx(0.58)
    # And the old model is still reachable for a deliberate comparison.
    rep10 = _bt(res).simulate({"0xlead": [t]}, lookback_days=30, now=NOW, book=book,
                              max_price_drift=0.0, entry_lag_seconds=10.0)
    assert rep10.results[0].entry_price == pytest.approx(0.52)


def test_no_book_means_no_trade():
    """PaperExecutor refuses a copy buy it has no book for. Falling back to the
    leader's price would grant a perfect fill exactly when we are blind."""
    res = {"m1": (_mkt("m1"), "tokW")}
    t = _trade("m1", "tokW", Side.BUY, 0.50, 1000, 1, "u1")
    stale = _at(t, 5000)                            # past the staleness bound
    rep = _bt(res).simulate({"0xlead": [t]}, lookback_days=30, now=NOW,
                            book={"tokW": [(stale, 0.51)]}, entry_lag_seconds=10.0)
    assert rep.results == []
    assert rep.skipped["no_book"] == 1


def test_price_drift_refuses_an_entry_that_already_moved():
    res = {"m1": (_mkt("m1"), "tokW")}
    t = _trade("m1", "tokW", Side.BUY, 0.50, 1000, 1, "u1")
    moved = {"tokW": [(_at(t), 0.58)]}              # 8c away, budget is 3c
    rep = _bt(res).simulate({"0xlead": [t]}, lookback_days=30, now=NOW,
                            book=moved, entry_lag_seconds=10.0)
    assert rep.results == []
    assert rep.skipped["price_drift"] == 1
    # Widen the budget and the same tape is copied at the drifted price.
    kept = _bt(res).simulate({"0xlead": [t]}, lookback_days=30, now=NOW,
                             book=moved, entry_lag_seconds=10.0, max_price_drift=0.20)
    assert kept.results[0].entry_price == pytest.approx(0.58)


def test_executor_limit_bites_where_the_drift_guard_does_not():
    """Live checks drift on the quote, then refuses the FILL if slippage pushes
    it past leader + budget. A quote sitting just inside the guard therefore
    still gets refused -- which the guard alone would never do."""
    res = {"m1": (_mkt("m1"), "tokW")}
    t = _trade("m1", "tokW", Side.BUY, 0.50, 1000, 1, "u1")
    inside = {"tokW": [(_at(t), 0.529)]}            # 2.9c drift: inside the 3c guard
    s = _settings().model_copy(update={"slippage_bps": 60.0})
    rep = _bt(res).simulate({"0xlead": [t]}, lookback_days=30, now=NOW, settings=s,
                            slippage_bps=60.0, book=inside, entry_lag_seconds=10.0)
    assert rep.results == []                        # 0.529 * 1.006 = 0.5322 > 0.53
    assert "price_drift" not in rep.skipped
    assert rep.skipped["entry_limit"] == 1


def test_exit_without_a_book_rides_to_resolution():
    """No bid to hit means we could not have sold either, so the tranche
    settles at the payout rather than at the leader's exit price."""
    res = {"m1": (_mkt("m1"), "tokW")}
    buy = _trade("m1", "tokW", Side.BUY, 0.50, 1000, 3, "u1")
    sell = _trade("m1", "tokW", Side.SELL, 0.40, 1000, 2, "u2")
    book = {"tokW": [(_at(buy), 0.50)]}             # covers the buy, not the sell
    rep = _bt(res).simulate({"0xlead": [buy, sell]}, lookback_days=30, now=NOW,
                            book=book, entry_lag_seconds=10.0)
    assert rep.skipped["exit_no_book"] == 1
    assert rep.results[0].closed_by == "resolution"


def test_exit_fills_on_the_book_when_there_is_one():
    res = {"m1": (_mkt("m1"), "tokW")}
    buy = _trade("m1", "tokW", Side.BUY, 0.50, 1000, 3, "u1")
    sell = _trade("m1", "tokW", Side.SELL, 0.40, 1000, 2, "u2")
    book = {"tokW": [(_at(buy), 0.50), (_at(sell), 0.60)]}
    rep = _bt(res).simulate({"0xlead": [buy, sell]}, lookback_days=30, now=NOW,
                            book=book, entry_lag_seconds=10.0)
    assert rep.results[0].closed_by == "leader-exit"
    # Sold at the book's 0.60, not at the leader's 0.40.
    assert rep.results[0].pnl == pytest.approx((0.60 - 0.50) * rep.results[0].shares)


def test_candidate_entries_cover_what_the_second_pass_needs_prices_for():
    """Pass 1 must report entries the RiskManager refused too, or pass 2 turns
    the price filters on for trades it has no prices for."""
    # Both settle at NOW, so the first entry still holds its exposure when the
    # second arrives and the per-leader cap has something to bind against.
    res = {"m1": (_mkt("m1", end_days_ago=0.0), "tokW"),
           "m2": (_mkt("m2", end_days_ago=0.0), "tokX")}
    tapes = {"0xlead": [
        _trade("m1", "tokW", Side.BUY, 0.50, 1000, 2, "u1"),
        _trade("m2", "tokX", Side.BUY, 0.50, 1000, 1, "u2"),
    ]}
    s = _settings().model_copy(update={"max_per_leader_usd": 20.0})   # 2nd is refused
    rep = _bt(res).simulate(tapes, lookback_days=30, now=NOW, settings=s)
    assert len(rep.results) == 1
    assert rep.skipped["bankroll_or_caps"] == 1
    assert {tok for tok, _ in rep.candidate_entries} == {"tokW", "tokX"}


def test_attached_book_is_used_by_later_simulates():
    """A sweep fetches once and attaches; every arm must then price off it,
    including helpers that never learned to pass a `book` argument."""
    res = {"m1": (_mkt("m1"), "tokW")}
    t = _trade("m1", "tokW", Side.BUY, 0.50, 1000, 1, "u1")
    bt = _bt(res)
    assert bt.simulate({"0xlead": [t]}, lookback_days=30,
                       now=NOW).results[0].entry_price == pytest.approx(0.50)
    bt.attach_book({"tokW": [(_at(t), 0.52)]})
    rep = bt.simulate({"0xlead": [t]}, lookback_days=30, now=NOW, entry_lag_seconds=10.0)
    assert rep.results[0].entry_price == pytest.approx(0.52)
    # An explicit argument still wins over the attached one.
    rep2 = bt.simulate({"0xlead": [t]}, lookback_days=30, now=NOW,
                       entry_lag_seconds=10.0, book={"tokW": [(_at(t), 0.49)]})
    assert rep2.results[0].entry_price == pytest.approx(0.49)


def test_build_book_discovery_ignores_an_attached_book():
    """Discovery runs at the loosest filters; if it inherited the attached book
    it would drop every trade that book has no quote for, and the fetch would
    then never widen past the gaps it already has."""
    res = {"m1": (_mkt("m1"), "tokW"), "m2": (_mkt("m2"), "tokX")}
    tapes = {"0xlead": [
        _trade("m1", "tokW", Side.BUY, 0.50, 1000, 2, "u1"),
        _trade("m2", "tokX", Side.BUY, 0.50, 1000, 1, "u2"),
    ]}
    bt = _bt(res)
    bt.attach_book({"tokW": [(0, 0.50)]})        # covers one token, badly
    seen = {}

    def fake_cache_fetch(moments, price_cache, *, cache=None, progress=None, **kw):
        seen["tokens"] = {tok for tok, _ in moments}
        return {}

    import pmbot.backtest as bmod
    real, bmod.fetch_book = bmod.fetch_book, fake_cache_fetch
    try:
        bt.build_book(tapes, lookback_days=30, now=NOW)
    finally:
        bmod.fetch_book = real
    assert seen["tokens"] == {"tokW", "tokX"}    # both, not just the quoted one
    assert bt._book == {"tokW": [(0, 0.50)]}     # and the attachment survives


def test_prefetch_markets_warms_the_memo_and_skips_hits():
    """The replay must find prefetched markets already resolved, and a market
    already in the memo must not be refetched."""
    calls = []

    class CountingGamma(FakeGamma):
        def get_market_with_resolution(self, cid):
            calls.append(cid)
            return self._res.get(cid, (None, None))

    res = {c: (_mkt(c), f"tok{c}") for c in ("m1", "m2", "m3")}
    bt = ExactCopyBacktester(data=None, gamma=CountingGamma(res), settings=_settings())
    assert bt.prefetch_markets(["m1", "m2", "m2", "m3", ""]) == 3   # deduped, blanks dropped
    assert sorted(calls) == ["m1", "m2", "m3"]
    calls.clear()
    assert bt.prefetch_markets(["m1", "m2"]) == 0                   # all memoized
    assert calls == []
    tapes = {"0xlead": [_trade("m1", "tokm1", Side.BUY, 0.50, 1000, 1, "u1")]}
    assert len(bt.simulate(tapes, lookback_days=30, now=NOW).results) == 1
    assert calls == []                                              # replay hit the memo


def test_prefetch_does_not_memoize_a_failure():
    """A transient Gamma error must stay unmemoized, or every trade in that
    market is silently dropped for the rest of the run."""
    class AngryGamma(FakeGamma):
        def get_market_with_resolution(self, cid):
            raise RuntimeError("429 rate limited")

    bt = ExactCopyBacktester(data=None, gamma=AngryGamma({}), settings=_settings())
    bt.prefetch_markets(["m1"])
    assert "m1" not in bt._mkt_cache


def test_failed_book_fetch_is_not_cached_as_empty():
    """An empty series means 'no quote', which simulate reads as 'no trade'.
    Caching a fetch ERROR that way would silently delete real trades from every
    later run, so a failure must leave the token absent and retryable."""
    from pmbot.backtest import fetch_book

    class Boom:
        def get_price_history(self, token, **kw):
            raise RuntimeError("429")

    cache: dict = {}
    out = fetch_book([("tokW", NOW)], Boom(), cache=cache, workers=1)
    assert cache == {}                    # nothing written, so next pass retries
    assert out == {"tokW": []}            # this pass has no quote for it

    class Fine:
        def get_price_history(self, token, **kw):
            return []                     # a genuine empty response IS cached

    fetch_book([("tokW", NOW)], Fine(), cache=cache, workers=1)
    assert cache == {"tokW": []}
