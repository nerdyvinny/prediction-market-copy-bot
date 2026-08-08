"""Tests for leader scoring math and risk sizing (all offline)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pmbot.config import Settings
from pmbot.leaders.config import FilterConfig
from pmbot.leaders.scoring import (
    WalletStats,
    compute_wallet_stats,
    failing_filters,
    passes_filters,
    rank_wallets,
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
