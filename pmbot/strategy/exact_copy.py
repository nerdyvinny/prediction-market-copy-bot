"""Strategy #5 — exact copy: mirror a leader's BUYs *and* SELLs, in near real
time, sized off our own bankroll/position (not the leader's).

Filters per copied ENTRY (exits are only checked for dedupe + market open —
mirroring a leader out of a position reduces risk and is never blocked):
  - market is liquid enough (`min_liquidity`)
  - price isn't at the extremes (`price_min`/`price_max`) — little edge there
  - leader's notional >= `min_leader_notional` — small probes carry little
    conviction and our 5% slice of them dies to slippage
  - the current quote hasn't drifted more than `max_price_drift` from the
    leader's own fill price — if the edge that made the trade worth copying
    has already been arbitraged away by faster bots, skip it rather than
    chase a stale price

BUY: same shape as a fresh entry, sized by the RiskManager off leader notional.
SELL: mirrored *proportionally*. We track each leader's running per-token
share count from the trades we've observed (using the same `apply_fill`
accounting the ledger uses) so a partial exit by the leader becomes a partial
exit of our own position, not a full liquidation. If the leader's prior
position is unknown (e.g. it predates when we started following them), we
conservatively treat the sell as a full exit.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from datetime import datetime, timezone

from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient, PriceCache
from pmbot.models import Market, Side, Signal
from pmbot.portfolio.ledger import Ledger, apply_fill
from pmbot.strategy.base import Strategy

log = logging.getLogger(__name__)


class ExactCopyStrategy(Strategy):
    name = "exact_copy"

    def __init__(
        self,
        data: PolymarketDataClient,
        gamma: GammaClient,
        ledger: Ledger,
        leaders: list[str] | None = None,
        *,
        price_cache: PriceCache | None = None,
        min_liquidity: float | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        max_price_drift: float | None = None,
        min_leader_notional: float | None = None,
        min_hours_to_resolution: float | None = None,
        trades_per_leader: int = 25,
        market_cache_ttl: float = 300.0,
    ):
        s = get_settings()
        self.data = data
        self.gamma = gamma
        self.ledger = ledger
        self.price_cache = price_cache
        self.leaders = [w.lower() for w in (leaders or [])]
        self.min_liquidity = s.min_market_liquidity_usd if min_liquidity is None else min_liquidity
        self.price_min = s.copy_price_min if price_min is None else price_min
        self.price_max = s.copy_price_max if price_max is None else price_max
        self.max_price_drift = s.copy_max_price_drift if max_price_drift is None else max_price_drift
        self.min_leader_notional = (
            s.copy_min_leader_notional_usd if min_leader_notional is None else min_leader_notional
        )
        self.min_hours_to_resolution = (
            s.copy_min_hours_to_resolution if min_hours_to_resolution is None
            else min_hours_to_resolution
        )
        self.trades_per_leader = trades_per_leader
        self._market_cache_ttl = market_cache_ttl
        self._market_cache: dict[str, tuple[float, Market | None]] = {}
        # Per (leader, token) running share count, reconstructed from trades
        # we've observed since we started following this leader.
        self._leader_shares: dict[tuple[str, str], float] = {}
        self._seen_uids: set[str] = set()

    def set_leaders(self, leaders: list[str]) -> None:
        self.leaders = [w.lower() for w in leaders]

    def _market(self, condition_id: str) -> Market | None:
        now = time.monotonic()
        hit = self._market_cache.get(condition_id)
        if hit and (now - hit[0]) < self._market_cache_ttl:
            return hit[1]
        try:
            m = self.gamma.get_market(condition_id)
        except Exception as e:
            log.debug("strategy: market lookup failed %s: %s", condition_id[:12], e)
            m = None
        self._market_cache[condition_id] = (now, m)
        return m

    def _too_stale(self, token_id: str, leader_price: float) -> bool:
        if not self.price_cache:
            return False
        try:
            quote = self.price_cache.get_quote(token_id)
        except Exception as e:
            log.debug("strategy: quote fetch failed for %s: %s", token_id[:10], e)
            return False
        mid = None
        if quote and quote.bid and quote.ask:
            mid = (quote.bid + quote.ask) / 2
        elif quote and (quote.bid or quote.ask):
            mid = quote.bid or quote.ask
        if mid is None:
            return False
        return abs(mid - leader_price) > self.max_price_drift

    def generate(self) -> Iterable[Signal]:
        now = datetime.now(timezone.utc)
        for leader in self.leaders:
            try:
                trades = self.data.get_trades(user=leader, limit=self.trades_per_leader)
            except Exception as e:
                log.debug("strategy: trades fetch failed for %s: %s", leader[:10], e)
                continue
            # Oldest-first so leader position tracking replays in order.
            for t in sorted(trades, key=lambda tr: tr.timestamp):
                key = (leader, t.token_id)
                already_seen = t.uid in self._seen_uids
                if not already_seen:
                    # Apply exactly once per trade uid — the same trade stays
                    # in the fetched window across multiple poll cycles, and
                    # re-applying it would double-count the leader's position.
                    prior_shares = self._leader_shares.get(key, 0.0)
                    eff = apply_fill(prior_shares, 0.0, t.side, t.shares, t.price)
                    self._seen_uids.add(t.uid)
                    self._leader_shares[key] = eff.new_shares

                if already_seen or self.ledger.has_copied(t.uid):
                    continue

                market = self._market(t.market_id)
                if market is None or market.closed:
                    continue

                if t.side is Side.BUY:
                    # Entry-only filters: exits are never blocked by these —
                    # mirroring a leader out of a position reduces our risk.
                    if not (self.price_min <= t.price <= self.price_max):
                        continue                   # skip extreme-priced, low-edge trades
                    if t.usd_size < self.min_leader_notional:
                        continue                   # low-conviction probe; not worth our slice
                    if self._too_stale(t.token_id, t.price):
                        continue                   # edge likely already gone
                    if market.liquidity_usd is not None and market.liquidity_usd < self.min_liquidity:
                        continue
                    if self.min_hours_to_resolution > 0 and market.end_date is not None:
                        hours_left = (market.end_date - now).total_seconds() / 3600
                        if hours_left < self.min_hours_to_resolution:
                            continue               # too close to resolution; lag is adverse
                    yield Signal(
                        market_id=t.market_id,
                        token_id=t.token_id,
                        outcome=t.outcome,
                        side=Side.BUY,
                        target_price=t.price,
                        size_usd=t.usd_size,       # leader notional = copy target
                        reason=f"copy {leader[:8]}… buy",
                        source_leader=leader,
                        source_uid=t.uid,
                    )
                else:
                    our_position = self.ledger.get_position(t.token_id)
                    if our_position is None or abs(our_position.shares) <= 1e-9:
                        continue                   # nothing of ours to mirror-sell
                    sell_fraction = 1.0 if prior_shares <= 1e-9 else min(1.0, t.shares / prior_shares)
                    our_value = abs(our_position.shares) * our_position.avg_price
                    yield Signal(
                        market_id=t.market_id,
                        token_id=t.token_id,
                        outcome=t.outcome,
                        side=Side.SELL,
                        target_price=t.price,
                        size_usd=our_value * sell_fraction,
                        size_shares=abs(our_position.shares) * sell_fraction,
                        reason=f"copy {leader[:8]}… sell {sell_fraction*100:.0f}%",
                        source_leader=leader,
                        source_uid=t.uid,
                    )
