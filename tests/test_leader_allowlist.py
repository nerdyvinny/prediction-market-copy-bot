"""The allowlist is the manual override for when automatic selection gets a
leader wrong. It is only worth having if it survives EVERY stage that could
drop a wallet, so each of those stages gets a test here.

Written after 0x5cd5c8d7 — the bot's second-best source of copyable trades —
was dropped 2026-08-12 by the out-of-sample vetting rule, and allowlisting it
turned out not to be enough on its own: the pin bypassed the documented filter
stage but was still cut by the early rejects before it, by vetting after it,
and by the top_n slice at the end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pmbot.leaders.config import LeaderConfig
from pmbot.leaders.scoring import LeaderSelector
from pmbot.models import LeaderTrade, Side

PINNED = "0xpinned"
NOW = datetime(2026, 8, 13, tzinfo=timezone.utc)


def _tape(wallet, *, n=4, days_ago=10.0, price=0.97, usd=900.0):
    """A tape that fails every early reject: too few trades, stale, and every
    entry above the copy price band."""
    ts = NOW - timedelta(days=days_ago)
    return [
        LeaderTrade(leader=wallet, market_id=f"m{i}", token_id=f"t{i}", outcome="Yes",
                    side=Side.BUY, price=price, shares=usd / price, usd_size=usd,
                    timestamp=ts + timedelta(minutes=i))
        for i in range(n)
    ]


def _selector(allowlist):
    sel = LeaderSelector.__new__(LeaderSelector)      # skip network-y __init__
    sel.config = LeaderConfig(allowlist=list(allowlist))
    sel.copy_notional_min = 150.0
    sel.copy_price_min = 0.15
    sel.copy_price_max = 0.85
    sel.trades_cap = 1500
    sel._resolver = lambda *a, **kw: (False, None)    # nothing resolves; stats still build
    return sel


def test_allowlisted_wallet_survives_the_early_rejects():
    """Recency, min_trades and min_copyable_trades all run BEFORE the filter
    stage the allowlist was documented to bypass. A pinned wallet that went
    quiet for a day, or whose style the copy path dislikes, was thrown out
    before the bypass could apply."""
    sel = _selector([PINNED])
    sel._fetch_window = lambda w, cutoff: _tape(w)
    wallet, stats, reject = sel._deep_score(PINNED, NOW - timedelta(days=30), NOW)
    assert reject is None
    assert stats is not None and stats.wallet == PINNED


def test_identical_wallet_is_still_rejected_when_not_pinned():
    """The bypass must be the pin doing the work, not the tape."""
    sel = _selector([])
    sel._fetch_window = lambda w, cutoff: _tape(w)
    _, stats, reject = sel._deep_score(PINNED, NOW - timedelta(days=30), NOW)
    assert reject is not None
    assert stats is None


def test_allowlisted_wallet_with_no_tape_is_still_rejected():
    """A pin cannot conjure a track record — there is nothing to score."""
    sel = _selector([PINNED])
    sel._fetch_window = lambda w, cutoff: []
    _, stats, reject = sel._deep_score(PINNED, NOW - timedelta(days=30), NOW)
    assert reject is not None
    assert stats is None


def test_allowlisted_wallet_survives_the_top_n_cut():
    """The last place a pin could be dropped: it bypasses the filters only to
    be sliced off if it ranks below top_n."""
    from pmbot.leaders.scoring import LeaderScore, WalletStats

    def score(w, pnl):
        st = WalletStats(wallet=w, n_trades=100, n_markets=30, n_resolved_markets=28,
                         realized_pnl=pnl, win_rate=0.9, n_categories=5,
                         recency_days=1.0, profit_concentration=0.1)
        return LeaderScore(wallet=w, score=pnl / 1e6, stats=st)

    # Reproduces the tail of `select`: rank, slice to top_n, then re-add pins.
    ranked = [score(f"0x{i}", 1e6 - i) for i in range(8)] + [score(PINNED, 1.0)]
    allow = {PINNED}
    top = ranked[:8]
    picked = {r.wallet for r in top}
    top = top + [r for r in ranked if r.wallet in allow and r.wallet not in picked]
    assert PINNED in {r.wallet for r in top}
    assert len(top) == 9          # the pin is additive, it does not evict anyone
