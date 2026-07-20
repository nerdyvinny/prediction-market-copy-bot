"""Offline tests for the selection funnel: feed profiling, the persistent
resolution store, and LeaderSelector's shortlist -> deep-score -> report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pmbot.data.resolution_cache import ResolutionStore
from pmbot.leaders.config import FilterConfig, LeaderConfig, SelectionConfig
from pmbot.leaders.discovery import FeedProfile, profile_candidates
from pmbot.leaders.scoring import LeaderSelector
from pmbot.models import LeaderTrade, Market, Side

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


# --- feed profiles -------------------------------------------------------
def test_feed_profile_quality_rewards_sample_size():
    proven = FeedProfile("a", resolved_wins=20, resolved_losses=2)
    lucky = FeedProfile("b", resolved_wins=2, resolved_losses=0)
    fresh = FeedProfile("c")
    assert proven.quality > lucky.quality > fresh.quality
    assert fresh.quality == pytest.approx(0.5)      # no data -> neutral


class FeedGamma:
    """One decisively resolved market + one open market with live prices."""

    def __init__(self):
        self.closed = Market(
            market_id="mC", question="closed?", closed=True,
            end_date=NOW - timedelta(days=1),
            tokens={"Yes": "W", "No": "L"},
            outcome_prices={"W": 1.0, "L": 0.0},
        )
        self.open = Market(
            market_id="mO", question="open?", closed=False,
            tokens={"Yes": "OY", "No": "ON"},
            outcome_prices={"OY": 0.70, "ON": 0.30},
        )

    def get_markets(self, *, limit=50, closed=False, active=True, **kw):
        return [self.closed] if closed else [self.open]


class FeedData:
    """Raw market feeds: winner-buyer, loser-buyer, and an open-market rider."""

    def __init__(self):
        ts = int(NOW.timestamp())
        self.feeds = {
            "mC": [
                {"proxyWallet": "0xwin", "asset": "W", "side": "BUY",
                 "price": 0.50, "size": 100, "timestamp": ts - 3600},
                {"proxyWallet": "0xlose", "asset": "L", "side": "BUY",
                 "price": 0.50, "size": 100, "timestamp": ts - 3600},
            ],
            "mO": [
                {"proxyWallet": "0xpaper", "asset": "OY", "side": "BUY",
                 "price": 0.40, "size": 50, "timestamp": ts - 600},
            ],
        }

    def get_raw_trades(self, *, market=None, user=None, limit=100, offset=0):
        rows = self.feeds.get(market, [])
        return rows[offset:offset + limit]


def test_profile_candidates_estimates_win_quality():
    profiles = profile_candidates(FeedData(), FeedGamma(), top_open_markets=5,
                                  top_closed_markets=5, per_market_trades=100)
    assert set(profiles) == {"0xwin", "0xlose", "0xpaper"}
    win, lose, paper = profiles["0xwin"], profiles["0xlose"], profiles["0xpaper"]
    assert win.resolved_wins == 1 and win.resolved_pnl == pytest.approx(50.0)
    assert lose.resolved_losses == 1 and lose.resolved_pnl == pytest.approx(-50.0)
    assert paper.mtm_wins == 1 and paper.mtm_pnl == pytest.approx((0.70 - 0.40) * 50)
    assert win.quality > paper.quality > lose.quality


# --- resolution store ----------------------------------------------------
def test_resolution_store_roundtrip(tmp_path):
    db = str(tmp_path / "res.db")
    with ResolutionStore(db) as store:
        assert store.load() == {}
        store.save_many({"c1": "tok1"})
        store.save_many({"c1": "tok1", "c2": "tok2"})   # idempotent re-save
    with ResolutionStore(db) as store:                   # survives reopen
        assert store.load() == {"c1": (True, "tok1"), "c2": (True, "tok2")}


# --- the funnel end to end (offline) -------------------------------------
def _tape_good(wallet: str) -> list[LeaderTrade]:
    """60 trades over 30 resolved markets, 27 wins / 3 losses (90% win rate)."""
    trades = []
    for i in range(30):
        for j in range(2):
            trades.append(LeaderTrade(
                leader=wallet, market_id=f"M{i}", token_id=f"T{i}", outcome="Yes",
                side=Side.BUY, price=0.50, shares=5, usd_size=2.5,
                timestamp=NOW - timedelta(hours=1 + (i * 2 + j) % 40),
                tx_hash=f"{wallet}-{i}-{j}", event_slug=f"ev{i}",
            ))
    return trades


def _tape_stale(wallet: str) -> list[LeaderTrade]:
    """Plenty of trades, but the newest is 10 days old (dead account)."""
    return [LeaderTrade(
        leader=wallet, market_id=f"M{i}", token_id=f"T{i}", outcome="Yes",
        side=Side.BUY, price=0.50, shares=5, usd_size=2.5,
        timestamp=NOW - timedelta(days=10, hours=i % 48),
        tx_hash=f"{wallet}-{i}", event_slug=f"ev{i}",
    ) for i in range(60)]


class FunnelData(FeedData):
    """Feeds put all three wallets in the pool; tapes drive deep scoring."""

    def __init__(self):
        super().__init__()
        ts = int(NOW.timestamp())
        # Everyone appears >= feed_min_trades times in the open-market feed.
        self.feeds["mO"] = [
            {"proxyWallet": w, "asset": "OY", "side": "BUY",
             "price": 0.40, "size": 10, "timestamp": ts - 600 - k}
            for w in ("0xgood", "0xdead", "0xquiet") for k in range(3)
        ]
        self.tapes = {
            "0xgood": _tape_good("0xgood"),
            "0xdead": _tape_stale("0xdead"),
            "0xquiet": _tape_good("0xquiet")[:5],   # too few trades
            "0xinc": _tape_good("0xinc"),           # incumbent, absent from feeds
        }

    def get_trades(self, *, user=None, market=None, limit=100, offset=0):
        tape = sorted(self.tapes.get(user, []), key=lambda t: t.timestamp, reverse=True)
        return tape[offset:offset + limit]


class FunnelGamma(FeedGamma):
    def get_resolution(self, condition_id):
        i = int(condition_id[1:])
        if i < 27:
            return (True, f"T{i}")       # our token won
        return (True, "OTHER")           # resolved against us


def _config():
    return LeaderConfig(
        selection=SelectionConfig(top_n=8),
        filters=FilterConfig(),          # the real new defaults
        weights={"realized_pnl": 0.4, "win_rate": 0.3, "recency": 0.3},
    )


def test_selector_funnel_filters_and_reports(tmp_path):
    store = ResolutionStore(str(tmp_path / "res.db"))
    sel = LeaderSelector(
        FunnelData(), FunnelGamma(), config=_config(), resolution_store=store,
        deep_score_limit=10, explore_n=0, max_workers=1,
        copy_notional_min=0.0, copy_price_min=0.0, copy_price_max=1.0,
    )
    top = sel.select(now=NOW, incumbents=["0xinc"])

    assert sorted(r.wallet for r in top) == ["0xgood", "0xinc"]
    for r in top:
        assert r.stats.win_rate == pytest.approx(0.9)
        assert r.stats.n_resolved_markets == 30

    rep = sel.last_report
    # Pool = all 5 feed wallets (incl. the 2 from the closed-market feed with
    # too few feed trades to shortlist). 0xinc never appeared in any feed...
    assert rep["pool"] == 5
    assert rep["deep_scored"] == 4               # ...but incumbents are always scored
    assert rep["early_rejects"] == {"recency": 1, "min_trades": 1}
    assert rep["eligible"] == 2 and rep["followed"] == 2

    # Terminal resolutions were persisted for the next rescore — including
    # markets that resolved against us (the winner is just another token).
    cached = store.load()
    assert cached["M0"] == (True, "T0")
    assert cached["M29"] == (True, "OTHER")
    assert len(cached) == 30
    store.close()


def test_selector_uses_cached_resolutions_without_refetching(tmp_path):
    store = ResolutionStore(str(tmp_path / "res.db"))
    store.save_many({f"M{i}": f"T{i}" for i in range(27)})

    class CountingGamma(FunnelGamma):
        calls = 0

        def get_resolution(self, condition_id):
            CountingGamma.calls += 1
            return super().get_resolution(condition_id)

    sel = LeaderSelector(
        FunnelData(), CountingGamma(), config=_config(), resolution_store=store,
        deep_score_limit=10, explore_n=0, max_workers=1,
        copy_notional_min=0.0, copy_price_min=0.0, copy_price_max=1.0,
    )
    top = sel.select(now=NOW)
    assert any(r.wallet == "0xgood" for r in top)
    # Only the 3 losing markets (never terminal-with-winner... they resolved
    # with token "OTHER", cached too after first sight) were fetched live.
    assert CountingGamma.calls == 3
    store.close()
