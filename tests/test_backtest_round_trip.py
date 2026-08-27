"""The backtest's round-trip filter must match what the live loop can see."""

from datetime import datetime, timedelta, timezone

from pmbot.backtest import round_tripped_entry_uids
from pmbot.models import LeaderTrade, Side

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def trade(uid, side, shares, *, offset_s=0.0, token="tok", price=0.5):
    return LeaderTrade(
        leader="0xlead", market_id="mkt", token_id=token, outcome="Yes",
        side=side, price=price, shares=shares, usd_size=shares * price,
        timestamp=T0 + timedelta(seconds=offset_s), tx_hash=uid,
    )


def test_fast_full_exit_is_round_tripped():
    tape = [
        trade("buy1", Side.BUY, 100.0, offset_s=0),
        trade("sell1", Side.SELL, 100.0, offset_s=4),
    ]
    assert round_tripped_entry_uids(tape) == {"buy1"}


def test_slow_full_exit_is_not_round_tripped():
    """We poll every 10s, so an exit 5 minutes later lands after we copied."""
    tape = [
        trade("buy1", Side.BUY, 100.0, offset_s=0),
        trade("sell1", Side.SELL, 100.0, offset_s=300),
    ]
    assert round_tripped_entry_uids(tape) == set()
    # …but a whole-tape scan (the old measurement) does retire it.
    assert round_tripped_entry_uids(
        tape, window_seconds=None, tape_depth=None
    ) == {"buy1"}


def test_partial_exit_keeps_the_entry():
    tape = [
        trade("buy1", Side.BUY, 100.0, offset_s=0),
        trade("sell1", Side.SELL, 40.0, offset_s=2),
    ]
    assert round_tripped_entry_uids(tape) == set()


def test_truncation_crumb_still_counts_as_full():
    """183.314053 shares exit as 183.31; the leftover is a crumb, not a hold."""
    tape = [
        trade("buy1", Side.BUY, 183.314053, offset_s=0),
        trade("sell1", Side.SELL, 183.31, offset_s=3),
    ]
    assert round_tripped_entry_uids(tape) == {"buy1"}


def test_entry_scrolled_off_the_tape_window():
    """Both halves must sit in the same 25-trade fetch to be seen together."""
    tape = [trade("buy1", Side.BUY, 100.0, offset_s=0)]
    tape += [
        trade(f"noise{i}", Side.BUY, 1.0, offset_s=1, token=f"other{i}")
        for i in range(30)
    ]
    tape.append(trade("sell1", Side.SELL, 100.0, offset_s=2))
    assert round_tripped_entry_uids(tape) == set()


def test_sell_predating_the_window_cannot_retire_a_later_entry():
    tape = [
        trade("sell0", Side.SELL, 500.0, offset_s=0),
        trade("buy1", Side.BUY, 100.0, offset_s=1),
    ]
    assert round_tripped_entry_uids(tape) == set()


def test_positions_on_other_tokens_are_independent():
    tape = [
        trade("buyA", Side.BUY, 100.0, offset_s=0, token="A"),
        trade("buyB", Side.BUY, 100.0, offset_s=1, token="B"),
        trade("sellA", Side.SELL, 100.0, offset_s=2, token="A"),
    ]
    assert round_tripped_entry_uids(tape) == {"buyA"}
