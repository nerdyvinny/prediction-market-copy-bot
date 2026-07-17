"""Paper executor: simulate fills against the live order book + a slippage model.

No orders are placed and no funds move. A BUY is assumed to fill at the ask
nudged up by the slippage budget; a SELL at the bid nudged down. If the book is
unavailable, we fall back to the signal's target price. Every fill is recorded
to the ledger so paper P&L tracks exactly like live would.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from pmbot.arb.fees import kalshi_taker_fee
from pmbot.config import get_settings
from pmbot.data import PriceCache
from pmbot.execution.executor import TradeExecutor
from pmbot.models import Fill, Position, Side, Signal, Venue
from pmbot.portfolio.ledger import Ledger

log = logging.getLogger(__name__)

_MIN_PRICE = 1e-4
_MAX_PRICE = 1 - 1e-4


class PaperExecutor(TradeExecutor):
    mode = "paper"

    def __init__(
        self,
        ledger: Ledger,
        price_cache: PriceCache | None = None,
        slippage_bps: float | None = None,
    ):
        settings = get_settings()
        self.ledger = ledger
        self.price_cache = price_cache or PriceCache()
        self.slippage_bps = settings.slippage_bps if slippage_bps is None else slippage_bps

    def execute(self, signal: Signal) -> Fill | None:
        if signal.size_usd <= 0:
            return None
        fill = self._build_fill(signal)
        if fill is None:
            log.warning("paper: no price for token %s; skipping", signal.token_id)
            return None
        self.ledger.record_fill(fill)
        log.info(
            "paper %s %s %.2f sh @ %.4f ($%.2f) %s",
            signal.side.value, signal.outcome, fill.shares, fill.fill_price,
            signal.size_usd, signal.reason,
        )
        return fill

    def execute_group(self, signals: list[Signal]) -> list[Fill] | None:
        """Atomic multi-leg fill: price every leg first, record only if ALL
        legs priced. This is what makes paper arb both-or-neither."""
        fills = [self._build_fill(s) for s in signals if s.size_usd > 0]
        if len(fills) != len(signals) or any(f is None for f in fills):
            log.warning("paper: leg group aborted (a leg failed to price)")
            return None
        for f in fills:
            self.ledger.record_fill(f)
            log.info(
                "paper %s %s[%s] %.2f sh @ %.4f ($%.2f, fee $%.2f) %s",
                f.signal.side.value, f.signal.outcome, f.signal.venue, f.shares,
                f.fill_price, f.size_usd, f.fee_usd, f.signal.reason,
            )
        return fills  # type: ignore[return-value]

    def _build_fill(self, signal: Signal) -> Fill | None:
        """Price a signal and construct the Fill without recording it."""
        price = self._resolve_fill_price(signal)
        if price is None:
            return None
        if signal.side is Side.SELL:
            # Fill SELLs in shares, never size_usd / price: size_usd is valued
            # at our avg cost, so converting it at a collapsed market price
            # would sell orders of magnitude more shares than we hold and flip
            # the position into a fictitious short. Hard-cap at held shares.
            pos = self.ledger.get_position(signal.token_id)
            held = abs(pos.shares) if pos else 0.0
            if held <= 1e-9:
                log.warning("paper: sell of %s with no position; skipping", signal.token_id[:10])
                return None
            desired = signal.size_usd / price if signal.size_shares is None else signal.size_shares
            shares = min(desired, held)
            size_usd = shares * price
        else:
            shares = signal.size_usd / price
            size_usd = signal.size_usd
        fee = 0.0
        if signal.venue == Venue.KALSHI.value:
            # Kalshi taker fee on the order (contracts == shares).
            fee = kalshi_taker_fee(shares, price)
        return Fill(
            signal=signal,
            fill_price=price,
            size_usd=size_usd,
            shares=shares,
            timestamp=datetime.now(timezone.utc),
            mode=self.mode,
            slippage_bps=self.slippage_bps,
            fee_usd=fee,
        )

    def _resolve_fill_price(self, signal: Signal) -> float | None:
        slip = self.slippage_bps / 10_000.0
        quote = None
        if signal.venue == Venue.POLYMARKET.value:
            try:
                quote = self.price_cache.get_quote(signal.token_id)
            except Exception as e:  # network hiccup -> fall back to target price
                log.debug("paper: quote fetch failed (%s); using target price", e)
        # Kalshi legs fill at the scan-time book price (target_price): the
        # CLOB cache can't quote Kalshi tokens, and the scan happened seconds
        # ago in the same cycle. Slippage budget still applies.

        if signal.side is Side.BUY:
            base = quote.ask if (quote and quote.ask) else signal.target_price
            price = base * (1 + slip) if base else None
        else:
            base = quote.bid if (quote and quote.bid) else signal.target_price
            price = base * (1 - slip) if base else None

        if not price or price <= 0:
            return None
        return min(max(price, _MIN_PRICE), _MAX_PRICE)

    def get_positions(self) -> list[Position]:
        return self.ledger.get_positions()
