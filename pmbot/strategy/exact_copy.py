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
  - the trade is younger than `max_trade_age_minutes` — a newly followed
    leader's whole recent tape looks "unseen", and old entries whose price
    merely happens to be unchanged are history, not signals

BUY: same shape as a fresh entry, sized by the RiskManager off leader notional.
SELL: mirrored *proportionally*, and only over OUR OWN slice from that leader.
Two independent quantities drive this:
  - how much of THEIR position the leader exited — we track each leader's
    running per-token share count from the trades we've observed (using the
    same `apply_fill` accounting the ledger uses), so a partial trim becomes a
    partial exit rather than a full liquidation. If their prior position is
    unknown (e.g. it predates when we started following them), we
    conservatively treat the sell as a full exit.
  - how many shares WE hold because of that leader
    (`Ledger.copied_shares_for_leader`). Several followed leaders can hold the
    same outcome token while `positions` knows only the combined total, so
    sizing off the total let one leader's exit liquidate another's copy.

Exit-only leaders: a leader who falls off the followed list while we still
hold positions copied from them is kept on the watchlist as exit-only — their
SELLs are still mirrored (so our copied positions don't ride unmanaged to
resolution) but their BUYs never open new positions.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from datetime import datetime, timezone

from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient, PriceCache
from pmbot.models import LeaderTrade, Market, Side, Signal
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
        max_trade_age_minutes: float | None = None,
        trades_per_leader: int = 25,
        market_cache_ttl: float = 300.0,
    ):
        s = get_settings()
        self.data = data
        self.gamma = gamma
        self.ledger = ledger
        self.price_cache = price_cache
        self.leaders = [w.lower() for w in (leaders or [])]
        self.exit_only_leaders: list[str] = []
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
        self.max_trade_age_minutes = (
            s.copy_max_trade_age_minutes if max_trade_age_minutes is None
            else max_trade_age_minutes
        )
        self.trades_per_leader = trades_per_leader
        self._market_cache_ttl = market_cache_ttl
        self._market_cache: dict[str, tuple[float, Market | None]] = {}
        # Per (leader, token) running share count, reconstructed from trades
        # we've observed since we started following this leader. Persisted in
        # the ledger so a restart keeps proportional exits proportional
        # instead of degrading every leader trim into a full exit of ours.
        self._leader_shares: dict[tuple[str, str], float] = {}
        # Two dedupe sets with different jobs and lifetimes:
        #   _seen_uids     — "already counted toward leader position tracking";
        #                    persisted, so restarts never double-count.
        #   _processed_uids — "copy decision was made" (copied OR deliberately
        #                    filtered); in-memory only, so a restart re-decides
        #                    (the ledger's has_copied still dedupes real fills)
        #                    and a transient skip isn't a permanent one.
        self._seen_uids: set[str] = set()
        self._processed_uids: set[str] = set()
        try:
            self.ledger.prune_seen_trades(older_than_days=30.0)
            self._leader_shares = self.ledger.load_leader_positions()
            self._seen_uids = self.ledger.load_seen_uids()
        except Exception as e:
            log.debug("strategy: leader-tracking restore failed: %s", e)

    def set_leaders(self, leaders: list[str], *, exit_only: list[str] | None = None) -> None:
        """Update the watchlist. `exit_only` wallets keep their SELLs mirrored
        but never trigger BUYs; a wallet in both lists is treated as followed."""
        self.leaders = [w.lower() for w in leaders]
        followed = set(self.leaders)
        self.exit_only_leaders = [
            w.lower() for w in (exit_only or []) if w.lower() not in followed
        ]

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
            # (Same fix as LeaderSelector._resolver and the backtesters.)
            log.debug("strategy: market lookup failed %s: %s", condition_id[:12], e)
            return hit[1] if hit else None
        self._market_cache[condition_id] = (now, m)
        return m

    def _price_drift_ok(self, token_id: str, leader_price: float) -> bool | None:
        """Is the leader's fill price still on the book?

        True  — verified: the quote sits within `max_price_drift`.
        False — verified: the edge has moved away; don't chase it.
        None  — COULD NOT VERIFY (fetch error, or a book with no top level).
                The caller must treat this as a transient skip and retry next
                cycle, never as a decision. Deciding on "no answer" is what let
                a single CLOB hiccup retire a copyable trade for the whole
                process — the trade was never re-quoted again.

        Having no cache client is an explicit opt-out (backtests/offline), so
        that verifies as True rather than blocking every entry.
        """
        if not self.price_cache:
            return True
        try:
            quote = self.price_cache.get_quote(token_id)
        except Exception as e:
            log.debug("strategy: quote fetch failed for %s: %s", token_id[:10], e)
            return None
        mid = None
        if quote and quote.bid and quote.ask:
            mid = (quote.bid + quote.ask) / 2
        elif quote and (quote.bid or quote.ask):
            mid = quote.bid or quote.ask
        if mid is None:
            return None
        return abs(mid - leader_price) <= self.max_price_drift

    def _entry_reject_reason(
        self, t: LeaderTrade, market: Market, now: datetime
    ) -> str | None:
        """Why this entry can never be copied, or None if it's still a candidate.

        Every reason here is settled for the life of the trade — the leader's
        price and notional are fixed, a closed market never reopens, and both
        the trade's age and the market's time-to-resolution only move one way —
        so the caller may retire the uid. (Thin liquidity can in principle
        recover, but not meaningfully inside the max-trade-age window.)
        Transient conditions — a failed market lookup, an unverifiable quote —
        are deliberately NOT decided here.
        """
        if market.closed:
            return "market closed"
        if not (self.price_min <= t.price <= self.price_max):
            return "price outside entry band"     # little edge at the extremes
        if t.usd_size < self.min_leader_notional:
            return "leader notional too small"    # low-conviction probe
        if self.max_trade_age_minutes > 0:
            age_min = (now - t.timestamp).total_seconds() / 60.0
            if age_min > self.max_trade_age_minutes:
                return "trade too old"            # stale history, not a fresh signal
        if market.liquidity_usd is not None and market.liquidity_usd < self.min_liquidity:
            return "market too thin"
        if self.min_hours_to_resolution > 0 and market.end_date is not None:
            hours_left = (market.end_date - now).total_seconds() / 3600
            if hours_left < self.min_hours_to_resolution:
                return "too close to resolution"  # lag is adverse here
        return None

    def generate(self) -> Iterable[Signal]:
        now = datetime.now(timezone.utc)
        watchlist = [(w, False) for w in self.leaders]
        watchlist += [(w, True) for w in self.exit_only_leaders]
        for leader, exit_only in watchlist:
            try:
                trades = self.data.get_trades(user=leader, limit=self.trades_per_leader)
            except Exception as e:
                log.debug("strategy: trades fetch failed for %s: %s", leader[:10], e)
                continue
            # Oldest-first so leader position tracking replays in order.
            for t in sorted(trades, key=lambda tr: tr.timestamp):
                key = (leader, t.token_id)
                if t.uid not in self._seen_uids:
                    # Count toward leader position tracking exactly once per
                    # trade uid — the same trade stays in the fetched window
                    # across poll cycles, and re-applying it would double-count
                    # the leader's position.
                    prior_shares = self._leader_shares.get(key, 0.0)
                    eff = apply_fill(prior_shares, 0.0, t.side, t.shares, t.price)
                    self._seen_uids.add(t.uid)
                    self._leader_shares[key] = eff.new_shares
                    try:
                        self.ledger.record_leader_observation(
                            leader, t.token_id, eff.new_shares, t.uid, t.timestamp
                        )
                    except Exception as e:
                        # Both writes share one transaction, so a failure lands
                        # neither and a restart replays this trade correctly —
                        # but a DB that keeps failing must not do so silently.
                        log.warning("strategy: leader-tracking persist failed: %s", e)
                elif t.side is Side.SELL:
                    # Tracked before this process started (restart): the
                    # stored count is post-sell, so reconstruct the pre-sell
                    # count for proportional mirroring.
                    prior_shares = self._leader_shares.get(key, 0.0) + t.shares
                else:
                    prior_shares = self._leader_shares.get(key, 0.0)

                if t.uid in self._processed_uids or self.ledger.has_copied(t.uid):
                    continue

                if exit_only and t.side is Side.BUY:
                    continue                       # exit-only leader: mirror exits, never new entries

                market = self._market(t.market_id)

                if t.side is Side.BUY:
                    # Entries need live market metadata; a TRANSIENT lookup
                    # failure leaves the trade unprocessed so the next cycle
                    # retries it instead of skipping it forever.
                    if market is None:
                        continue
                    # Entry-only filters: exits are never blocked by these —
                    # mirroring a leader out of a position reduces our risk.
                    reason = self._entry_reject_reason(t, market, now)
                    if reason is not None:
                        self._processed_uids.add(t.uid)   # settled: never copyable
                        continue
                    drift_ok = self._price_drift_ok(t.token_id, t.price)
                    if drift_ok is None:
                        continue         # couldn't verify — transient, retry next cycle
                    self._processed_uids.add(t.uid)       # verified: decision is final
                    if not drift_ok:
                        continue         # edge already arbitraged away
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
                    # Exits are risk-reducing: a failed market lookup must
                    # never block one (only a definitively closed market does —
                    # settlement owns those).
                    if market is not None and market.closed:
                        self._processed_uids.add(t.uid)
                        continue
                    our_position = self.ledger.get_position(t.token_id)
                    if our_position is None or abs(our_position.shares) <= 1e-9:
                        self._processed_uids.add(t.uid)
                        continue                   # nothing of ours to mirror-sell
                    # Only THIS leader's slice is ours to exit. `positions`
                    # holds the combined total, so sizing off it let one
                    # leader's exit liquidate another leader's copy of the same
                    # token — a full exit by a leader who contributed 30% of
                    # our shares sold all 100%. Clamped to the real position,
                    # which settlement zeroes (settlement fills carry no
                    # source_leader, so the raw balance can outlive the shares).
                    held_total = abs(our_position.shares)
                    from_leader = self.ledger.copied_shares_for_leader(leader, t.token_id)
                    copied = min(max(0.0, from_leader), held_total)
                    if copied <= 1e-9:
                        self._processed_uids.add(t.uid)
                        continue                   # we hold this token, but not from them
                    # NB: a mirror-exit we yield is deliberately NOT retired
                    # here. The RiskManager can still reject it (e.g. the slice
                    # rounds under min_ticket_usd), and retiring it would drop
                    # the exit for good — `has_copied` already dedupes the ones
                    # that actually fill.
                    sell_fraction = 1.0 if prior_shares <= 1e-9 else min(1.0, t.shares / prior_shares)
                    sell_shares = copied * sell_fraction
                    yield Signal(
                        market_id=t.market_id,
                        token_id=t.token_id,
                        outcome=t.outcome,
                        side=Side.SELL,
                        target_price=t.price,
                        # Estimate, valued at our blended avg cost across every
                        # leader in the token; size_shares is what the executor
                        # actually fills on.
                        size_usd=sell_shares * our_position.avg_price,
                        size_shares=sell_shares,
                        reason=(f"copy {leader[:8]}… sell {sell_fraction*100:.0f}%"
                                + (" (exit-only)" if exit_only else "")),
                        source_leader=leader,
                        source_uid=t.uid,
                    )
