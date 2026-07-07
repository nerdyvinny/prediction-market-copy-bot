"""Venue-aware ledger + atomic paper execution of arbitrage leg groups."""

from __future__ import annotations

from pmbot.execution.paper_executor import PaperExecutor
from pmbot.models import Quote, Side, Signal, Venue
from pmbot.portfolio.ledger import Ledger
from pmbot.risk import RiskManager


class FakePrices:
    def __init__(self, quotes):
        self._q = quotes

    def get_quote(self, token_id, *, force=False):
        q = self._q.get(token_id)
        if q is None:
            raise RuntimeError("no book")
        return q


def pm_leg(group="g1", size=40.0, price=0.40):
    return Signal(
        market_id="0xmkt", token_id="tok-yes", outcome="Yes", side=Side.BUY,
        target_price=price, size_usd=size, reason="arb test",
        source_uid="arb:0xmkt:Yes:KX-T", venue=Venue.POLYMARKET.value, leg_group=group,
    )


def k_leg(group="g1", size=55.0, price=0.55):
    return Signal(
        market_id="KX-T", token_id="kalshi:KX-T:no", outcome="NO", side=Side.BUY,
        target_price=price, size_usd=size, reason="arb test",
        source_uid="arb:0xmkt:Yes:KX-T", venue=Venue.KALSHI.value, leg_group=group,
    )


class TestPaperGroupExecution:
    def test_both_legs_fill_and_record_venue_and_fee(self):
        led = Ledger(":memory:")
        prices = FakePrices({"tok-yes": Quote("tok-yes", bid=0.39, ask=0.40)})
        ex = PaperExecutor(led, prices, slippage_bps=0.0)
        fills = ex.execute_group([pm_leg(), k_leg()])
        assert fills is not None and len(fills) == 2

        pm_fill = next(f for f in fills if f.signal.venue == Venue.POLYMARKET.value)
        k_fill = next(f for f in fills if f.signal.venue == Venue.KALSHI.value)
        assert pm_fill.fee_usd == 0.0
        # Kalshi leg: 100 contracts @ 0.55 -> fee = ceil(0.07*100*0.55*0.45) = $1.74
        assert abs(k_fill.shares - 100.0) < 1e-6
        assert k_fill.fee_usd == 1.74

        positions = led.get_positions()
        assert {p.venue for p in positions} == {"polymarket", "kalshi"}
        assert led.summary()["fees_usd"] == 1.74
        led.close()

    def test_kalshi_leg_fills_at_scan_price_not_clob(self):
        led = Ledger(":memory:")
        # Price cache knows NOTHING about kalshi token ids -> must not be hit.
        ex = PaperExecutor(led, FakePrices({}), slippage_bps=0.0)
        fill = ex.execute(k_leg(group=None))
        assert fill is not None
        assert fill.fill_price == 0.55
        led.close()

    def test_group_aborts_if_any_leg_unpriceable(self):
        led = Ledger(":memory:")
        # PM quote missing AND target_price=0 -> PM leg cannot price.
        bad_pm = Signal(
            market_id="0xmkt", token_id="tok-yes", outcome="Yes", side=Side.BUY,
            target_price=0.0, size_usd=40.0, reason="arb test",
            venue=Venue.POLYMARKET.value, leg_group="g1",
        )
        ex = PaperExecutor(led, FakePrices({}), slippage_bps=0.0)
        assert ex.execute_group([bad_pm, k_leg()]) is None
        assert led.fill_count() == 0          # neither leg recorded
        led.close()

    def test_ledger_migrates_old_schema(self, tmp_path):
        """A pre-arb database opens cleanly and gains the new columns."""
        import sqlite3

        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                mode TEXT NOT NULL, market_id TEXT NOT NULL, token_id TEXT NOT NULL,
                outcome TEXT NOT NULL, side TEXT NOT NULL, fill_price REAL NOT NULL,
                size_usd REAL NOT NULL, shares REAL NOT NULL,
                slippage_bps REAL NOT NULL DEFAULT 0,
                reason TEXT, source_leader TEXT, source_uid TEXT
            );
            CREATE TABLE positions (
                token_id TEXT PRIMARY KEY, market_id TEXT NOT NULL,
                outcome TEXT NOT NULL, shares REAL NOT NULL DEFAULT 0,
                avg_price REAL NOT NULL DEFAULT 0, realized_pnl REAL NOT NULL DEFAULT 0
            );
            INSERT INTO fills (ts, mode, market_id, token_id, outcome, side,
                               fill_price, size_usd, shares)
            VALUES ('2026-01-01T00:00:00', 'paper', 'm', 't', 'Yes', 'BUY', 0.5, 10, 20);
            """
        )
        conn.commit()
        conn.close()

        led = Ledger(str(db))
        s = led.summary()             # exercises fee_usd column
        assert s["fills"] == 1 and s["fees_usd"] == 0.0
        led.close()


class TestRiskGroupGate:
    def test_approves_within_bankroll_and_caps(self):
        led = Ledger(":memory:")
        rm = RiskManager(led)
        rm.bankroll, rm.max_per_market = 500.0, 100.0
        assert rm.check_group([pm_leg(size=40.0), k_leg(size=55.0)]) is True
        led.close()

    def test_rejects_over_bankroll(self):
        led = Ledger(":memory:")
        rm = RiskManager(led)
        rm.bankroll, rm.max_per_market = 50.0, 100.0
        assert rm.check_group([pm_leg(size=40.0), k_leg(size=55.0)]) is False
        led.close()

    def test_rejects_over_market_cap(self):
        led = Ledger(":memory:")
        rm = RiskManager(led)
        rm.bankroll, rm.max_per_market = 500.0, 50.0
        assert rm.check_group([pm_leg(size=40.0), k_leg(size=55.0)]) is False
        led.close()

    def test_rejects_empty_or_zero(self):
        led = Ledger(":memory:")
        rm = RiskManager(led)
        assert rm.check_group([]) is False
        led.close()
