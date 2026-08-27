"""Settle resolved paper positions so realized P&L reflects reality.

Positions are held to resolution (both the long-term copy strategy and arb
pairs), but the ledger only realizes P&L on closing fills. This module checks
each open position's market and, once resolved, records a closing SELL at the
settlement price ($1 winner / $0 loser). Reuses the ledger's average-cost
accounting, so no parallel P&L math.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from pmbot.data import GammaClient, KalshiClient
from pmbot.models import Fill, Position, Side, Signal, Venue
from pmbot.portfolio.ledger import SETTLEMENT_REASON, Ledger

log = logging.getLogger(__name__)


class Settler:
    def __init__(self, ledger: Ledger, gamma: GammaClient, kalshi: KalshiClient | None = None):
        self.ledger = ledger
        self.gamma = gamma
        self.kalshi = kalshi

    def settle_open_positions(self) -> int:
        """Settle every open position whose market has resolved.

        Returns the number of positions settled. Per-position failures are
        isolated — neither a dead API nor a failed ledger write can block the
        rest of the sweep.
        """
        settled = 0
        for pos in self.ledger.get_positions():
            try:
                price = self._settlement_price(pos)
            except Exception as e:
                log.debug("settle: lookup failed for %s: %s", pos.token_id[:24], e)
                continue
            if price is None:
                continue
            try:
                self._record_settlement(pos, price)
            except Exception as e:
                # The write used to sit outside the guard, so one failure (a
                # locked DB, a full disk) aborted the whole sweep and left
                # every later resolved position unsettled — with its bankroll
                # pinned until the next interval. A write failure isn't
                # transient noise, so it logs louder than a lookup miss.
                log.warning("settle: recording %s failed: %s", pos.token_id[:24], e)
                continue
            settled += 1
        return settled

    def force_settle_market(self, market_id: str, price: float) -> int:
        """Manually settle every open position in one market at `price`.

        Escape hatch for markets that never resolve decisively (final winner
        price below the 0.99 auto-settle threshold) — without it their
        positions pin bankroll forever. Returns positions settled.
        """
        n = 0
        for pos in self.ledger.get_positions():
            if pos.market_id == market_id:
                self._record_settlement(pos, price)
                n += 1
        return n

    # -- resolution lookups ------------------------------------------------
    def _settlement_price(self, pos: Position) -> float | None:
        """1.0 / 0.0 once resolved decisively; None while open/undecided."""
        if pos.venue == Venue.KALSHI.value:
            return self._kalshi_price(pos)
        return self._polymarket_price(pos)

    def _polymarket_price(self, pos: Position) -> float | None:
        closed, winner = self.gamma.get_resolution(pos.market_id)
        if not closed or winner is None:
            return None
        return 1.0 if pos.token_id == winner else 0.0

    def _kalshi_price(self, pos: Position) -> float | None:
        if self.kalshi is None:
            return None
        # Kalshi positions store market_id = ticker, token_id = "kalshi:<ticker>:<side>".
        m = self.kalshi.get_market(pos.market_id)
        if m is None or m.result not in ("yes", "no"):
            return None
        side = pos.token_id.rsplit(":", 1)[-1].lower()
        return 1.0 if side == m.result else 0.0

    # -- accounting ----------------------------------------------------------
    def _record_settlement(self, pos: Position, price: float) -> None:
        side = Side.SELL if pos.shares > 0 else Side.BUY
        shares = abs(pos.shares)
        signal = Signal(
            market_id=pos.market_id,
            token_id=pos.token_id,
            outcome=pos.outcome,
            side=side,
            target_price=price,
            size_usd=round(shares * price, 6),
            # Leader attribution keys off this exact string: it marks where a
            # token's previous round ended, so earlier fills stop counting
            # toward any leader's slice. See `_SINCE_SETTLEMENT`.
            reason=SETTLEMENT_REASON,
            venue=pos.venue,
        )
        fill = Fill(
            signal=signal,
            fill_price=price,
            size_usd=signal.size_usd,
            shares=shares,
            timestamp=datetime.now(timezone.utc),
            mode="paper",
        )
        self.ledger.record_fill(fill)
        log.info(
            "settled %s %s: %.2f sh @ %.0f -> realized %+.2f",
            pos.venue, pos.outcome, shares, price,
            (price - pos.avg_price) * pos.shares,
        )
