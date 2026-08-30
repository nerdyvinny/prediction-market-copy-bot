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

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Current open positions."""
