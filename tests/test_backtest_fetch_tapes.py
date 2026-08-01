"""Pagination tests for ExactCopyBacktester.fetch_tapes().

These pin the two silent-truncation paths that once cut a 3000-trade tape to
998: a short-but-non-empty page being read as end-of-tape, and a transient
fetch error breaking the loop with only a DEBUG line. `_vet_leaders` keeps or
drops live leaders off this tape, so truncation is a wrong trading decision.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.models import LeaderTrade, Side

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _trade(i: int, leader: str = "0xlead") -> LeaderTrade:
    return LeaderTrade(
        leader=leader, market_id=f"c{i}", token_id=f"t{i}", outcome="Yes",
        side=Side.BUY, price=0.5, shares=10.0, usd_size=5.0,
        timestamp=NOW - timedelta(minutes=i), tx_hash=f"uid{i}", event_slug="grp",
    )


class FakeData:
    """Serves `total` trades, optionally with a short page or failures."""

    def __init__(self, total, *, short_page_at=None, fail_at=None, fail_times=0):
        self.total = total
        self.short_page_at = short_page_at   # offset that returns a partial page
        self.fail_at = fail_at               # offset that raises
        self.fail_times = fail_times
        self.failures = 0
        self.calls = []

    def get_trades(self, *, user, limit, offset=0):
        self.calls.append(offset)
        if self.fail_at is not None and offset == self.fail_at and self.failures < self.fail_times:
            self.failures += 1
            raise TimeoutError("simulated transient API failure")
        if offset >= self.total:
            return []
        n = min(limit, self.total - offset)
        if self.short_page_at is not None and offset == self.short_page_at:
            n = max(1, n // 2)               # partial page, NOT end of tape
        return [_trade(offset + k) for k in range(n)]


def _bt(data, limit):
    return ExactCopyBacktester(data, gamma=None, trades_limit=limit)


def test_short_page_does_not_end_pagination():
    """A partial page mid-tape must not be mistaken for the end."""
    data = FakeData(1200, short_page_at=500)
    tapes = _bt(data, 3000).fetch_tapes(["0xLEAD"])
    assert len(tapes["0xlead"]) == 1200


def test_empty_page_ends_pagination():
    data = FakeData(700)
    tapes = _bt(data, 3000).fetch_tapes(["0xLEAD"])
    assert len(tapes["0xlead"]) == 700
    # Stops on the empty page rather than spinning past it.
    assert data.calls[-1] >= 700


def test_trades_limit_is_respected():
    data = FakeData(5000)
    tapes = _bt(data, 1500).fetch_tapes(["0xLEAD"])
    assert len(tapes["0xlead"]) == 1500


def test_transient_failure_is_retried_not_truncated():
    """Two failures at one offset are retried; the tape still completes."""
    data = FakeData(1200, fail_at=500, fail_times=2)
    tapes = _bt(data, 3000).fetch_tapes(["0xLEAD"])
    assert len(tapes["0xlead"]) == 1200
    assert data.failures == 2


def test_persistent_failure_warns_loudly(caplog):
    """Exhausted retries must surface at WARNING, not vanish at DEBUG."""
    data = FakeData(1200, fail_at=500, fail_times=99)
    with caplog.at_level(logging.WARNING, logger="pmbot.backtest"):
        tapes = _bt(data, 3000).fetch_tapes(["0xLEAD"])
    assert len(tapes["0xlead"]) == 500          # truncated, but reported
    assert any("TRUNCATED" in r.message or "TRUNCATED" in r.getMessage()
               for r in caplog.records)


def test_duplicate_only_page_stops_loop():
    """A non-advancing API must not spin forever."""

    class Stuck(FakeData):
        def get_trades(self, *, user, limit, offset=0):
            self.calls.append(offset)
            if len(self.calls) > 50:
                raise AssertionError("fetch_tapes did not stop on duplicate pages")
            return [_trade(k) for k in range(10)]   # same uids every time

    data = Stuck(100)
    tapes = _bt(data, 3000).fetch_tapes(["0xLEAD"])
    assert len(tapes["0xlead"]) == 10
