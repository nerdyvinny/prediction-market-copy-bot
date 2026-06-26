"""Strategy interface: turn observations into (unsized) Signals.

A strategy decides *what* to trade and *why*. It does NOT decide size — that's
the RiskManager's job — and it does NOT place orders — that's the executor's.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from pmbot.models import Signal


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def generate(self) -> Iterable[Signal]:
        """Yield unsized Signals representing intended positions."""
