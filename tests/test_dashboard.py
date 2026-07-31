"""Dashboard read-model tests: the daily P&L replay and the leader drill-down.

Both features derive numbers the UI presents as fact, so the things worth
pinning are (a) the per-fill realized deltas sum back to what the ledger says,
and (b) the leader panel only ever marks a position "copied" when our own
ledger actually holds that token.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pmbot.dashboard import app as dash
from pmbot.models import Fill, Side, Signal
from pmbot.portfolio.ledger import Ledger

LEADER = "0xlead"
MKT = "mktA"


def _fill(led, *, token, side, price, shares, uid, ts, leader=LEADER, fee=0.01):
    sig = Signal(
        market_id=MKT, token_id=token, outcome="Yes", side=side,
        target_price=price, size_usd=shares * price, reason="test",
        source_leader=leader, source_uid=uid,
    )
    led.record_fill(Fill(
        signal=sig, fill_price=price, size_usd=shares * price, shares=shares,
        timestamp=ts, mode="paper", fee_usd=fee,
    ))


def _read_fills(led) -> list[dict]:
    rows = led.conn.execute(
        "SELECT ts, market_id, token_id, outcome, side, fill_price, size_usd,"
        "       shares, reason, source_leader, venue, fee_usd "
        "FROM fills ORDER BY ts"
    ).fetchall()
    return [dict(r) for r in rows]


@pytest.fixture
def ledger():
    led = Ledger(":memory:")
    yield led
    led.close()


# --- daily P&L replay ------------------------------------------------------

def test_realized_deltas_sum_to_ledger_total(ledger):
    """The calendar's cells must add up to the hero tile's closed P&L."""
    t0 = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    _fill(ledger, token="a", side=Side.BUY, price=0.40, shares=100, uid="1", ts=t0)
    _fill(ledger, token="a", side=Side.SELL, price=0.60, shares=100, uid="2",
          ts=t0 + timedelta(days=1))
    _fill(ledger, token="b", side=Side.BUY, price=0.50, shares=50, uid="3",
          ts=t0 + timedelta(days=2))
    _fill(ledger, token="b", side=Side.SELL, price=0.30, shares=50, uid="4",
          ts=t0 + timedelta(days=2, hours=3))

    fills = _read_fills(ledger)
    dash._annotate_realized(fills)

    summary = ledger.summary()
    assert sum(f["realized_usd"] for f in fills) == pytest.approx(
        summary["realized_pnl"], abs=1e-6)
    assert sum(f["net_realized_usd"] for f in fills) == pytest.approx(
        summary["net_pnl"], abs=1e-6)
    # +$20 on token a, −$10 on token b, minus four 1c fees.
    assert sum(f["net_realized_usd"] for f in fills) == pytest.approx(9.96)


def test_buys_book_no_realized_only_their_fee(ledger):
    """An entry-only day should show the fee drag, not a phantom gain."""
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    _fill(ledger, token="a", side=Side.BUY, price=0.40, shares=100, uid="1", ts=t0)
    fills = _read_fills(ledger)
    dash._annotate_realized(fills)
    assert fills[0]["realized_usd"] == 0.0
    assert fills[0]["net_realized_usd"] == pytest.approx(-0.01)


def test_partial_exit_realizes_only_the_shares_sold(ledger):
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    _fill(ledger, token="a", side=Side.BUY, price=0.40, shares=100, uid="1", ts=t0,
          fee=0.0)
    _fill(ledger, token="a", side=Side.SELL, price=0.60, shares=40, uid="2",
          ts=t0 + timedelta(hours=1), fee=0.0)
    fills = _read_fills(ledger)
    dash._annotate_realized(fills)
    assert fills[1]["realized_usd"] == pytest.approx(8.0)  # 40 * 0.20


# --- leader drill-down -----------------------------------------------------

def test_our_book_tags_the_leader_we_copied_from(ledger):
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    _fill(ledger, token="a", side=Side.BUY, price=0.4, shares=10, uid="1", ts=t0)
    _fill(ledger, token="b", side=Side.BUY, price=0.4, shares=10, uid="2", ts=t0,
          leader="0xother")

    book = dash._our_book_by_token(_read_fills(ledger), LEADER)
    assert book["a"]["from_this_leader"] is True
    assert book["b"]["from_this_leader"] is False   # same market, different source
    assert book["a"]["invested_usd"] == pytest.approx(4.0)
    assert "c" not in book


def test_our_book_nets_buys_and_sells(ledger):
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    _fill(ledger, token="a", side=Side.BUY, price=0.4, shares=10, uid="1", ts=t0)
    _fill(ledger, token="a", side=Side.SELL, price=0.6, shares=10, uid="2",
          ts=t0 + timedelta(hours=1))
    book = dash._our_book_by_token(_read_fills(ledger), LEADER)
    assert book["a"]["invested_usd"] == pytest.approx(4.0)
    assert book["a"]["returned_usd"] == pytest.approx(6.0)


def test_leader_positions_marks_copies_and_rejects_strangers(tmp_path, monkeypatch):
    """End-to-end through the endpoint: only followed wallets, only real copies."""
    from fastapi.testclient import TestClient

    db = str(tmp_path / "t.db")
    led = Ledger(db)
    led.set_followed_leaders({LEADER: 0.9})
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    _fill(led, token="held", side=Side.BUY, price=0.40, shares=100, uid="1", ts=t0)
    _fill(led, token="exited", side=Side.BUY, price=0.50, shares=50, uid="2", ts=t0)
    _fill(led, token="exited", side=Side.SELL, price=0.70, shares=50, uid="3",
          ts=t0 + timedelta(hours=1))
    led.close()

    settings = dash.get_settings()
    monkeypatch.setattr(settings, "db_path", db)

    class FakeData:
        def get_positions(self, wallet, limit=500):
            return [
                {"asset": "held", "conditionId": MKT, "title": "Held one",
                 "outcome": "Yes", "size": 100, "avgPrice": 0.4, "curPrice": 0.55,
                 "currentValue": 55.0, "cashPnl": 15.0, "percentPnl": 37.5},
                {"asset": "exited", "conditionId": MKT, "title": "Exited one",
                 "outcome": "Yes", "size": 50, "avgPrice": 0.5, "curPrice": 0.7,
                 "currentValue": 35.0, "cashPnl": 10.0, "percentPnl": 20.0},
                {"asset": "skipped", "conditionId": MKT, "title": "Never copied",
                 "outcome": "No", "size": 20, "avgPrice": 0.3, "curPrice": 0.1,
                 "currentValue": 2.0, "cashPnl": -4.0, "percentPnl": -66.7},
            ]

    monkeypatch.setattr(dash, "_get_data", lambda: FakeData())
    dash._leader_pos_cache.clear()

    client = TestClient(dash.app)
    assert client.get("/api/leader/0xnotfollowed/positions").status_code == 404

    body = client.get(f"/api/leader/{LEADER}/positions").json()
    by_token = {p["token_id"]: p for p in body["positions"]}
    assert body["copied_count"] == 2
    assert by_token["held"]["our_status"] == "open"
    assert by_token["exited"]["our_status"] == "closed"    # sold out, still theirs
    assert by_token["skipped"]["copied"] is False
    assert by_token["skipped"]["our_status"] is None
    assert body["total_value_usd"] == pytest.approx(92.0)
    # Biggest position first, so the panel leads with what matters.
    assert [p["token_id"] for p in body["positions"]] == ["held", "exited", "skipped"]


def test_leader_positions_serves_stale_book_when_api_falls_over(tmp_path, monkeypatch):
    """A blip in the public API shouldn't blank a panel the user is reading."""
    from fastapi.testclient import TestClient

    db = str(tmp_path / "t.db")
    led = Ledger(db)
    led.set_followed_leaders({LEADER: 0.9})
    led.close()
    monkeypatch.setattr(dash.get_settings(), "db_path", db)

    calls = {"n": 0}

    class FlakyData:
        def get_positions(self, wallet, limit=500):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("boom")
            return [{"asset": "x", "conditionId": MKT, "title": "T",
                     "outcome": "Yes", "size": 1, "avgPrice": 0.5, "curPrice": 0.5,
                     "currentValue": 0.5, "cashPnl": 0.0, "percentPnl": 0.0}]

    monkeypatch.setattr(dash, "_get_data", lambda: FlakyData())
    dash._leader_pos_cache.clear()

    client = TestClient(dash.app)
    first = client.get(f"/api/leader/{LEADER}/positions").json()
    assert first["stale"] is False

    monkeypatch.setattr(dash, "LEADER_POS_TTL", 0.0)  # force a refetch
    second = client.get(f"/api/leader/{LEADER}/positions").json()
    assert second["stale"] is True
    assert len(second["positions"]) == 1

    dash._leader_pos_cache.clear()
    assert client.get(f"/api/leader/{LEADER}/positions").status_code == 502
