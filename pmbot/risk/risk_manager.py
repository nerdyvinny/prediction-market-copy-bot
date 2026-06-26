"""Risk manager: turn an unsized Signal into a sized one (or reject it).

Sizing pipeline (each step can only shrink the order):
  1. start from the leader's notional * copy_fraction
  2. clamp to remaining per-leader allowance
  3. clamp to remaining per-market allowance
  4. clamp to remaining bankroll (total deployed across all positions)
  5. reject dust (< min_ticket_usd)
"""

from __future__ import annotations

import logging
from dataclasses import replace

from pmbot.config import Settings, get_settings
from pmbot.models import Signal
from pmbot.portfolio.ledger import Ledger

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, ledger: Ledger, settings: Settings | None = None, min_ticket_usd: float = 1.0):
        s = settings or get_settings()
        self.ledger = ledger
        self.bankroll = s.bankroll_usd
        self.copy_fraction = s.copy_fraction
        self.max_per_market = s.max_per_market_usd
        self.max_per_leader = s.max_per_leader_usd
        self.min_ticket_usd = min_ticket_usd

    def size(self, signal: Signal) -> Signal | None:
        """Return the signal with `size_usd` set to a safe amount, or None to skip.

        `signal.size_usd` on input is the leader's notional (the copy target).
        """
        desired = signal.size_usd * self.copy_fraction
        if desired <= 0:
            return None

        if signal.source_leader:
            used = abs(self.ledger.exposure_for_leader(signal.source_leader))
            desired = min(desired, self.max_per_leader - used)

        used_market = self.ledger.exposure_for_market(signal.market_id)
        desired = min(desired, self.max_per_market - used_market)

        total_deployed = sum(abs(p.shares * p.avg_price) for p in self.ledger.get_positions())
        desired = min(desired, self.bankroll - total_deployed)

        if desired < self.min_ticket_usd:
            log.debug("risk: rejecting %s (sized $%.2f < min $%.2f)",
                      signal.token_id[:10], desired, self.min_ticket_usd)
            return None
        return replace(signal, size_usd=round(desired, 2))
