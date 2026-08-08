"""Risk manager: turn an unsized Signal into a sized one (or reject it).

Sizing pipeline for an ENTRY (each step can only shrink the order):
  1. start from the leader's notional * copy_fraction
  2. clamp to remaining per-leader allowance
  3. clamp to remaining per-market allowance
  4. clamp to remaining bankroll (total deployed across all positions)
  5. reject dust (< min_ticket_usd)

EXITS take none of those steps: closing risk is not sized off copy_fraction,
does not consume bankroll, and is never blocked by a cap. It is capped only at
what we actually hold, and refused only below `exit_dust_usd`.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from pmbot.config import Settings, get_settings
from pmbot.models import Side, Signal
from pmbot.portfolio.ledger import Ledger

log = logging.getLogger(__name__)

# Conviction-weight clamp: a leader can get at most 2x / at least 0.5x the
# base copy_fraction, so weighting tilts allocation without dominating it.
_WEIGHT_MIN = 0.5
_WEIGHT_MAX = 2.0


class RiskManager:
    def __init__(self, ledger: Ledger, settings: Settings | None = None,
                 min_ticket_usd: float | None = None):
        s = settings or get_settings()
        self.ledger = ledger
        self.bankroll = s.bankroll_usd
        self.copy_fraction = s.copy_fraction
        self.max_per_market = s.max_per_market_usd
        self.max_per_leader = s.max_per_leader_usd
        self.compound = s.compound_profits
        self.min_ticket_usd = s.min_ticket_usd if min_ticket_usd is None else min_ticket_usd
        # Floor for EXITS. The entry floor exists to stop dust copies dying to
        # slippage; applying it to exits made small slices permanently
        # un-closeable — rejected at DEBUG, then re-offered every 10s until the
        # trade scrolled out of the window. Only a true crumb is refused now.
        self.min_exit_usd = s.exit_dust_usd
        # Per-leader conviction multipliers on copy_fraction (1.0 = neutral).
        # Set from vet-backtest ROI at rescore; clamped so no single leader's
        # hot streak can blow past the diversification the caps encode.
        self.leader_weights: dict[str, float] = {}

    def set_leader_weights(self, weights: dict[str, float]) -> None:
        self.leader_weights = {
            w.lower(): min(max(m, _WEIGHT_MIN), _WEIGHT_MAX) for w, m in weights.items()
        }

    def _free_bankroll(self) -> float:
        deployed = sum(abs(p.shares * p.avg_price) for p in self.ledger.get_positions())
        net_pnl = self.ledger.realized_pnl_total() - self.ledger.fees_total()
        base = self.bankroll
        if self.compound:
            # Winnings compound into deployable capital; losses shrink it.
            base += net_pnl
        else:
            # Even without compounding, net losses always shrink what we can
            # deploy — a real account cannot redeploy money it already lost.
            base += min(0.0, net_pnl)
        return base - deployed

    def size(self, signal: Signal) -> Signal | None:
        """Return the signal with `size_usd` set to a safe amount, or None to skip.

        `signal.size_usd` on input is the leader's notional (the copy target)
        for a BUY. For a SELL, the strategy has already computed the exact
        dollar amount to close (proportional to the leader's own exit) —
        closing risk shouldn't be shrunk by `copy_fraction` or blocked by
        bankroll headroom (selling frees capital, it doesn't consume it); it
        only needs to be capped at what we actually hold.
        """
        if signal.side is Side.SELL:
            position = self.ledger.get_position(signal.token_id)
            held_shares = abs(position.shares) if position else 0.0
            held_value = abs(position.shares * position.avg_price) if position else 0.0
            if held_shares <= 0:
                return None
            desired = min(signal.size_usd, held_value)
            if desired < self.min_exit_usd:
                log.info("risk: exit of %s too small to place ($%.4f < $%.4f)",
                         signal.token_id[:10], desired, self.min_exit_usd)
                return None
            if signal.size_shares is None:
                # No explicit share count: derive it from the dollar intent so a
                # partial exit stays partial. Defaulting to the whole position
                # here would let size_usd say "half" while the executor — which
                # trusts size_shares — sold everything.
                frac = min(1.0, desired / held_value) if held_value > 0 else 1.0
                shares = held_shares * frac
            else:
                shares = min(signal.size_shares, held_shares)
            return replace(signal, size_usd=round(desired, 2), size_shares=shares)

        fraction = self.copy_fraction
        if signal.source_leader:
            fraction *= self.leader_weights.get(signal.source_leader.lower(), 1.0)
        desired = signal.size_usd * fraction
        if desired <= 0:
            return None

        if signal.source_leader:
            used = self.ledger.exposure_for_leader(signal.source_leader)
            desired = min(desired, self.max_per_leader - used)

        used_market = self.ledger.exposure_for_market(signal.market_id)
        desired = min(desired, self.max_per_market - used_market)
        desired = min(desired, self._free_bankroll())

        if desired < self.min_ticket_usd:
            log.debug("risk: rejecting %s (sized $%.2f < min $%.2f)",
                      signal.token_id[:10], desired, self.min_ticket_usd)
            return None
        return replace(signal, size_usd=round(desired, 2))

    def check_group(self, signals: list[Signal]) -> bool:
        """Approve/reject a pre-sized multi-leg group (arbitrage legs).

        Arb legs are sized upstream (budget + book depth) and must not be
        rescaled independently — shrinking one leg would unhedge the pair.
        So this is a yes/no gate: combined cost must fit the remaining
        bankroll and each market's remaining allowance.
        """
        total_cost = sum(s.size_usd for s in signals)
        if total_cost <= 0:
            return False

        if total_cost > self._free_bankroll():
            log.debug("risk: arb group rejected ($%.2f > free bankroll)", total_cost)
            return False

        for s in signals:
            used = self.ledger.exposure_for_market(s.market_id)
            if used + s.size_usd > self.max_per_market:
                log.debug("risk: arb group rejected (market %s over cap)", s.market_id[:12])
                return False
        return True
