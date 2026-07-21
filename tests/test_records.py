"""The resolved-market ledger: store roundtrip, harvesting, and the selector
shortlisting a proven winner the current feeds no longer show."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pmbot.data.resolution_cache import ResolutionStore
from pmbot.leaders.config import FilterConfig, LeaderConfig, SelectionConfig
from pmbot.leaders.records import RecordStore, harvest_resolved_records
from pmbot.leaders.scoring import LeaderSelector
from pmbot.models import LeaderTrade, Market, Side

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


# --- store ----------------------------------------------------------------
def test_record_store_roundtrip_and_prune(tmp_path):
    db = str(tmp_path / "rec.db")
    recent = (NOW - timedelta(days=2)).isoformat()
    ancient = (NOW - timedelta(days=200)).isoformat()
    with RecordStore(db) as store:
        store.add_market("m1", recent, {"0xa": (50.0, 2), "0xb": (-10.0, 1)})
        store.add_market("m2", recent, {"0xa": (30.0, 1)})
        store.add_market("m3", ancient, {"0xa": (999.0, 1)})
        assert store.harvested_ids() == {"m1", "m2", "m3"}

    with RecordStore(db) as store:                 # survives reopen
        since = (NOW - timedelta(days=30)).isoformat()
        summ = store.wallet_summaries(since)
        assert summ["0xa"] == (2, 0, pytest.approx(80.0))   # m3 outside window
        assert summ["0xb"] == (0, 1, pytest.approx(-10.0))
        store.prune(keep_days=90.0)
        assert store.harvested_ids() == {"m1", "m2"}


# --- harvesting -----------------------------------------------------------
class HarvestGamma:
    """One decisively resolved market on the closed feed."""

    def __init__(self):
        self.resolved = Market(
            market_id="mR", question="r?", closed=True,
            end_date=NOW - timedelta(days=1),
            tokens={"Yes": "W", "No": "L"},
            outcome_prices={"W": 1.0, "L": 0.0},
        )

    def get_markets(self, *, limit=50, closed=False, active=True, **kw):
        return [self.resolved] if closed else []


class HarvestData:
    def __init__(self):
        ts = int((NOW - timedelta(days=1, hours=2)).timestamp())
        self.feeds = {
            "mR": [
                {"proxyWallet": "0xREC", "asset": "W", "side": "BUY",
                 "price": 0.50, "size": 100, "timestamp": ts},
                {"proxyWallet": "0xdud", "asset": "L", "side": "BUY",
                 "price": 0.50, "size": 100, "timestamp": ts},
            ],
        }

    def get_raw_trades(self, *, market=None, user=None, limit=100, offset=0):
        rows = self.feeds.get(market, [])
        return rows[offset:offset + limit]


def test_harvest_records_wallet_pnl_once(tmp_path):
    store = RecordStore(str(tmp_path / "rec.db"))
    n = harvest_resolved_records(HarvestData(), HarvestGamma(), store)
    assert n == 1
    summ = store.wallet_summaries((NOW - timedelta(days=30)).isoformat())
    assert summ["0xrec"] == (1, 0, pytest.approx(50.0))     # wallets lowercased
    assert summ["0xdud"] == (0, 1, pytest.approx(-50.0))
    # Second harvest: the market is already recorded — nothing new.
    assert harvest_resolved_records(HarvestData(), HarvestGamma(), store) == 0
    store.close()


# --- selector integration -------------------------------------------------
def _tape_good(wallet: str) -> list[LeaderTrade]:
    """60 trades over 30 resolved markets, 27 wins (90%), all recent."""
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


class ChurnedGamma:
    """Feed churn: no closed feeds anymore, an open feed without our wallet;
    still resolves the deep-score markets."""

    def __init__(self):
        self.open = Market(
            market_id="mO", question="o?", closed=False,
            tokens={"Yes": "OY"}, outcome_prices={"OY": 0.6},
        )

    def get_markets(self, *, limit=50, closed=False, active=True, **kw):
        return [] if closed else [self.open]

    def get_resolution(self, condition_id):
        i = int(condition_id[1:])
        return (True, f"T{i}") if i < 27 else (True, "OTHER")


class ChurnedData:
    """0xrec is nowhere in today's feeds — only their tape exists."""

    def __init__(self):
        ts = int(NOW.timestamp())
        self.feeds = {
            "mO": [{"proxyWallet": "0xother", "asset": "OY", "side": "BUY",
                    "price": 0.40, "size": 10, "timestamp": ts - 600 - k}
                   for k in range(3)],
        }
        self.tapes = {"0xrec": _tape_good("0xrec")}

    def get_raw_trades(self, *, market=None, user=None, limit=100, offset=0):
        rows = self.feeds.get(market, [])
        return rows[offset:offset + limit]

    def get_trades(self, *, user=None, market=None, limit=100, offset=0):
        tape = sorted(self.tapes.get(user, []), key=lambda t: t.timestamp, reverse=True)
        return tape[offset:offset + limit]


def test_selector_shortlists_feed_invisible_wallet_from_records(tmp_path):
    """A wallet with accumulated winning records gets deep-scored (and can be
    followed) even when today's feeds don't contain a single trade of theirs —
    the exact failure that made past whale discoveries one-off luck."""
    records = RecordStore(str(tmp_path / "rec.db"))
    records.add_market("mR", (NOW - timedelta(days=1)).isoformat(),
                       {"0xrec": (50.0, 1)})
    store = ResolutionStore(str(tmp_path / "res.db"))
    cfg = LeaderConfig(
        selection=SelectionConfig(top_n=8),
        filters=FilterConfig(),
        weights={"realized_pnl": 0.4, "win_rate": 0.3, "recency": 0.3},
    )
    sel = LeaderSelector(
        ChurnedData(), ChurnedGamma(), config=cfg, resolution_store=store,
        record_store=records, deep_score_limit=10, explore_n=0, max_workers=1,
        copy_notional_min=0.0, copy_price_min=0.0, copy_price_max=1.0,
    )
    top = sel.select(now=NOW)
    assert [r.wallet for r in top] == ["0xrec"]
    assert sel.last_report["record_wallets"] == 1
    records.close()
    store.close()
