"""Strategies: turn observations into Signals."""

from pmbot.strategy.base import Strategy
from pmbot.strategy.longterm_copy import LongTermCopyStrategy

__all__ = ["Strategy", "LongTermCopyStrategy"]
