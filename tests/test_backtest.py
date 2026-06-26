"""Offline tests for backtest metrics + drawdown (pure functions)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pmbot.backtest import BacktestReport, CopyResult, max_drawdown

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_max_drawdown():
    assert max_drawdown([]) == 0.0
    assert max_drawdown([1, 2, 3]) == 0.0          # monotonic up
    assert max_drawdown([0, -5, -3, -10, 2]) == pytest.approx(10.0)
    assert max_drawdown([5, 3, 8, 4]) == pytest.approx(4.0)


def _r(leader, size, pnl, won, day):
    return CopyResult(
        leader=leader, market_id="m", outcome="Yes",
        entry_ts=T0, resolve_ts=T0 + timedelta(days=day),
        entry_price=0.5, size_usd=size, shares=size / 0.5, won=won, pnl=pnl,
    )


def test_report_metrics():
    report = BacktestReport(
        results=[
            _r("L1", 50, +10, True, 1),
            _r("L1", 50, -50, False, 2),
            _r("L2", 25, +25, True, 3),
        ],
        starting_bankroll=500.0,
    )
    m = report.metrics()
    assert m["n_trades"] == 3
    assert m["invested"] == pytest.approx(125.0)
    assert m["net_pnl"] == pytest.approx(-15.0)
    assert m["roi"] == pytest.approx(-15.0 / 125.0)
    assert m["win_rate"] == pytest.approx(2 / 3)
    assert m["avg_edge"] == pytest.approx((0.2 - 1.0 + 1.0) / 3)
    # cumulative by resolve_ts: [10, -40, -15] -> peak 10, trough -40 -> dd 50
    assert m["max_drawdown"] == pytest.approx(50.0)
    assert m["by_leader"]["L1"] == (2, pytest.approx(-40.0))
    assert m["by_leader"]["L2"] == (1, pytest.approx(25.0))


def test_report_empty():
    report = BacktestReport(results=[], starting_bankroll=500.0)
    assert report.metrics()["n_trades"] == 0
    assert "0 copyable" in report.summary_text()
