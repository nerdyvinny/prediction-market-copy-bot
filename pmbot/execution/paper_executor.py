"""Paper executor: simulate fills against the live order book + a slippage model.

No orders are placed and no funds move. A BUY is assumed to fill at the ask
nudged up by the slippage budget; a SELL at the bid nudged down. If the book is
unavailable, we fall back to the signal's target price. Every fill is recorded
to the ledger so paper P&L tracks exactly like live would.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

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
        # Limit-order semantics for copied entries: never pay more than the
        # leader's price + the drift budget. The strategy's drift guard checks
        # the MID; a thin book's ask can sit far above it, and chasing it
        # would let live fills run away from what every backtest assumes.
        self.max_entry_premium = settings.copy_max_price_drift
        # Why the last `execute` returned None, in the same vocabulary
        # `BacktestReport.skipped` uses. The engine folds it into the cycle's
        # skip line; without it a refused copy was three different events that
        # all looked like "no fill".
        self.last_refusal: str | None = None

    def execute(self, signal: Signal) -> Fill | None:
        self.last_refusal = None
        if signal.size_usd <= 0:
            self.last_refusal = "zero_size"
            return None
        fill = self._build_fill(signal)
        if fill is None:
            log.warning("paper: no price for token %s; skipping", signal.token_id)
            return None
        self.ledger.record_fill(fill)
        log.info(
            # fill.size_usd, not signal.size_usd: a SELL's requested size is
            # valued at our avg cost, so logging it printed the cost basis
            # instead of the proceeds and made every exit look flat.
            "paper %s %s %.2f sh @ %.4f ($%.2f) %s",
            signal.side.value, signal.outcome, fill.shares, fill.fill_price,
            fill.size_usd, signal.reason,
        )
        return fill

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
                self.last_refusal = "exit_no_position"
                return None
            desired = signal.size_usd / price if signal.size_shares is None else signal.size_shares
            shares = min(desired, held)
            size_usd = shares * price
        else:
            shares = signal.size_usd / price
            size_usd = signal.size_usd
        return Fill(
            signal=signal,
            fill_price=price,
            size_usd=size_usd,
            shares=shares,
            timestamp=datetime.now(timezone.utc),
            mode=self.mode,
            slippage_bps=self.slippage_bps,
            fee_usd=0.0,
        )

    def _resolve_fill_price(self, signal: Signal) -> float | None:
        slip = self.slippage_bps / 10_000.0
        quote = None
        if signal.venue == Venue.POLYMARKET.value:
            try:
                quote = self.price_cache.get_quote(signal.token_id)
            except Exception as e:  # network hiccup -> fall back to target price
                log.debug("paper: quote fetch failed (%s); using target price", e)
        is_copy_buy = (
            signal.side is Side.BUY
            and signal.source_leader is not None
            and signal.venue == Venue.POLYMARKET.value
        )
        if signal.side is Side.BUY:
            if quote and quote.ask:
                base = quote.ask
            elif is_copy_buy:
                # No live book -> no copied entry. Falling back to the leader's
                # own price would grant perfect fills exactly when we're blind
                # (CLOB outage), inflating paper P&L with trades a live order
                # could never get.
                log.info("paper: no book for %s; skipping copy buy", signal.token_id[:10])
                self.last_refusal = "no_book"
                return None
            else:
                base = signal.target_price
            price = base * (1 + slip) if base else None
            if price and is_copy_buy and price > signal.target_price + self.max_entry_premium:
                log.info(
                    "paper: fill %.4f beyond limit %.4f (leader %.4f); skipping copy buy",
                    price, signal.target_price + self.max_entry_premium, signal.target_price,
                )
                self.last_refusal = "entry_limit"
                return None
        else:
            is_copy_sell = (
                signal.source_leader is not None
                and signal.venue == Venue.POLYMARKET.value
            )
            if quote and quote.bid:
                base = quote.bid
            elif is_copy_sell:
                # Symmetric with the copy-BUY guard above. Falling back to the
                # leader's own price grants an exit no live order could get —
                # and an empty book is exactly where that bites: a dead,
                # illiquid, or already-resolved market (the strategy's market
                # cache is 300s stale, so "closed since we last looked" is
                # routine). The uid is not retired, so this retries every poll;
                # if the book never comes back, settlement books the true
                # payout, which is the honest answer since we could not have
                # sold either.
                log.info("paper: no bid for %s; skipping copy sell", signal.token_id[:10])
                self.last_refusal = "exit_no_book"
                return None
            else:
                base = signal.target_price
            price = base * (1 - slip) if base else None

        if not price or price <= 0:
            self.last_refusal = "no_price"
            return None
        return min(max(price, _MIN_PRICE), _MAX_PRICE)

    def get_positions(self) -> list[Position]:
        return self.ledger.get_positions()
