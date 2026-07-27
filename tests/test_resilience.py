"""Failure-path hardening: retry deadlines, settlement isolation, DB pragmas.

Every test here pins behavior that only shows up when something breaks — the
paths that are invisible in a healthy run and so drift without notice.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pmbot.db import BUSY_TIMEOUT_MS, connect
from pmbot.models import Fill, Market, Side, Signal
from pmbot.portfolio.ledger import Ledger
from pmbot.portfolio.settlement import Settler
from pmbot.strategy.longterm_copy import LongTermCopyStrategy

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


# -- SQLite tuning (pmbot.db) --------------------------------------------
def test_file_backed_ledger_uses_wal_and_busy_timeout(tmp_path):
    """The loop writes while the dashboard reads the same file. Without WAL
    those block each other; without a real busy timeout the loser raises
    'database is locked' mid-cycle."""
    led = Ledger(str(tmp_path / "pmbot.db"))
    try:
        assert led.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert led.conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
    finally:
        led.close()


def test_memory_ledger_still_works(tmp_path):
    """WAL is a no-op on :memory: (backtests + most tests use it). It must
    degrade quietly rather than raise."""
    conn = connect(":memory:")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
    conn.close()


def test_two_connections_can_read_and_write_concurrently(tmp_path):
    """The dashboard's open read cursor must not block the loop's fill."""
    path = str(tmp_path / "pmbot.db")
    writer, reader = Ledger(path), Ledger(path)
    try:
        reader.conn.execute("SELECT * FROM fills").fetchall()   # dashboard-ish read
        sig = Signal("m1", "tok1", "Yes", Side.BUY, 0.5, 10.0, "test")
        writer.record_fill(Fill(signal=sig, fill_price=0.5, size_usd=10.0,
                                shares=20.0, timestamp=NOW, mode="paper"))
        assert reader.fill_count() == 1
    finally:
        writer.close()
        reader.close()


# -- settlement isolation -------------------------------------------------
class _Resolved:
    def get_resolution(self, market_id):
        return (True, f"win-{market_id}")


class _FlakyLedger:
    """Wraps a real ledger and fails the Nth record_fill."""

    def __init__(self, led: Ledger, fail_on_token: str):
        self._led = led
        self._fail_on = fail_on_token

    def get_positions(self, **kw):
        return self._led.get_positions(**kw)

    def record_fill(self, fill: Fill):
        if fill.signal.token_id == self._fail_on:
            raise RuntimeError("database is locked")
        return self._led.record_fill(fill)


def test_settlement_write_failure_does_not_abort_the_sweep():
    """One failed ledger write used to abort settlement for every position
    after it, pinning their bankroll until the next interval."""
    led = Ledger(":memory:")
    try:
        for i in range(3):
            sig = Signal(f"m{i}", f"win-m{i}", "Yes", Side.BUY, 0.5, 10.0, "test")
            led.record_fill(Fill(signal=sig, fill_price=0.5, size_usd=10.0,
                                 shares=20.0, timestamp=NOW, mode="paper"))
        # The middle position's write blows up; the other two must still settle.
        settler = Settler(_FlakyLedger(led, "win-m1"), _Resolved())
        assert settler.settle_open_positions() == 2
        open_tokens = {p.token_id for p in led.get_positions()}
        assert open_tokens == {"win-m1"}          # only the failed one is left
    finally:
        led.close()


# -- retry deadlines ------------------------------------------------------
class _Boom:
    """Selector whose result the engine cannot adopt."""

    last_report: dict = {}

    def select(self, incumbents=None):
        return [object()]                          # not a LeaderScore -> apply blows up


def _engine_with_broken_rescore(tmp_path):
    from pmbot.config import get_settings
    from pmbot.engine import Engine

    s = get_settings().model_copy(update={"db_path": str(tmp_path / "pmbot.db")})
    return Engine(settings=s, selector=_Boom())


def test_rescore_deadline_is_armed_even_when_applying_the_result_throws(tmp_path):
    """An un-advanced deadline restarts a ~15-minute network-heavy rescore on
    every 10s cycle instead of backing off once."""
    import time

    eng = _engine_with_broken_rescore(tmp_path)
    try:
        eng._rescore_result = [object()]           # unusable payload
        eng._rescore_thread = _DeadThread()
        before = time.monotonic()
        try:
            eng._rescore_tick()
        except Exception:
            pass                                   # the throw is expected
        assert eng._next_rescore >= before + eng._rescore_retry_s - 1
    finally:
        eng.close()


class _DeadThread:
    def is_alive(self) -> bool:
        return False


def test_settle_deadline_is_armed_even_when_the_sweep_throws(tmp_path):
    """Same shape as the rescore deadline: a throwing sweep must not retry
    every cycle."""
    import time

    from pmbot.config import get_settings
    from pmbot.engine import Engine

    class _ExplodingSettler:
        calls = 0

        def settle_open_positions(self):
            _ExplodingSettler.calls += 1
            raise RuntimeError("database is locked")

    s = get_settings().model_copy(update={
        "db_path": str(tmp_path / "pmbot.db"),
        "poll_interval_seconds": 0.0,              # don't sleep through the test
    })
    eng = Engine(settings=s, selector=_Boom())
    try:
        eng.leaders = [object()]                   # skip the first-rescore branch
        eng.settler = _ExplodingSettler()
        eng._next_rescore = time.monotonic() + 10_000   # keep rescore out of the way
        eng.run(max_cycles=3)
        # Without the fix this fires once per cycle; with it, only the first.
        assert _ExplodingSettler.calls == 1
    finally:
        eng.close()


# -- cached-error regression (LongTermCopyStrategy) -----------------------
class _FlakyGamma:
    def __init__(self, markets, fail_n=1):
        self._markets = markets
        self.calls = 0
        self.fail_n = fail_n

    def get_market(self, condition_id):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise RuntimeError("rate limited")
        return self._markets.get(condition_id)


class _FakeData:
    def __init__(self, trades):
        self._trades = trades

    def get_trades(self, *, user=None, market=None, limit=100):
        return list(self._trades)


def test_longterm_does_not_cache_market_lookup_errors():
    """A rate-limited Gamma call must not poison the cache: caching it dropped
    every trade in that market for the whole TTL window. (Same fix already in
    ExactCopyStrategy, LeaderSelector and both backtesters.)"""
    from datetime import timedelta

    from pmbot.models import LeaderTrade

    far = datetime.now(timezone.utc) + timedelta(days=60)
    markets = {"m1": Market(market_id="m1", question="q", end_date=far,
                            liquidity_usd=99_999.0, closed=False)}
    trade = LeaderTrade(leader="0xlead", market_id="m1", token_id="tok1",
                        outcome="Yes", side=Side.BUY, price=0.5, shares=100,
                        usd_size=50.0, timestamp=datetime.now(timezone.utc),
                        tx_hash="u1", event_slug="g")
    led = Ledger(":memory:")
    try:
        strat = LongTermCopyStrategy(
            _FakeData([trade]), _FlakyGamma(markets), led, leaders=["0xlead"],
            min_liquidity=5000, price_min=0.05, price_max=0.95,
        )
        assert list(strat.generate()) == []        # cycle 1: lookup failed
        sigs = list(strat.generate())              # cycle 2: fresh lookup works
        assert len(sigs) == 1 and sigs[0].side is Side.BUY
    finally:
        led.close()
