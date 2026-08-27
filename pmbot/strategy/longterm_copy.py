"""Strategy #4 — long-term outcome copy.

For each auto-selected leader, mirror their *entries* (BUYs) in markets that
resolve far enough in the future that execution lag barely matters. We hold to
resolution, so we deliberately do NOT mirror their exits (SELLs).

Filters per copied trade:
  - side is BUY
  - market is open and resolves >= `min_days_to_resolution` days out
  - market liquidity >= `min_liquidity`
  - we haven't already copied this exact leader-trade (ledger dedupe)

Sizing is left entirely to the RiskManager; `Signal.size_usd` here carries the
leader's notional as the copy *target*.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from datetime import datetime, timezone

from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.models import Market, Side, Signal
from pmbot.portfolio.ledger import Ledger
from pmbot.strategy.base import Strategy

log = logging.getLogger(__name__)


class LongTermCopyStrategy(Strategy):
    name = "longterm_copy"

    def __init__(
        self,
        data: PolymarketDataClient,
        gamma: GammaClient,
        ledger: Ledger,
        leaders: list[str] | None = None,
        *,
        min_days_to_resolution: int | None = None,
        min_liquidity: float | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        trades_per_leader: int = 25,
        market_cache_ttl: float = 300.0,
    ):
        s = get_settings()
        self.data = data
        self.gamma = gamma
        self.ledger = ledger
        self.leaders = [w.lower() for w in (leaders or [])]
        self.min_days = (
            s.longterm_min_days_to_resolution if min_days_to_resolution is None else min_days_to_resolution
        )
        self.min_liquidity = s.min_market_liquidity_usd if min_liquidity is None else min_liquidity
        self.price_min = s.copy_price_min if price_min is None else price_min
        self.price_max = s.copy_price_max if price_max is None else price_max
        self.trades_per_leader = trades_per_leader
        self._market_cache_ttl = market_cache_ttl
        self._market_cache: dict[str, tuple[float, Market | None]] = {}

    def set_leaders(self, leaders: list[str], *, exit_only: list[str] | None = None) -> None:
        # `exit_only` is accepted for engine-interface parity but unused:
        # this strategy holds to resolution and never mirrors exits.
        self.leaders = [w.lower() for w in leaders]

    def _market(self, condition_id: str) -> Market | None:
        now = time.monotonic()
        hit = self._market_cache.get(condition_id)
        if hit and (now - hit[0]) < self._market_cache_ttl:
            return hit[1]
        try:
            m = self.gamma.get_market(condition_id)
        except Exception as e:
            # Transient failure (rate limit, timeout): serve the stale entry if
            # we have one, but never CACHE the error — a poisoned entry would
            # silently drop every trade in the market for the next TTL window.
            # (Same fix as ExactCopyStrategy._market, LeaderSelector._resolver
            # and both backtesters.)
            log.debug("strategy: market lookup failed %s: %s", condition_id[:12], e)
            return hit[1] if hit else None
        self._market_cache[condition_id] = (now, m)
        return m

    def generate(self) -> Iterable[Signal]:
        now = datetime.now(timezone.utc)
        for leader in self.leaders:
            try:
                trades = self.data.get_trades(user=leader, limit=self.trades_per_leader)
            except Exception as e:
                log.debug("strategy: trades fetch failed for %s: %s", leader[:10], e)
                continue
            for t in trades:
                if t.side is not Side.BUY:
                    continue                       # hold-to-resolution: don't mirror exits
                if not (self.price_min <= t.price <= self.price_max):
                    continue                       # skip extreme-priced, low-edge trades
                if self.ledger.has_copied(t.uid):
                    continue
                market = self._market(t.market_id)
                if market is None or market.closed:
                    continue
                if market.end_date is None:
                    continue                       # can't confirm horizon -> skip
                days = (market.end_date - now).total_seconds() / 86_400
                if days < self.min_days:
                    continue
                if market.liquidity_usd is not None and market.liquidity_usd < self.min_liquidity:
                    continue
                yield Signal(
                    market_id=t.market_id,
                    token_id=t.token_id,
                    outcome=t.outcome,
                    side=Side.BUY,
                    target_price=t.price,
                    size_usd=t.usd_size,           # leader notional = copy target
                    reason=f"copy {leader[:8]}… longterm (~{days:.0f}d)",
                    source_leader=leader,
                    source_uid=t.uid,
                )
