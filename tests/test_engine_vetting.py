"""Engine.rescore() leader vetting: drop leaders whose copy-backtest loses."""

from __future__ import annotations

import pmbot.engine as engine_mod
from pmbot.config import Settings
from pmbot.engine import Engine
from pmbot.leaders.scoring import LeaderScore, WalletStats
from pmbot.models import Signal  # noqa: F401  (imported for type parity with engine)


def _score(wallet):
    st = WalletStats(wallet=wallet, n_trades=200, n_markets=30, n_resolved_markets=20,
                     realized_pnl=1000.0, win_rate=0.7, n_categories=5,
                     recency_days=1.0, concentration=0.2)
    return LeaderScore(wallet=wallet, score=0.9, stats=st)


class FakeSelector:
    def __init__(self, wallets):
        self._wallets = wallets

    def select(self, now=None):
        return [_score(w) for w in self._wallets]


class FakeStrategy:
    def __init__(self):
        self.leaders = []

    def set_leaders(self, leaders):
        self.leaders = leaders

    def generate(self):
        return []


class FakeReport:
    def __init__(self, n, pnl):
        self._m = {"n_trades": n, "net_pnl": pnl}

    def metrics(self):
        return self._m


class FakeVetter:
    """Stub for ExactCopyBacktester: per-wallet canned results."""

    outcomes = {}

    def __init__(self, *a, **kw):
        pass

    def run(self, leaders, **kw):
        return FakeReport(*self.outcomes[leaders[0]])


def test_vetting_drops_unprofitable_keeps_profitable_and_unknown(monkeypatch):
    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {
        "0xgood": (25, 40.0),     # profitable copy -> keep
        "0xbad": (25, -30.0),     # loses money when copied -> drop
        "0xthin": (0, 0.0),       # no evidence -> keep
    }
    s = Settings(db_path=":memory:", copy_vet_leaders=True, arb_enabled=False)
    strat = FakeStrategy()
    eng = Engine(settings=s, selector=FakeSelector(["0xgood", "0xbad", "0xthin"]),
                 strategy=strat)
    ranked = eng.rescore()
    assert [r.wallet for r in ranked] == ["0xgood", "0xthin"]
    assert strat.leaders == ["0xgood", "0xthin"]
    eng.close()


def test_vetting_disabled_keeps_everyone(monkeypatch):
    monkeypatch.setattr(engine_mod, "ExactCopyBacktester", FakeVetter)
    FakeVetter.outcomes = {"0xbad": (25, -30.0)}
    s = Settings(db_path=":memory:", copy_vet_leaders=False, arb_enabled=False)
    eng = Engine(settings=s, selector=FakeSelector(["0xbad"]), strategy=FakeStrategy())
    assert [r.wallet for r in eng.rescore()] == ["0xbad"]
    eng.close()
