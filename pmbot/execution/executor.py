"""Abstract trade executor: the seam between strategy and the outside world.

Paper and (later) live executors implement this so the engine never needs to
know whether an order is simulated or real.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pmbot.models import Fill, Position, Signal


class TradeExecutor(ABC):
    mode: str = "abstract"

    @abstractmethod
    def execute(self, signal: Signal) -> Fill | None:
        """Attempt to fill a sized signal. Returns the Fill, or None if skipped."""

    def execute_group(self, signals: list[Signal]) -> list[Fill] | None:
        """Fill a multi-leg group all-or-nothing (arbitrage legs).

        Default is sequential and NOT atomic; executors that can should
        override to guarantee both-or-neither.
        """
        fills = []
        for sig in signals:
            fill = self.execute(sig)
            if fill is None:
                return None
            fills.append(fill)
        return fills

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Current open positions."""
