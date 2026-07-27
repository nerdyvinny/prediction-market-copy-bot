"""Several followed leaders holding the SAME outcome token.

`positions` stores one combined total per token, so sizing a mirror-exit off it
let a leader who contributed 30% of our shares sell 100% of them when they
exited — liquidating another leader's copy while that leader was still in.
The strategy now sizes exits off `Ledger.copied_shares_for_leader`.

Every other strategy test follows exactly one leader, which is why this went
unnoticed: with one leader the combined total IS that leader's slice.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pmbot.models import Fill, LeaderTrade, Market, Side, Signal
from pmbot.portfolio.ledger import Ledger
from pmbot.strategy import ExactCopyStrategy

NOW = datetime.now(timezone.utc)
MKT, TOK = "m1", "tok1"
A, B = "0xaaa", "0xbbb"


def _trade(leader, side, shares, uid, price=0.50):
    return LeaderTrade(leader=leader, market_id=MKT, token_id=TOK, outcome="Yes",
                       side=side, price=price, shares=shares, usd_size=shares * price,
                       timestamp=NOW, tx_hash=uid, event_slug="g")


class _Data:
    """Per-wallet tapes (the shared FakeData elsewhere returns one list)."""

    def __init__(self, by_leader):
        self.by_leader = by_leader

    def get_trades(self, *, user=None, market=None, limit=100):
        return list(self.by_leader.get(user, []))


class _Gamma:
    def get_market(self, condition_id):
        return Market(market_id=MKT, question="q", end_date=None,
                      liquidity_usd=99_999.0, closed=False)


def _copy_buy(led: Ledger, leader: str, shares: float, price: float = 0.50, uid=None):
    """Record a fill as if we'd copied `leader` into TOK."""
    sig = Signal(MKT, TOK, "Yes", Side.BUY, price, shares * price, "copy",
                 source_leader=leader, source_uid=uid or f"{leader}-buy")
    led.record_fill(Fill(signal=sig, fill_price=price, size_usd=shares * price,
                         shares=shares, timestamp=NOW, mode="paper"))


def _strategy(led: Ledger, tapes: dict, leaders: list[str]):
    return ExactCopyStrategy(
        _Data(tapes), _Gamma(), led, leaders=leaders, price_cache=None,
        min_liquidity=0.0, price_min=0.0, price_max=1.0,
        min_leader_notional=0.0, max_trade_age_minutes=0,
    )


def _sells(strategy):
    return [s for s in strategy.generate() if s.side is Side.SELL]


# -- the bug --------------------------------------------------------------
def test_one_leaders_exit_does_not_sell_another_leaders_copy():
    led = Ledger(":memory:")
    try:
        _copy_buy(led, A, 30.0)
        _copy_buy(led, B, 70.0)
        assert led.get_position(TOK).shares == 100.0

        # A opens and fully exits their own position; B does nothing.
        tapes = {
            A: [_trade(A, Side.BUY, 200, "a1"), _trade(A, Side.SELL, 200, "a2")],
            B: [_trade(B, Side.BUY, 200, "b1")],
        }
        sells = _sells(_strategy(led, tapes, [A, B]))
        assert len(sells) == 1
        assert sells[0].source_leader == A
        assert sells[0].size_shares == 30.0          # A's slice, not the 100 total
    finally:
        led.close()


def test_partial_exit_scales_only_the_leaders_own_slice():
    """A trims half of THEIR position -> we trim half of OUR slice from them."""
    led = Ledger(":memory:")
    try:
        _copy_buy(led, A, 30.0)
        _copy_buy(led, B, 70.0)
        tapes = {A: [_trade(A, Side.BUY, 200, "a1"), _trade(A, Side.SELL, 100, "a2")]}
        sells = _sells(_strategy(led, tapes, [A, B]))
        assert len(sells) == 1
        assert sells[0].size_shares == 15.0          # 50% of A's 30, not of 100
        assert "sell 50%" in sells[0].reason
    finally:
        led.close()


def test_no_exit_when_we_hold_the_token_but_not_from_this_leader():
    """We hold TOK only because of B. A exiting is not our trade to mirror."""
    led = Ledger(":memory:")
    try:
        _copy_buy(led, B, 70.0)
        tapes = {A: [_trade(A, Side.BUY, 200, "a1"), _trade(A, Side.SELL, 200, "a2")]}
        assert _sells(_strategy(led, tapes, [A, B])) == []
    finally:
        led.close()


def test_exit_only_leader_still_exits_only_their_own_slice():
    """The exit-only path (dropped leader we still hold from) uses the same
    sizing — it must not liquidate a currently-followed leader's copy."""
    led = Ledger(":memory:")
    try:
        _copy_buy(led, A, 30.0)
        _copy_buy(led, B, 70.0)
        tapes = {A: [_trade(A, Side.BUY, 200, "a1"), _trade(A, Side.SELL, 200, "a2")]}
        strat = _strategy(led, tapes, [B])
        strat.set_leaders([B], exit_only=[A])
        sells = _sells(strat)
        assert len(sells) == 1
        assert sells[0].size_shares == 30.0
        assert "(exit-only)" in sells[0].reason
    finally:
        led.close()


def test_single_leader_full_exit_still_sells_everything():
    """Regression guard: the common one-leader case is unchanged."""
    led = Ledger(":memory:")
    try:
        _copy_buy(led, A, 40.0)
        tapes = {A: [_trade(A, Side.BUY, 200, "a1"), _trade(A, Side.SELL, 200, "a2")]}
        sells = _sells(_strategy(led, tapes, [A]))
        assert len(sells) == 1 and sells[0].size_shares == 40.0
    finally:
        led.close()


def test_stale_attribution_is_clamped_to_what_we_actually_hold():
    """Ledgers written before this fix contain over-exits: one leader's SELL
    booked against shares another leader paid for. That leaves a negative
    balance for the seller and an inflated one for the payer, and the strategy
    must heal rather than emit a negative or oversized exit."""
    led = Ledger(":memory:")
    try:
        _copy_buy(led, A, 50.0, uid="a-buy-1")
        _copy_buy(led, B, 50.0, uid="b-buy-1")
        # Legacy over-exit: B's mirror-sell took the whole 100-share position.
        over = Signal(MKT, TOK, "Yes", Side.SELL, 0.60, 60.0, "legacy",
                      source_leader=B, source_uid="b-oversell")
        led.record_fill(Fill(signal=over, fill_price=0.60, size_usd=60.0,
                             shares=100.0, timestamp=NOW, mode="paper"))
        _copy_buy(led, A, 40.0, uid="a-buy-2")

        assert led.get_position(TOK).shares == 40.0
        assert led.copied_shares_for_leader(A, TOK) == 90.0    # inflated
        assert led.copied_shares_for_leader(B, TOK) == -50.0   # negative

        # B's negative balance must not produce a signal at all.
        b_tape = [_trade(B, Side.BUY, 200, "b1"), _trade(B, Side.SELL, 200, "b2")]
        assert _sells(_strategy(led, {B: b_tape}, [B])) == []
        # A's inflated balance is capped at the 40 shares that actually exist.
        a_tape = [_trade(A, Side.BUY, 200, "a1"), _trade(A, Side.SELL, 200, "a2")]
        sells = _sells(_strategy(led, {A: a_tape}, [A]))
        assert len(sells) == 1 and sells[0].size_shares == 40.0
    finally:
        led.close()


# -- ledger unit ----------------------------------------------------------
def test_copied_shares_for_leader_nets_buys_and_sells():
    led = Ledger(":memory:")
    try:
        _copy_buy(led, A, 30.0, uid="a1")
        _copy_buy(led, B, 70.0, uid="b1")
        assert led.copied_shares_for_leader(A, TOK) == 30.0
        assert led.copied_shares_for_leader(B, TOK) == 70.0

        sig = Signal(MKT, TOK, "Yes", Side.SELL, 0.60, 6.0, "exit",
                     source_leader=A, source_uid="a2")
        led.record_fill(Fill(signal=sig, fill_price=0.60, size_usd=6.0,
                             shares=10.0, timestamp=NOW, mode="paper"))
        assert led.copied_shares_for_leader(A, TOK) == 20.0
        assert led.copied_shares_for_leader(B, TOK) == 70.0     # untouched
    finally:
        led.close()


def test_copied_shares_for_leader_is_zero_for_unknowns():
    led = Ledger(":memory:")
    try:
        _copy_buy(led, A, 30.0)
        assert led.copied_shares_for_leader("0xnobody", TOK) == 0.0
        assert led.copied_shares_for_leader(A, "other-token") == 0.0
        assert led.copied_shares_for_leader("", TOK) == 0.0
    finally:
        led.close()


def test_settlement_fills_are_not_attributed_to_any_leader():
    """Settlement writes a SELL with no source_leader, so it can't reduce a
    leader's balance — the strategy clamps against the (now zero) position."""
    led = Ledger(":memory:")
    try:
        _copy_buy(led, A, 30.0)
        settle = Signal(MKT, TOK, "Yes", Side.SELL, 1.0, 30.0, "settlement")
        led.record_fill(Fill(signal=settle, fill_price=1.0, size_usd=30.0,
                             shares=30.0, timestamp=NOW, mode="paper"))
        assert led.get_position(TOK).shares == 0.0
        assert led.copied_shares_for_leader(A, TOK) == 30.0     # stale by design

        tapes = {A: [_trade(A, Side.BUY, 200, "a1"), _trade(A, Side.SELL, 200, "a2")]}
        assert _sells(_strategy(led, tapes, [A])) == []         # nothing left to exit
    finally:
        led.close()


# -- dust sweep -----------------------------------------------------------
# A leader's exit ratio is rarely exactly 1.0. Seen live: a leader left
# 0.0011% behind, so our mirrored "sell 100%" left 0.000676 shares of 61.36
# (~$0.0005) and the ledger's ABS(shares) > 1e-9 open test counted that
# fully-closed trade as open forever. `sweep_exit_dust` takes the whole slice
# when the leftover is worth less than `exit_dust_usd`.
FULL_EXIT_RATIO = 0.999988976770894      # the exact ratio observed live


def _sweeping(led: Ledger, tapes: dict, leaders: list[str], dust_usd: float = 0.01):
    strat = _strategy(led, tapes, leaders)
    strat.sweep_exit_dust = True
    strat.exit_dust_usd = dust_usd
    return strat


def _near_full_tape(leader):
    """`leader` exits all but a rounding crumb of their own position."""
    return [_trade(leader, Side.BUY, 200, f"{leader}-1"),
            _trade(leader, Side.SELL, 200 * FULL_EXIT_RATIO, f"{leader}-2")]


def test_near_full_exit_leaves_dust_when_the_sweep_is_off():
    """The original bug, reproduced. Sets the flag explicitly rather than
    leaning on the default, which now ships on."""
    led = Ledger(":memory:")
    try:
        _copy_buy(led, A, 30.0)
        strat = _strategy(led, {A: _near_full_tape(A)}, [A])
        strat.sweep_exit_dust = False
        sells = _sells(strat)
        assert len(sells) == 1
        assert sells[0].size_shares < 30.0            # crumb left behind
        assert 30.0 - sells[0].size_shares < 1e-3     # ...and it is only a crumb
        assert "sell 100%" in sells[0].reason         # while the label rounds to 100%
    finally:
        led.close()


def test_sweep_closes_the_crumb_on_a_near_full_exit():
    led = Ledger(":memory:")
    try:
        _copy_buy(led, A, 30.0)
        sells = _sells(_sweeping(led, {A: _near_full_tape(A)}, [A]))
        assert len(sells) == 1
        assert sells[0].size_shares == 30.0           # exactly flat, no residue
    finally:
        led.close()


def test_sweep_never_reaches_another_leaders_slice():
    """The whole point of sizing off the leader's own slice. Sweeping must
    round up to `copied`, never to the combined position."""
    led = Ledger(":memory:")
    try:
        _copy_buy(led, A, 30.0)
        _copy_buy(led, B, 70.0)
        assert led.get_position(TOK).shares == 100.0

        tapes = {A: _near_full_tape(A), B: [_trade(B, Side.BUY, 200, "b1")]}
        sells = _sells(_sweeping(led, tapes, [A, B]))
        assert len(sells) == 1
        assert sells[0].source_leader == A
        assert sells[0].size_shares == 30.0           # A's slice in full...
        assert sells[0].size_shares != 100.0          # ...never B's too
    finally:
        led.close()


def test_sweep_does_not_touch_a_genuine_partial_exit():
    """A real 50% trim is worth far more than the dust threshold, so it must
    stay a 50% trim - the sweep must not promote partials into full exits."""
    led = Ledger(":memory:")
    try:
        _copy_buy(led, A, 30.0)
        tapes = {A: [_trade(A, Side.BUY, 200, "a1"), _trade(A, Side.SELL, 100, "a2")]}
        sells = _sells(_sweeping(led, tapes, [A]))
        assert len(sells) == 1
        assert sells[0].size_shares == 15.0
    finally:
        led.close()
