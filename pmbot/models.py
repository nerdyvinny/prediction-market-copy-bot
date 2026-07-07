"""Shared internal data models.

These are deliberately decoupled from any external API shape — the data clients
map raw Polymarket JSON into these types so the rest of the bot has one
stable vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Venue(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


@dataclass(frozen=True)
class LeaderTrade:
    """A single trade observed from a leader wallet (public Data API)."""

    leader: str          # wallet address (0x...)
    market_id: str       # Polymarket condition id
    token_id: str        # outcome ERC-1155 token id (used by CLOB)
    outcome: str         # outcome label, e.g. "Yes"
    side: Side
    price: float         # probability price, 0..1
    shares: float        # outcome shares
    usd_size: float      # notional in USDC
    timestamp: datetime
    tx_hash: str | None = None
    event_slug: str | None = None  # coarse grouping for category/consistency scoring

    @property
    def uid(self) -> str:
        """Stable id for dedupe (don't copy the same trade twice)."""
        if self.tx_hash:
            return self.tx_hash
        return f"{self.leader}:{self.token_id}:{self.timestamp.isoformat()}"


@dataclass(frozen=True)
class Market:
    """Market metadata from the Gamma API."""

    market_id: str                       # condition id
    question: str
    category: str | None = None
    end_date: datetime | None = None     # resolution date
    liquidity_usd: float | None = None
    closed: bool = False
    tokens: dict[str, str] = field(default_factory=dict)  # outcome -> token_id


@dataclass(frozen=True)
class Quote:
    """Top-of-book for one outcome token."""

    token_id: str
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None   # shares available at best bid
    ask_size: float | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return self.bid if self.ask is None else self.ask
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class KalshiMarket:
    """A binary market on Kalshi, including the top-of-book quotes that the
    /markets endpoint embeds (saves a per-market orderbook call when scanning).

    Prices are in dollars 0..1, same convention as Polymarket probability
    prices. Sizes are in contracts (1 contract pays $1 if right).
    """

    ticker: str                     # e.g. "KXHIGHNY-26JUL04-T98"
    event_ticker: str
    title: str
    subtitle: str = ""
    status: str = ""                # "active" | "closed" | "settled" | ...
    close_time: datetime | None = None
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    yes_ask_size: float | None = None   # contracts available at yes_ask
    no_ask_size: float | None = None
    volume_24h: float | None = None
    liquidity_usd: float | None = None
    rules_primary: str = ""
    result: str = ""                # "yes" | "no" | "" when unsettled

    @property
    def is_open(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class Signal:
    """A strategy's intent to take a position (pre risk-sizing)."""

    market_id: str
    token_id: str
    outcome: str
    side: Side
    target_price: float
    size_usd: float
    reason: str
    source_leader: str | None = None
    source_uid: str | None = None  # leader-trade uid, for dedupe
    venue: str = Venue.POLYMARKET.value
    # Arbitrage legs that must execute together share a leg_group id; the
    # engine executes such groups both-or-neither.
    leg_group: str | None = None


@dataclass(frozen=True)
class Fill:
    """An executed (or simulated) fill."""

    signal: Signal
    fill_price: float
    size_usd: float
    shares: float
    timestamp: datetime
    mode: str               # "paper" | "live"
    slippage_bps: float = 0.0
    fee_usd: float = 0.0    # exchange fee (Kalshi taker fee; Polymarket = 0)


@dataclass
class Position:
    """Aggregated holding in one outcome token."""

    market_id: str
    token_id: str
    outcome: str
    shares: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    venue: str = Venue.POLYMARKET.value
