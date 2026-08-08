"""Engine.rescore() leader vetting: drop leaders whose copy-backtest loses."""

from __future__ import annotations

import pmbot.engine as engine_mod
from pmbot.config import Settings
from pmbot.engine import Engine
from pmbot.leaders.scoring import LeaderScore, WalletStats
from pmbot.models import Signal  # noqa: F401  (imported for type parity with engine)


def _score(wallet):
    st = WalletStats(wallet=wallet, n_trades=200, n_markets=30, n_resolved_markets=28,
                     realized_pnl=1000.0, win_rate=0.85, n_categories=5,
                     recency_days=1.0, profit_concentration=0.2)
    return LeaderScore(wallet=wallet, score=0.9, stats=st)


class FakeSelector:
    def __init__(self, wallets):
        self._wallets = wallets
        self.seen_incumbents = None

    def select(self, now=None, incumbents=None, **kwargs):
        self.seen_incumbents = incumbents
        return [_score(w) for w in self._wallets]


class FakeStrategy:
    def __init__(self):
        self.leaders = []
        self.exit_only = []

    def set_leaders(self, leaders, *, exit_only=None):
        self.leaders = leaders
        self.exit_only = list(exit_only or [])

    def generate(self):
        return []


class FakeReport:
    def __init__(self, n, pnl):
        self._m = {"n_trades": n, "net_pnl": pnl}

    def metrics(self):
        return self._m


class FakeVetter:
    """Stub for ExactCopyBacktester: canned results per wallet per window.

    The engine calls simulate() twice per wallet — recent window first, then
    the older consistency window — so a call counter picks the answer. Wallets
    absent from `prior` get a clean older record, keeping tests that only care
    about recent behaviour short.
    """

    outcomes = {}                  # wallet -> (n_trades, net_pnl), recent window
    prior = {}                     # wallet -> (n_trades, net_pnl), older window
    DEFAULT_PRIOR = (25, 40.0)

    def __init__(self, *a, **kw):
        self._calls = {}

    def fetch_tapes(self, leaders, **kw):
        return {leaders[0]: []}    # content unused; simulate() is canned

    def simulate(self, tapes, **kw):
        wallet = next(iter(tapes))
        n = self._calls.get(wallet, 0)
        self._calls[wallet] = n + 1
        if n == 0:
            return FakeReport(*self.outcomes[wallet])
        return FakeReport(*self.prior.get(wallet, self.DEFAULT_PRIOR))


def test_vetting_drops_unprofitable_and_unproven(monkeypatch):
    """Absence of evidence is a rejection now, not a free pass."""
    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {
        "0xgood": (25, 40.0),     # profitable copy -> keep
        "0xbad": (25, -30.0),     # loses money when copied -> drop
        "0xthin": (0, 0.0),       # never makes a trade we'd mirror -> drop
    }
    FakeVetter.prior = {}
    s = Settings(db_path=":memory:", copy_vet_leaders=True, arb_enabled=False)
    strat = FakeStrategy()
    eng = Engine(settings=s, selector=FakeSelector(["0xgood", "0xbad", "0xthin"]),
                 strategy=strat)
    ranked = eng.rescore()
    assert [r.wallet for r in ranked] == ["0xgood"]
    assert strat.leaders == ["0xgood"]
    eng.close()


def test_vetting_drops_too_few_trades_to_judge(monkeypatch):
    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {"0xlucky": (3, 90.0)}   # 3 wins is not a track record
    FakeVetter.prior = {}
    s = Settings(db_path=":memory:", copy_vet_leaders=True, arb_enabled=False,
                 copy_vet_min_trades=10)
    eng = Engine(settings=s, selector=FakeSelector(["0xlucky"]), strategy=FakeStrategy())
    assert eng.rescore() == []
    eng.close()


def test_vetting_drops_hot_streak_that_fails_out_of_sample(monkeypatch):
    """Profitable in the scoring window, a loser before it — that's the luck
    case the single-window test could never separate from skill."""
    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {"0xhot": (25, 120.0), "0xreal": (25, 60.0)}
    FakeVetter.prior = {"0xhot": (20, -80.0), "0xreal": (20, 45.0)}
    s = Settings(db_path=":memory:", copy_vet_leaders=True, arb_enabled=False)
    eng = Engine(settings=s, selector=FakeSelector(["0xhot", "0xreal"]),
                 strategy=FakeStrategy())
    assert [r.wallet for r in eng.rescore()] == ["0xreal"]
    eng.close()


def test_vetting_drops_wallet_with_no_history_before_the_window(monkeypatch):
    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {"0xbrandnew": (25, 70.0)}
    FakeVetter.prior = {"0xbrandnew": (0, 0.0)}     # tape starts inside the window
    s = Settings(db_path=":memory:", copy_vet_leaders=True, arb_enabled=False)
    eng = Engine(settings=s, selector=FakeSelector(["0xbrandnew"]),
                 strategy=FakeStrategy())
    assert eng.rescore() == []
    eng.close()


def test_consistency_check_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {"0xhot": (25, 120.0)}
    FakeVetter.prior = {"0xhot": (20, -80.0)}
    s = Settings(db_path=":memory:", copy_vet_leaders=True, arb_enabled=False,
                 copy_vet_require_consistency=False)
    eng = Engine(settings=s, selector=FakeSelector(["0xhot"]), strategy=FakeStrategy())
    assert [r.wallet for r in eng.rescore()] == ["0xhot"]
    eng.close()


def test_lineup_is_not_padded_to_top_n(monkeypatch):
    """The follow list is however many wallets are proven — not a quota."""
    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {f"0xw{i}": (25, 40.0 if i < 3 else -10.0) for i in range(8)}
    FakeVetter.prior = {}
    s = Settings(db_path=":memory:", copy_vet_leaders=True, arb_enabled=False)
    strat = FakeStrategy()
    eng = Engine(settings=s, selector=FakeSelector([f"0xw{i}" for i in range(8)]),
                 strategy=strat)
    assert len(eng.rescore()) == 3
    assert strat.leaders == ["0xw0", "0xw1", "0xw2"]
    eng.close()


def test_vetting_disabled_keeps_everyone(monkeypatch):
    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {"0xbad": (25, -30.0)}
    s = Settings(db_path=":memory:", copy_vet_leaders=False, arb_enabled=False)
    eng = Engine(settings=s, selector=FakeSelector(["0xbad"]), strategy=FakeStrategy())
    assert [r.wallet for r in eng.rescore()] == ["0xbad"]
    eng.close()


def test_rescore_seeds_incumbents_from_open_positions(monkeypatch):
    """Feed churn must never drop a leader we hold copied positions from:
    open-position leaders are passed as incumbents even on a fresh engine."""
    from datetime import datetime, timezone
    from pmbot.models import Fill, Side, Signal

    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {"0xnew": (25, 40.0)}
    s = Settings(db_path=":memory:", copy_vet_leaders=True, arb_enabled=False)
    sel = FakeSelector(["0xnew"])
    eng = Engine(settings=s, selector=sel, strategy=FakeStrategy())
    buy = Signal(market_id="m1", token_id="t1", outcome="Yes", side=Side.BUY,
                 target_price=0.5, size_usd=50, reason="copy",
                 source_leader="0xheld", source_uid="u1")
    eng.ledger.record_fill(Fill(signal=buy, fill_price=0.5, size_usd=50, shares=100,
                                timestamp=datetime.now(timezone.utc), mode="paper"))

    eng.rescore()
    assert sel.seen_incumbents == ["0xheld"]   # from the ledger, engine was fresh

    # Followed leaders join the incumbent set on the next rescore.
    eng.rescore()
    assert sel.seen_incumbents == ["0xheld", "0xnew"]
    eng.close()


def test_rescore_persists_follow_list_across_restarts(monkeypatch, tmp_path):
    """A followed leader with NO copied trades yet must survive a restart:
    the rescore persists the follow list to the ledger, and a fresh engine
    seeds incumbents from it. An empty rescore must NOT wipe the list."""
    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {"0xwhale": (25, 40.0)}
    db = str(tmp_path / "eng.db")

    s = Settings(db_path=db, copy_vet_leaders=True, arb_enabled=False)
    eng = Engine(settings=s, selector=FakeSelector(["0xwhale"]), strategy=FakeStrategy())
    eng.rescore()
    eng.close()

    # Restart: no fills in the ledger, yet the whale is still an incumbent.
    sel = FakeSelector([])                       # feeds lost sight of everyone
    eng2 = Engine(settings=s, selector=sel, strategy=FakeStrategy())
    eng2.rescore()
    assert sel.seen_incumbents == ["0xwhale"]
    eng2.close()

    # The empty rescore above must have kept (not wiped) the persisted list.
    sel3 = FakeSelector([])
    eng3 = Engine(settings=s, selector=sel3, strategy=FakeStrategy())
    eng3.rescore()
    assert sel3.seen_incumbents == ["0xwhale"]
    eng3.close()


def test_empty_rescore_keeps_current_leaders_and_watchlist(monkeypatch):
    """API trouble (or a starved funnel) must not stop copying: an empty
    rescore keeps the current leader set instead of wiping it and thrashing."""
    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {"0xgood": (25, 40.0)}
    s = Settings(db_path=":memory:", copy_vet_leaders=True, arb_enabled=False)
    strat = FakeStrategy()
    eng = Engine(settings=s, selector=FakeSelector(["0xgood"]), strategy=strat)
    eng.rescore()
    assert [r.wallet for r in eng.leaders] == ["0xgood"]

    eng.selector = FakeSelector([])              # feeds lose everyone
    eng.rescore()
    assert [r.wallet for r in eng.leaders] == ["0xgood"]   # kept, not wiped
    assert strat.leaders == ["0xgood"]                     # watchlist intact
    eng.close()


def test_rescore_passes_dropped_leaders_with_open_positions_as_exit_only(monkeypatch):
    """A leader missing from the ranked list while we still hold their copied
    positions goes to the strategy as exit-only (SELL mirroring continues)."""
    from datetime import datetime, timezone
    from pmbot.models import Fill, Side, Signal

    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {"0xnew": (25, 40.0)}
    s = Settings(db_path=":memory:", copy_vet_leaders=True, arb_enabled=False)
    strat = FakeStrategy()
    eng = Engine(settings=s, selector=FakeSelector(["0xnew"]), strategy=strat)
    buy = Signal(market_id="m1", token_id="t1", outcome="Yes", side=Side.BUY,
                 target_price=0.5, size_usd=50, reason="copy",
                 source_leader="0xheld", source_uid="u1")
    eng.ledger.record_fill(Fill(signal=buy, fill_price=0.5, size_usd=50, shares=100,
                                timestamp=datetime.now(timezone.utc), mode="paper"))

    eng.rescore()
    assert strat.leaders == ["0xnew"]
    assert strat.exit_only == ["0xheld"]
    eng.close()


# --- settle scheduling ----------------------------------------------------
class _CountingSettler:
    def __init__(self):
        self.calls = 0

    def settle_open_positions(self):
        self.calls += 1
        return 0


def test_first_settle_sweep_runs_immediately_after_boot(monkeypatch):
    """Regression: the first sweep must not wait out a whole settle interval.

    `_last_settle` was 0.0 and compared against `time.monotonic()`, which is
    seconds since BOOT on Linux. On a freshly rebooted VM `now - 0.0` was less
    than the interval, so every already-resolved position stayed unsettled —
    and its bankroll pinned — for up to half an hour, the exact thing a
    30-minute settle interval exists to prevent.
    """
    s = Settings(db_path=":memory:", copy_vet_leaders=False, arb_enabled=False,
                 settle_interval_hours=0.5, poll_interval_seconds=0)
    eng = Engine(settings=s, selector=FakeSelector(["0xgood"]), strategy=FakeStrategy())
    settler = _CountingSettler()
    eng.settler = settler
    # Simulate a machine that booted 60s ago: monotonic() is small.
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: 60.0)
    monkeypatch.setattr(engine_mod.time, "sleep", lambda _s: None)

    eng.run(max_cycles=1)
    assert settler.calls == 1
    eng.close()


def test_settle_sweep_does_not_rerun_inside_the_interval(monkeypatch):
    """...and having run, it waits the full interval before the next one."""
    s = Settings(db_path=":memory:", copy_vet_leaders=False, arb_enabled=False,
                 settle_interval_hours=0.5, poll_interval_seconds=0)
    eng = Engine(settings=s, selector=FakeSelector(["0xgood"]), strategy=FakeStrategy())
    settler = _CountingSettler()
    eng.settler = settler
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: 60.0)
    monkeypatch.setattr(engine_mod.time, "sleep", lambda _s: None)

    eng.run(max_cycles=3)
    assert settler.calls == 1        # once, not once per cycle
    eng.close()
