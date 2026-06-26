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

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return self.bid if self.ask is None else self.ask
        return (self.bid + self.ask) / 2.0


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


@dataclass
class Position:
    """Aggregated holding in one outcome token."""

    market_id: str
    token_id: str
    outcome: str
    shares: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
