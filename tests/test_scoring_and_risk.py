"""Tests for leader scoring math and risk sizing (all offline)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pmbot.config import Settings
from pmbot.leaders.config import FilterConfig
from pmbot.leaders.scoring import (
    LeaderScore,
    WalletStats,
    compute_wallet_stats,
    copyability,
    failing_filters,
    passes_filters,
    pnl_score,
    rank_wallets,
    recency_score,
    seat_roster,
    win_rate_score,
)
from pmbot.models import Fill, LeaderTrade, Side, Signal
from pmbot.portfolio.ledger import Ledger
from pmbot.risk import RiskManager

NOW = datetime(2026, 6, 26, tzinfo=timezone.utc)


def _t(market, token, side, price, shares, days_ago, slug):
    return LeaderTrade(
        leader="0xLEADER",
        market_id=market,
        token_id=token,
        outcome="Yes",
        side=side,
        price=price,
        shares=shares,
        usd_size=shares * price,
        timestamp=NOW - timedelta(days=days_ago),
        tx_hash=f"{market}-{token}-{side.value}-{days_ago}",
        event_slug=slug,
    )


def test_compute_wallet_stats_reconstructs_pnl_and_winrate():
    trades = [
        # Market A: buy and hold; A wins -> +60 realized, counts as a resolved win
        _t("A", "A1", Side.BUY, 0.40, 100, 10, "cat-a"),
        # Market B: buy then sell at a loss; B not resolved -> -10 realized, not a "leg"
        _t("B", "B1", Side.BUY, 0.60, 100, 8, "cat-b"),
        _t("B", "B1", Side.SELL, 0.50, 100, 5, "cat-b"),
    ]
    resolver = {"A": (True, "A1"), "B": (False, None)}.__getitem__

    st = compute_wallet_stats("0xLEADER", trades, resolver, now=NOW,
                              copyable_notional_min=45.0,
                              copyable_price_min=0.15, copyable_price_max=0.85)

    # Copyable BUYs at >=$45 notional in band: B1 buy ($60) yes; A1 buy ($40) no.
    assert st.n_copyable_trades == 1
    assert st.n_trades == 3
    assert st.n_markets == 2
    assert st.n_resolved_markets == 1
    assert st.realized_pnl == pytest.approx(60.0 - 10.0)
    assert st.win_rate == pytest.approx(1.0)        # only resolved market (A) won
    assert st.n_categories == 2
    # Best market's profit (A: +60) over net profit (+50): one winner is
    # propping up losses elsewhere, so concentration exceeds 1.0.
    assert st.profit_concentration == pytest.approx(60.0 / 50.0)
    assert st.recency_days == pytest.approx(5.0, abs=0.01)


def test_compute_wallet_stats_settles_losing_outcome():
    # Buy the losing outcome and hold to resolution -> full loss realized.
    trades = [_t("A", "A2", Side.BUY, 0.30, 100, 3, "cat-a")]
    resolver = {"A": (True, "A1")}.__getitem__   # A1 won, we held A2
    st = compute_wallet_stats("0xL", trades, resolver, now=NOW)
    assert st.realized_pnl == pytest.approx((0.0 - 0.30) * 100)
    assert st.n_resolved_markets == 1
    assert st.win_rate == 0.0


def test_sell_from_zero_is_not_scored_as_short():
    """A SELL with no tracked prior buy is the exit of a PRE-WINDOW position
    (Polymarket has no naked shorts). It must be ignored, not booked as a
    short — the old behavior turned a patient whale's profitable exit on a
    winning market into a fake loss and failed them on win rate."""
    trades = [_t("A", "A1", Side.SELL, 0.90, 100, 2, "cat-a")]
    resolver = {"A": (True, "A1")}.__getitem__   # their token WON
    st = compute_wallet_stats("0xL", trades, resolver, now=NOW)
    assert st.realized_pnl == pytest.approx(0.0)   # was -$10 fake short loss
    assert st.n_resolved_markets == 0              # nothing scoreable in window
    assert st.recency_days == pytest.approx(2.0, abs=0.01)  # still counts as activity


def test_sell_clamped_to_tracked_shares():
    # Bought 40 in-window; sells 100 (60 predate the window). Realize only
    # the tracked 40 and leave no phantom short to settle at resolution.
    trades = [
        _t("A", "A1", Side.BUY, 0.40, 40, 5, "cat-a"),
        _t("A", "A1", Side.SELL, 0.90, 100, 2, "cat-a"),
    ]
    resolver = {"A": (True, "A1")}.__getitem__
    st = compute_wallet_stats("0xL", trades, resolver, now=NOW)
    assert st.realized_pnl == pytest.approx((0.90 - 0.40) * 40)
    assert st.n_resolved_markets == 1
    assert st.win_rate == pytest.approx(1.0)


def test_passes_filters_enforces_new_spec():
    # Defaults: 30d window, ≥50 trades, ≥25 resolved, ≥80% win rate, net-positive,
    # last trade ≤48h, ≤40% of profit from one market, single-market OK.
    base = dict(wallet="w", n_trades=120, n_markets=30, n_resolved_markets=30,
                realized_pnl=500.0, win_rate=0.85, n_categories=1,
                recency_days=1.0, profit_concentration=0.2, n_copyable_trades=20)
    f = FilterConfig()
    assert passes_filters(WalletStats(**base), f) is True

    assert failing_filters(WalletStats(**{**base, "n_trades": 40}), f) == ["min_trades"]
    assert failing_filters(WalletStats(**{**base, "recency_days": 3.0}), f) == ["recency"]
    assert failing_filters(WalletStats(**{**base, "realized_pnl": -1}), f) == ["pnl"]
    assert failing_filters(WalletStats(**{**base, "n_resolved_markets": 10}), f) == ["resolved_markets"]
    assert failing_filters(WalletStats(**{**base, "win_rate": 0.7}), f) == ["win_rate"]
    assert failing_filters(WalletStats(**{**base, "profit_concentration": 0.9}), f) == ["profit_concentration"]
    # The style gate: perfect stats but nothing our copy side would mirror.
    assert failing_filters(WalletStats(**{**base, "n_copyable_trades": 2}), f) == ["copyable_trades"]


def test_win_rate_enforced_even_on_thin_resolved_samples():
    # The old ">=5 resolved markets or win rate is waived" bypass is gone:
    # a 2-0 lucky wallet fails on sample size, not sneaks past on it.
    base = dict(wallet="w", n_trades=120, n_markets=30, n_resolved_markets=2,
                realized_pnl=500.0, win_rate=1.0, n_categories=1,
                recency_days=1.0, profit_concentration=0.2, n_copyable_trades=20)
    fails = failing_filters(WalletStats(**base), FilterConfig())
    assert "resolved_markets" in fails
    zero = WalletStats(**{**base, "win_rate": 0.0})
    assert "win_rate" in failing_filters(zero, FilterConfig())


def test_rank_wallets_orders_by_weighted_score():
    good = WalletStats("good", 200, 30, 28, 1000, 0.90, 5, 1.0, 0.2)
    weak = WalletStats("weak", 110, 12, 25, 50, 0.81, 2, 30.0, 0.35)
    ranked = rank_wallets([weak, good], {"realized_pnl": .40, "win_rate": .30,
                                         "consistency": .00, "recency": .30})
    assert [r.wallet for r in ranked] == ["good", "weak"]
    assert 0.0 <= ranked[-1].score <= 1.0


# --- copyability + incumbency seating ------------------------------------
def _ws(wallet, *, pnl=1000.0, win=0.90, recency=1.0, copyable=0):
    return WalletStats(wallet, 200, 30, 28, pnl, win, 5, recency, 0.2, copyable)


def _score(wallet, score):
    return LeaderScore(wallet=wallet, score=score, stats=_ws(wallet))


def test_copyability_saturates_at_target():
    # Absolute scale, so it does not drift with the rest of the pool.
    assert copyability(0, 40) == 0.0
    assert copyability(20, 40) == pytest.approx(0.5)
    assert copyability(40, 40) == pytest.approx(1.0)
    # 235 copyable trades is a scalper, not 6x better than 40.
    assert copyability(235, 40) == pytest.approx(1.0)
    assert copyability(10, 0) == 0.0          # target off -> term contributes nothing


WEIGHTS = {"realized_pnl": .25, "win_rate": .25, "consistency": .0,
           "recency": .20, "copyability": .30}
FLOOR = 0.80        # leaders.yaml min_win_rate; select() passes it to rank_wallets


def test_copyability_breaks_the_tie_between_equal_wallets():
    ranked = rank_wallets([_ws("thin", copyable=2), _ws("deep", copyable=60)],
                          WEIGHTS, copyable_target=40)
    assert [r.wallet for r in ranked] == ["deep", "thin"]


OLD_WEIGHTS = {"realized_pnl": .40, "win_rate": .30, "consistency": .0, "recency": .30}


def test_copyability_promotes_the_wallet_we_could_actually_copy():
    """Regression on real numbers: the 2026-08-23 lineup as logged.

    0x5cd5c8d7 supplied 25 of the month's 91 buys -- more than any other
    wallet -- yet ranked LAST of 5 under the old weights, behind 0xfc25f141
    which supplied one. Weighting copyability and anchoring win rate to the
    eligibility floor lifts it to 2nd.
    """
    lineup = [                                    # pnl, win rate, copyable
        _ws("0xd487f513", pnl=56358, win=0.98, copyable=22),
        _ws("0xfc25f141", pnl=12089, win=0.98, copyable=24),
        _ws("0x5cd5c8d7", pnl=1515, win=0.95, copyable=77),
        _ws("0xf0ea0711", pnl=12149, win=0.87, copyable=77),
        _ws("0xbb3eaeb8", pnl=6229, win=0.89, copyable=75),
    ]
    before = [r.wallet for r in rank_wallets(lineup, OLD_WEIGHTS)]
    after = [r.wallet for r in rank_wallets(lineup, WEIGHTS, copyable_target=40,
                                            win_rate_floor=FLOOR)]

    assert before.index("0xfc25f141") < before.index("0x5cd5c8d7")
    assert after.index("0x5cd5c8d7") < after.index("0xfc25f141")
    assert after.index("0x5cd5c8d7") == 1


def test_win_rate_score_measures_up_from_the_eligibility_floor():
    assert win_rate_score(0.80, 0.80) == 0.0        # exactly at the bar
    assert win_rate_score(1.00, 0.80) == pytest.approx(1.0)
    assert win_rate_score(0.90, 0.80) == pytest.approx(0.5)
    assert win_rate_score(0.70, 0.80) == 0.0        # below the bar, clamped
    assert win_rate_score(0.90, 0.0) == pytest.approx(0.9)   # floor off -> raw
    assert win_rate_score(0.90, 1.0) == 0.0         # degenerate floor


def test_win_rate_no_longer_flattens_a_wallet_that_just_cleared_the_bar():
    """The old bug: min-max across the SURVIVORS of a >=0.80 filter gave the
    weakest of them a flat 0.0, so a 1-point win-rate gap was worth as much as
    the entire copyability term. Anchored to the floor, 0.82 scores 0.10."""
    assert win_rate_score(0.82, FLOOR) == pytest.approx(0.10)
    lo = _ws("just-passed", pnl=5000.0, win=0.82, copyable=60)
    hi = _ws("stellar", pnl=5000.0, win=0.98, copyable=2)
    ranked = rank_wallets([lo, hi], WEIGHTS, copyable_target=40, win_rate_floor=FLOOR)
    # A far more copyable wallet is no longer buried by a 16-point win-rate gap.
    assert ranked[0].wallet == "just-passed"


def test_pnl_score_is_log_spaced_between_absolute_anchors():
    assert pnl_score(1000, 1000, 50000) == 0.0        # at the floor
    assert pnl_score(500, 1000, 50000) == 0.0         # below it
    assert pnl_score(50000, 1000, 50000) == pytest.approx(1.0)
    assert pnl_score(90000, 1000, 50000) == pytest.approx(1.0)   # clamped
    # log spacing: each 10x step is worth the same, so a whale is better
    # evidence than a mid-pack wallet but not proportionally better.
    step = pnl_score(10000, 100, 100000) - pnl_score(1000, 100, 100000)
    assert step == pytest.approx(pnl_score(100000, 100, 100000)
                                 - pnl_score(10000, 100, 100000))
    assert pnl_score(5000, 0, 0) == 0.0               # disabled anchors
    assert pnl_score(5000, 1000, 500) == 0.0          # target below floor


def test_pnl_falls_back_to_pool_minmax_when_anchors_are_unset():
    """Shipped OFF: the anchored scale measured worse on live outcomes, so the
    default must leave ranking byte-identical to before it existed."""
    pool = [_ws("small", pnl=2000.0, win=0.90, copyable=50),
            _ws("whale", pnl=90000.0, win=0.90, copyable=50)]
    unset = rank_wallets(pool, WEIGHTS, copyable_target=40, win_rate_floor=FLOOR)
    legacy = rank_wallets(pool, WEIGHTS, copyable_target=40, win_rate_floor=FLOOR,
                          pnl_floor_usd=0.0, pnl_target_usd=0.0)
    assert [r.score for r in unset] == [r.score for r in legacy]
    # min-max still gives the pool leader 1.0 and the other 0.0 -- the wart the
    # anchored scale would fix, kept deliberately until there is data to justify it.
    assert unset[0].score - unset[1].score == pytest.approx(WEIGHTS["realized_pnl"])


GATE = 96.0     # leaders.yaml max_hours_since_last_trade


def test_recency_score_is_anchored_to_the_staleness_gate():
    assert recency_score(0.0, GATE) == pytest.approx(1.0)       # just traded
    assert recency_score(2.0, GATE) == pytest.approx(0.5)       # 48h of a 96h gate
    assert recency_score(4.0, GATE) == pytest.approx(0.0)       # at the gate
    assert recency_score(9.0, GATE) == 0.0                      # past it, clamped
    assert recency_score(1.0, 0.0) == 0.0                       # gate off


def test_recency_no_longer_spends_the_full_weight_on_a_few_hours():
    """The old bug: min-max across wallets that have ALL cleared a 96-hour gate
    gave the freshest 1.0 and the least fresh 0.0, so a 19-hour gap between two
    perfectly current wallets was worth the entire recency weight."""
    fresh = _ws("traded-1h", recency=1 / 24)
    older = _ws("traded-20h", recency=20 / 24)
    minmax = rank_wallets([fresh, older], WEIGHTS, copyable_target=40, win_rate_floor=FLOOR)
    anchored = rank_wallets([fresh, older], WEIGHTS, copyable_target=40,
                            win_rate_floor=FLOOR, recency_max_hours=GATE)
    assert abs(minmax[0].score - minmax[1].score) == pytest.approx(WEIGHTS["recency"])
    gap = abs(anchored[0].score - anchored[1].score)
    assert gap == pytest.approx(WEIGHTS["recency"] * 19 / GATE, abs=1e-6)
    assert gap < WEIGHTS["recency"] / 4


def test_recency_term_does_not_drift_when_only_the_pool_changes():
    """Recency was the LAST pool-relative term and the worst drifter: removing
    one stale wallet moved everyone else's score by up to 0.116, several times
    the gap between adjacent leaders in a real lineup."""
    a = _ws("a", recency=2 / 24)
    b = _ws("b", recency=6 / 24)
    stale = _ws("stale", recency=70 / 24)
    kw = dict(copyable_target=40, win_rate_floor=FLOOR, recency_max_hours=GATE)
    withs = {r.wallet: r.score for r in rank_wallets([a, b, stale], WEIGHTS, **kw)}
    without = {r.wallet: r.score for r in rank_wallets([a, b], WEIGHTS, **kw)}
    assert withs["a"] == pytest.approx(without["a"])
    assert withs["b"] == pytest.approx(without["b"])


def test_recency_falls_back_to_pool_minmax_when_the_gate_is_unset():
    pool = [_ws("x", recency=1 / 24), _ws("y", recency=3.0)]
    unset = rank_wallets(pool, WEIGHTS, copyable_target=40, win_rate_floor=FLOOR)
    legacy = rank_wallets(pool, WEIGHTS, copyable_target=40, win_rate_floor=FLOOR,
                          recency_max_hours=0.0)
    assert [r.score for r in unset] == [r.score for r in legacy]


def test_win_rate_term_does_not_drift_when_only_the_pool_changes():
    """Pool-relative terms re-rank a wallet for someone ELSE's change, which is
    a churn source in its own right. The anchored term is absolute, so a wallet
    scores the same whoever it is standing next to."""
    a = _ws("a", pnl=5000.0, win=0.90, copyable=50)
    b = _ws("b", pnl=5000.0, win=0.84, copyable=50)
    newcomer = _ws("c", pnl=5000.0, win=0.99, copyable=50)

    pair = {r.wallet: r.score for r in
            rank_wallets([a, b], WEIGHTS, copyable_target=40, win_rate_floor=FLOOR)}
    trio = {r.wallet: r.score for r in
            rank_wallets([a, b, newcomer], WEIGHTS, copyable_target=40, win_rate_floor=FLOOR)}
    assert pair["a"] == pytest.approx(trio["a"])
    assert pair["b"] == pytest.approx(trio["b"])


def test_copyability_is_ignored_when_unweighted():
    old = {"realized_pnl": .40, "win_rate": .30, "consistency": .0, "recency": .30}
    ranked = rank_wallets([_ws("a", copyable=0), _ws("b", copyable=90)], old)
    assert ranked[0].score == pytest.approx(ranked[1].score)


def test_seat_roster_keeps_an_incumbent_that_was_merely_outranked():
    # 0xc23dc0ec's exact failure: it got no worse, the pool just grew.
    ranked = [_score("new1", .9), _score("new2", .8), _score("inc", .3)]
    seated = seat_roster(ranked, ["inc"], top_n=2, explore_slots=1)
    assert set(r.wallet for r in seated) == {"inc", "new1"}
    assert [r.wallet for r in seated] == ["new1", "inc"]     # returned by score


def test_seat_roster_leaves_slots_contestable():
    # Incumbency may claim only top_n - explore_slots seats; the rest go on
    # rank, so a newcomer can still displace a weak incumbent.
    ranked = [_score("inc1", .9), _score("new", .7), _score("inc2", .5)]
    seated = seat_roster(ranked, ["inc1", "inc2"], top_n=2, explore_slots=1)
    assert [r.wallet for r in seated] == ["inc1", "new"]


def test_seat_roster_does_not_resurrect_an_ineligible_incumbent():
    # A leader that failed the filters never reaches `ranked`, so incumbency
    # cannot save it. Only failing a test loses a seat -- but it does lose it.
    ranked = [_score("new1", .9), _score("new2", .8)]
    seated = seat_roster(ranked, ["dropped"], top_n=2, explore_slots=1)
    assert [r.wallet for r in seated] == ["new1", "new2"]


def test_seat_roster_matches_the_wallet_case_insensitively():
    ranked = [_score("0xAbC", .2), _score("new", .9)]
    seated = seat_roster(ranked, ["0xabc"], top_n=1, explore_slots=0)
    assert [r.wallet for r in seated] == ["0xAbC"]


def test_seat_roster_disabled_is_plain_top_n():
    ranked = [_score("new1", .9), _score("new2", .8), _score("inc", .3)]
    seated = seat_roster(ranked, ["inc"], top_n=2, keep_incumbents=False)
    assert [r.wallet for r in seated] == ["new1", "new2"]


def test_seat_roster_never_exceeds_top_n_or_duplicates():
    ranked = [_score(f"w{i}", 1.0 - i / 10) for i in range(6)]
    seated = seat_roster(ranked, ["w4", "w5"], top_n=3, explore_slots=1)
    assert len(seated) == 3
    assert len({r.wallet for r in seated}) == 3
    assert {"w4", "w5"} <= {r.wallet for r in seated}     # both protected seats


def test_seat_roster_handles_no_incumbents_and_zero_seats():
    ranked = [_score("a", .9), _score("b", .5)]
    assert [r.wallet for r in seat_roster(ranked, [], top_n=2, explore_slots=2)] == ["a", "b"]
    assert seat_roster(ranked, ["a"], top_n=0) == []


# --- risk sizing ---------------------------------------------------------
def _settings(**kw):
    base = dict(bankroll_usd=500.0, copy_fraction=0.05, max_per_market_usd=50.0,
                max_per_leader_usd=150.0)
    base.update(kw)
    return Settings(**base)


def _sig(size_usd, leader="0xLEAD", token="tok", market="mkt"):
    return Signal(market_id=market, token_id=token, outcome="Yes", side=Side.BUY,
                  target_price=0.50, size_usd=size_usd, reason="t",
                  source_leader=leader, source_uid=f"u-{size_usd}")


def test_risk_size_applies_copy_fraction():
    led = Ledger(":memory:")
    rm = RiskManager(led, _settings(), min_ticket_usd=1.0)
    sized = rm.size(_sig(1000))           # 1000 * 0.05 = 50
    assert sized is not None and sized.size_usd == pytest.approx(50.0)
    led.close()


def test_risk_size_respects_market_cap():
    led = Ledger(":memory:")
    # Pre-load a $50 market exposure (100 sh @ 0.50).
    s = _sig(50)
    led.record_fill(Fill(signal=s, fill_price=0.50, size_usd=50, shares=100,
                         timestamp=NOW, mode="paper"))
    rm = RiskManager(led, _settings(), min_ticket_usd=1.0)
    # market cap is 50 and already used -> no room.
    assert rm.size(_sig(1000)) is None
    led.close()


def test_pct_cap_replaces_dollar_cap():
    # 3% of a $500 bankroll = $15, and it wins over the $50 dollar cap.
    s = _settings(max_per_market_pct=0.03)
    assert s.per_market_cap_usd == pytest.approx(15.0)
    led = Ledger(":memory:")
    rm = RiskManager(led, s, min_ticket_usd=1.0)
    # 1000 * 0.05 = 50 desired, but the pct cap clamps it to 15.
    sized = rm.size(_sig(1000))
    assert sized is not None and sized.size_usd == pytest.approx(15.0)
    led.close()


def test_pct_cap_absent_leaves_dollar_cap_in_force():
    # Every sweep script passes an explicit dollar cap and must be unaffected.
    s = _settings()
    assert s.max_per_market_pct is None
    assert s.per_market_cap_usd == pytest.approx(50.0)


def test_pct_cap_scales_with_bankroll():
    assert _settings(bankroll_usd=1000.0,
                     max_per_market_pct=0.03).per_market_cap_usd == pytest.approx(30.0)


def test_pct_cap_accumulates_across_copies_in_one_market():
    # Two copies in the same market share the 3% budget, not one each.
    led = Ledger(":memory:")
    s = _sig(50)
    led.record_fill(Fill(signal=s, fill_price=0.50, size_usd=10, shares=20,
                         timestamp=NOW, mode="paper"))
    rm = RiskManager(led, _settings(max_per_market_pct=0.03), min_ticket_usd=1.0)
    sized = rm.size(_sig(1000))            # $15 cap, $10 already used -> $5 left
    assert sized is not None and sized.size_usd == pytest.approx(5.0)
    led.close()


def test_risk_size_rejects_dust():
    led = Ledger(":memory:")
    rm = RiskManager(led, _settings(), min_ticket_usd=1.0)
    assert rm.size(_sig(10)) is None      # 10 * 0.05 = 0.50 < 1.0
    led.close()


def _sell_sig(size_usd, token="tok", market="mkt"):
    return Signal(market_id=market, token_id=token, outcome="Yes", side=Side.SELL,
                  target_price=0.50, size_usd=size_usd, reason="exit",
                  source_leader="0xLEAD", source_uid=f"s-{size_usd}")


def test_risk_size_sell_is_not_shrunk_by_copy_fraction():
    led = Ledger(":memory:")
    # Position worth $100 (200 sh @ 0.50).
    buy = _sig(100)
    led.record_fill(Fill(signal=buy, fill_price=0.50, size_usd=100, shares=200,
                         timestamp=NOW, mode="paper"))
    rm = RiskManager(led, _settings(), min_ticket_usd=1.0)
    # A full-value exit request should pass through uncut, not *0.05.
    sized = rm.size(_sell_sig(100))
    assert sized is not None and sized.size_usd == pytest.approx(100.0)
    led.close()


def test_risk_size_sell_without_share_count_stays_proportional():
    """A partial dollar exit with no explicit share count must sell the
    matching FRACTION of the position, not all of it.

    The executor trusts size_shares over size_usd, so defaulting to the whole
    holding here would let size_usd say "half" while the fill sold everything —
    the same shape as the phantom-short bug.
    """
    led = Ledger(":memory:")
    buy = _sig(100)
    led.record_fill(Fill(signal=buy, fill_price=0.50, size_usd=100, shares=200,
                         timestamp=NOW, mode="paper"))
    rm = RiskManager(led, _settings(), min_ticket_usd=1.0)
    sized = rm.size(_sell_sig(25))            # a quarter of the $100 basis
    assert sized is not None
    assert sized.size_usd == pytest.approx(25.0)
    assert sized.size_shares == pytest.approx(50.0)   # not 200
    led.close()


def test_risk_size_sell_capped_at_position_value():
    led = Ledger(":memory:")
    buy = _sig(100)
    led.record_fill(Fill(signal=buy, fill_price=0.50, size_usd=100, shares=200,
                         timestamp=NOW, mode="paper"))
    rm = RiskManager(led, _settings(), min_ticket_usd=1.0)
    # Asking to sell more than we hold gets capped to what we actually hold.
    sized = rm.size(_sell_sig(9999))
    assert sized is not None and sized.size_usd == pytest.approx(100.0)
    led.close()


def test_risk_size_sell_clamps_size_shares_to_held():
    led = Ledger(":memory:")
    buy = _sig(100)
    led.record_fill(Fill(signal=buy, fill_price=0.50, size_usd=100, shares=200,
                         timestamp=NOW, mode="paper"))
    rm = RiskManager(led, _settings(), min_ticket_usd=1.0)
    from dataclasses import replace as dc_replace
    sized = rm.size(dc_replace(_sell_sig(100), size_shares=5000.0))
    assert sized is not None and sized.size_shares == pytest.approx(200.0)
    # A USD-only sell (no share intent) defaults to everything we hold.
    sized_full = rm.size(_sell_sig(100))
    assert sized_full.size_shares == pytest.approx(200.0)
    led.close()


def test_risk_size_sell_with_no_position_rejected():
    led = Ledger(":memory:")
    rm = RiskManager(led, _settings(), min_ticket_usd=1.0)
    assert rm.size(_sell_sig(100)) is None
    led.close()


def test_leader_weight_scales_buy_sizing_and_neutral_default():
    led = Ledger(":memory:")
    rm = RiskManager(led, _settings(), min_ticket_usd=1.0)
    rm.set_leader_weights({"0xLEAD": 1.5, "0xhot": 99.0})
    sized = rm.size(_sig(1000))                    # 1000 * 0.05 * 1.5 = 75 -> market cap 50
    assert sized is not None and sized.size_usd == pytest.approx(50.0)
    sized = rm.size(_sig(400))                     # 400 * 0.05 * 1.5 = 30
    assert sized is not None and sized.size_usd == pytest.approx(30.0)
    # Unknown leader -> neutral 1.0; clamp caps stored weights at 2.0.
    sized = rm.size(_sig(400, leader="0xother"))
    assert sized is not None and sized.size_usd == pytest.approx(20.0)
    assert rm.leader_weights["0xhot"] == pytest.approx(2.0)
    led.close()


def test_compounding_redeploys_realized_profits():
    led = Ledger(":memory:")
    # Buy $400 (800 sh @0.50) then sell all @0.75: +$200 realized, 0 deployed.
    buy = _sig(400, token="tokC", market="mC")
    led.record_fill(Fill(signal=buy, fill_price=0.50, size_usd=400, shares=800,
                         timestamp=NOW, mode="paper"))
    sell = Signal("mC", "tokC", "Yes", Side.SELL, 0.75, 600, "exit",
                  source_leader=None, source_uid="s1")
    led.record_fill(Fill(signal=sell, fill_price=0.75, size_usd=600, shares=800,
                         timestamp=NOW, mode="paper"))

    compounding = RiskManager(led, _settings(bankroll_usd=500.0, compound_profits=True))
    fixed = RiskManager(led, _settings(bankroll_usd=500.0, compound_profits=False))
    assert compounding._free_bankroll() == pytest.approx(700.0)   # 500 + 200
    assert fixed._free_bankroll() == pytest.approx(500.0)
    led.close()


def test_compounding_shrinks_after_losses():
    led = Ledger(":memory:")
    buy = _sig(400, token="tokL", market="mL")
    led.record_fill(Fill(signal=buy, fill_price=0.50, size_usd=400, shares=800,
                         timestamp=NOW, mode="paper"))
    sell = Signal("mL", "tokL", "Yes", Side.SELL, 0.25, 200, "exit", source_uid="s2")
    led.record_fill(Fill(signal=sell, fill_price=0.25, size_usd=200, shares=800,
                         timestamp=NOW, mode="paper"))
    rm = RiskManager(led, _settings(bankroll_usd=500.0, compound_profits=True))
    assert rm._free_bankroll() == pytest.approx(300.0)            # 500 - 200
    led.close()


def test_fixed_bankroll_also_shrinks_after_losses():
    """Losses reduce deployable capital even with compounding OFF — a real
    account cannot redeploy money it already lost. (Profits still don't add.)"""
    led = Ledger(":memory:")
    buy = _sig(400, token="tokL", market="mL")
    led.record_fill(Fill(signal=buy, fill_price=0.50, size_usd=400, shares=800,
                         timestamp=NOW, mode="paper"))
    sell = Signal("mL", "tokL", "Yes", Side.SELL, 0.25, 200, "exit", source_uid="s3")
    led.record_fill(Fill(signal=sell, fill_price=0.25, size_usd=200, shares=800,
                         timestamp=NOW, mode="paper"))
    rm = RiskManager(led, _settings(bankroll_usd=500.0, compound_profits=False))
    assert rm._free_bankroll() == pytest.approx(300.0)            # 500 - 200 loss
    led.close()


def test_risk_size_sell_below_entry_floor_is_still_allowed():
    """A small mirrored exit must not be blocked by the ENTRY dust floor.

    The $1 floor exists to stop dust copies dying to slippage. Applied to
    exits it made small slices permanently un-closeable: rejected at DEBUG,
    then re-offered every 10s until the trade scrolled out of the window.
    """
    led = Ledger(":memory:")
    buy = _sig(100)
    led.record_fill(Fill(signal=buy, fill_price=0.50, size_usd=100, shares=200,
                         timestamp=NOW, mode="paper"))
    rm = RiskManager(led, _settings(), min_ticket_usd=1.0)
    sized = rm.size(_sell_sig(0.40))
    assert sized is not None
    assert sized.size_usd == pytest.approx(0.40)
    led.close()


def test_risk_size_sell_below_dust_floor_is_refused():
    """A true crumb is still refused — that is what exit_dust_usd is for."""
    led = Ledger(":memory:")
    buy = _sig(100)
    led.record_fill(Fill(signal=buy, fill_price=0.50, size_usd=100, shares=200,
                         timestamp=NOW, mode="paper"))
    rm = RiskManager(led, _settings(), min_ticket_usd=1.0)
    assert rm.size(_sell_sig(0.001)) is None
    led.close()
