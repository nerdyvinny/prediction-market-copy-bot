"""Static roster: the engine copies leaders.yaml and never re-selects.

Replaces the old Engine.rescore()/vetting suite. The selection funnel was
removed from the live loop because it was measured not to predict -- see the
module docstring in pmbot/engine.py. What still has to hold is narrower and
easier to state: we copy exactly what the config lists, a wallet we still hold
positions from but no longer follow becomes exit-only, and the settle sweep
keeps its timing.
"""

from __future__ import annotations

import pmbot.engine as engine_mod
from pmbot.config import Settings
from pmbot.engine import Engine


class FakeStrategy:
    def __init__(self):
        self.leaders = []
        self.exit_only = []
        self.exit_only_leaders = []

    def set_leaders(self, leaders, *, exit_only=None):
        self.leaders = list(leaders)
        self.exit_only = list(exit_only or [])
        self.exit_only_leaders = list(exit_only or [])

    def generate(self):
        return []


class _CountingSettler:
    def __init__(self):
        self.calls = 0

    def settle_open_positions(self):
        self.calls += 1
        return 0


def _engine(roster, strategy=None, **kw):
    s = Settings(db_path=":memory:", poll_interval_seconds=0, **kw)
    return Engine(settings=s, roster=roster, strategy=strategy or FakeStrategy())


def test_engine_copies_exactly_the_configured_roster():
    strat = FakeStrategy()
    eng = _engine(["0xAAA", "0xBBB"], strategy=strat)
    eng.install_roster()
    assert strat.leaders == ["0xaaa", "0xbbb"]
    assert eng.leaders == ["0xaaa", "0xbbb"]
    eng.close()


def test_roster_is_lowercased_and_deduplicated_in_file_order():
    """A hand-edited list will have duplicates and mixed case in it."""
    strat = FakeStrategy()
    eng = _engine(["0xAAA", "0xaaa", "0xBBB", "  0xCCC  ", ""], strategy=strat)
    eng.install_roster()
    assert strat.leaders == ["0xaaa", "0xbbb", "0xccc"]
    eng.close()


def test_held_wallet_off_the_roster_becomes_exit_only():
    """Deleting a line from leaders.yaml must not strand its open positions.

    Their SELLs stay mirrored so the position is still managed; their BUYs are
    ignored so no new money follows a leader we dropped.
    """
    strat = FakeStrategy()
    eng = _engine(["0xkeep"], strategy=strat)
    eng.ledger.leader_exposures = lambda: ["0xDROPPED", "0xkeep"]
    eng.install_roster()
    assert strat.leaders == ["0xkeep"]
    assert strat.exit_only == ["0xdropped"]
    eng.close()


def test_roster_is_persisted_for_the_dashboard():
    eng = _engine(["0xaaa", "0xbbb"])
    eng.install_roster()
    assert set(eng.ledger.followed_leaders()) == {"0xaaa", "0xbbb"}
    eng.close()


def test_empty_roster_copies_nothing_rather_than_falling_back():
    """No lineup floor. An empty list means trade nothing -- never a scramble
    to find replacements, which is what the old funnel did every night."""
    strat = FakeStrategy()
    eng = _engine([], strategy=strat)
    eng.install_roster()
    assert strat.leaders == []
    assert eng.leaders == []
    eng.close()


def test_engine_has_no_selection_machinery_left():
    """Guards the point of the change: nothing may re-select at runtime."""
    eng = _engine(["0xaaa"])
    for gone in ("rescore", "_rescore_tick", "_start_rescore", "_vet_leaders",
                 "_apply_rescore", "_compute_rescore", "_incumbents", "selector"):
        assert not hasattr(eng, gone), gone + " survived the strip"
    eng.close()


def test_roster_installed_once_on_run(monkeypatch):
    strat = FakeStrategy()
    eng = _engine(["0xaaa"], strategy=strat)
    monkeypatch.setattr(engine_mod.time, "sleep", lambda _s: None)
    eng.run(max_cycles=2)
    assert strat.leaders == ["0xaaa"]
    eng.close()


def test_first_settle_sweep_runs_immediately_after_boot(monkeypatch):
    """Regression: the first sweep must not wait out a whole settle interval.

    `_last_settle` was 0.0 and compared against `time.monotonic()`, which is
    seconds since BOOT on Linux. On a freshly rebooted VM `now - 0.0` was less
    than the interval, so every already-resolved position stayed unsettled --
    and its bankroll pinned -- for up to half an hour, the exact thing a
    30-minute settle interval exists to prevent.
    """
    eng = _engine(["0xaaa"], settle_interval_hours=0.5)
    settler = _CountingSettler()
    eng.settler = settler
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: 60.0)
    monkeypatch.setattr(engine_mod.time, "sleep", lambda _s: None)

    eng.run(max_cycles=1)
    assert settler.calls == 1
    eng.close()


def test_settle_sweep_does_not_rerun_inside_the_interval(monkeypatch):
    eng = _engine(["0xaaa"], settle_interval_hours=0.5)
    settler = _CountingSettler()
    eng.settler = settler
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: 60.0)
    monkeypatch.setattr(engine_mod.time, "sleep", lambda _s: None)

    eng.run(max_cycles=3)
    assert settler.calls == 1
    eng.close()
