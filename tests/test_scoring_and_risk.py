"""Tests for leader scoring math and risk sizing (all offline)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pmbot.config import Settings
from pmbot.leaders.config import FilterConfig
from pmbot.leaders.scoring import (
    WalletStats,
    compute_wallet_stats,
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

    # copyable_trades_only=False tests the raw P&L reconstruction (buys + sells).
    st = compute_wallet_stats("0xLEADER", trades, resolver, now=NOW,
                              copyable_trades_only=False)

    assert st.n_trades == 3
    assert st.n_markets == 2
    assert st.n_resolved_markets == 1
    assert st.realized_pnl == pytest.approx(60.0 - 10.0)
    assert st.win_rate == pytest.approx(1.0)        # only resolved market (A) won
    assert st.n_categories == 2
    assert st.concentration == pytest.approx(110.0 / 150.0)
    assert st.recency_days == pytest.approx(5.0, abs=0.01)


def test_compute_wallet_stats_copyable_filter_drops_sells_and_extremes():
    # Strategy #4 scoring: only BUY trades within the price band count.
    trades = [
        _t("A", "A1", Side.BUY, 0.40, 100, 10, "cat-a"),    # copyable
        _t("B", "B1", Side.SELL, 0.50, 100, 5, "cat-b"),    # dropped: SELL
        _t("C", "C1", Side.BUY, 0.99, 100, 4, "cat-c"),     # dropped: above band
        _t("D", "D1", Side.BUY, 0.01, 100, 3, "cat-d"),     # dropped: below band
    ]
    resolver = {k: (False, None) for k in "ABCD"}.__getitem__
    st = compute_wallet_stats("0xLEADER", trades, resolver, now=NOW,
                              copyable_trades_only=True, price_min=0.05, price_max=0.95)
    assert st.n_trades == 1          # only the single in-band BUY survives
    assert st.n_categories == 1


def test_compute_wallet_stats_settles_losing_outcome():
    # Buy the losing outcome and hold to resolution -> full loss realized.
    trades = [_t("A", "A2", Side.BUY, 0.30, 100, 3, "cat-a")]
    resolver = {"A": (True, "A1")}.__getitem__   # A1 won, we held A2
    st = compute_wallet_stats("0xL", trades, resolver, now=NOW)
    assert st.realized_pnl == pytest.approx((0.0 - 0.30) * 100)
    assert st.n_resolved_markets == 1
    assert st.win_rate == 0.0


def test_passes_filters_enforces_sample_and_winrate():
    base = dict(wallet="w", n_trades=120, n_markets=20, n_resolved_markets=10,
                realized_pnl=500.0, win_rate=0.6, n_categories=3,
                recency_days=2.0, concentration=0.2)
    # defaults: min_resolved_trades=100, min_resolved_markets=5, min_win_rate=0.55, cats>=2
    f = FilterConfig()
    assert passes_filters(WalletStats(**base), f) is True

    assert passes_filters(WalletStats(**{**base, "n_trades": 50}), f) is False        # sample
    assert passes_filters(WalletStats(**{**base, "realized_pnl": -1}), f) is False     # pnl
    assert passes_filters(WalletStats(**{**base, "n_categories": 1}), f) is False      # breadth
    assert passes_filters(WalletStats(**{**base, "concentration": 0.9}), f) is False   # concentration
    assert passes_filters(WalletStats(**{**base, "win_rate": 0.4}), f) is False        # win rate

    # No resolved track record -> rejected by the min_resolved_markets floor,
    # even with strong P&L (rejects high-volume wallets with zero settled outcomes).
    no_record = WalletStats(**{**base, "n_resolved_markets": 0, "win_rate": 0.0})
    assert passes_filters(no_record, f) is False

    # win-rate is only enforced once >=5 markets resolve: a 4-resolved wallet with
    # a low win rate clears win-rate, but a lower floor is needed to be eligible.
    thin_cfg = FilterConfig(min_resolved_trades=1, min_resolved_markets=1,
                            min_distinct_categories=1)
    thin = WalletStats(**{**base, "n_resolved_markets": 4, "win_rate": 0.0})
    assert passes_filters(thin, thin_cfg) is True


def test_rank_wallets_orders_by_weighted_score():
    good = WalletStats("good", 200, 30, 20, 1000, 0.70, 5, 1.0, 0.2)
    weak = WalletStats("weak", 110, 12, 8, 50, 0.56, 2, 30.0, 0.35)
    ranked = rank_wallets([weak, good], {"realized_pnl": .35, "win_rate": .25,
                                         "consistency": .20, "recency": .20})
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
