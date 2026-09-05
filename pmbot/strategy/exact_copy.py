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
  - the leader hasn't already fully exited it inside the same fetched tape
    (`skip_round_tripped_entries`) — both halves of a completed round-trip
    arrive in one poll cycle, and copying the entry then immediately
    mirroring the exit fills both legs at the same current price, banking the
    spread as a loss and none of the leader's move

BUY: same shape as a fresh entry, sized by the RiskManager off leader notional.
SELL: mirrored *proportionally*, and only over OUR OWN slice from that leader.
Two independent quantities drive this:
  - how much of THEIR position the leader exited — we track each leader's
    running per-token share count from the trades we've observed
    (`observe_leader_fill`, shared with the backtester), so a partial trim
    becomes a partial exit rather than a full liquidation. The count never
    goes negative: Polymarket has no naked shorts, so a sell we can't account
    for means the buy predates us. When the observed count is zero their prior
    position is unknown, and we conservatively treat the sell as a full exit.
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
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone

from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient, PriceCache
from pmbot.models import LeaderTrade, Market, Side, Signal
from pmbot.portfolio.ledger import Ledger
from pmbot.strategy.base import Strategy

log = logging.getLogger(__name__)

# Polymarket reports share counts truncated to 2 decimals, so a leader's "full"
# exit routinely sells fractionally fewer shares than the entry bought and
# leaves a sub-0.01 crumb behind. Treat a position this small as flat.
EXIT_CRUMB_SHARES = 0.01


def observe_leader_fill(prior_shares: float, side: Side, shares: float) -> float:
    """The leader's share count after this trade — never negative.

    THE source of truth for reconstructing a watched wallet's position; the
    backtester imports it so live and simulated exits cannot drift apart.

    This deliberately does NOT use `apply_fill`. That function is the ledger's
    *trading* accounting, built for a book that can go short: a SELL from zero
    opens a negative position. Polymarket has no naked shorts — a sell we can't
    account for means the buy predates our observation, not that the leader is
    short — and the negative then persisted through later buys, so
    `sell_fraction` fell to its "unknown prior -> full exit" branch on every
    subsequent trim. Clamping at zero is both correct and self-consistent: we
    only ever bought alongside observed BUYs, so an observed count of zero
    means they have sold everything we mirrored, and a full exit IS right.
    """
    delta = shares if side is Side.BUY else -shares
    return max(0.0, prior_shares + delta)


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
        self.sweep_exit_dust = s.sweep_exit_dust
        self.exit_dust_usd = s.exit_dust_usd
        self.skip_round_tripped_entries = s.skip_round_tripped_entries
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
        # uid -> the leader's share count immediately BEFORE that trade (SELLs
        # only). Lets a re-decided exit size off what we observed at the time
        # instead of reconstructing it; see `generate`.
        self._seen_priors: dict[str, float] = {}
        # Why we did NOT copy something, counted per cycle. The backtester has
        # reported this for months (`BacktestReport.skipped`) while the live
        # bot reported nothing, so the two could only ever be compared on the
        # trades that got through — and the interesting difference is in the
        # ones that don't. Reset at the top of every `generate()`; the engine
        # prints it. Only decisions taken THIS cycle are counted: a uid already
        # in `_processed_uids` returns before any of these.
        self.skips: Counter[str] = Counter()
        try:
            self.ledger.prune_seen_trades(older_than_days=30.0)
            self._leader_shares = self.ledger.load_leader_positions()
            self._seen_uids = self.ledger.load_seen_uids()
            self._seen_priors = self.ledger.load_seen_priors()
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

        The codes are deliberately the same strings `simulate` counts in
        `BacktestReport.skipped`, so a live cycle's refusal mix can be read
        straight against a backtest's without a translation table.

        Every reason here is settled for the life of the trade — the leader's
        price and notional are fixed, a closed market never reopens, and both
        the trade's age and the market's time-to-resolution only move one way —
        so the caller may retire the uid. (Thin liquidity can in principle
        recover, but not meaningfully inside the max-trade-age window.)
        Transient conditions — a failed market lookup, an unverifiable quote —
        are deliberately NOT decided here.
        """
        if market.closed:
            return "market_closed"
        if not (self.price_min <= t.price <= self.price_max):
            return "price_band"                   # little edge at the extremes
        if t.usd_size < self.min_leader_notional:
            return "leader_notional"              # low-conviction probe
        if self.max_trade_age_minutes > 0:
            age_min = (now - t.timestamp).total_seconds() / 60.0
            if age_min > self.max_trade_age_minutes:
                return "trade_too_old"            # stale history, not a fresh signal
        if market.liquidity_usd is not None and market.liquidity_usd < self.min_liquidity:
            return "liquidity"
        if self.min_hours_to_resolution > 0 and market.end_date is not None:
            hours_left = (market.end_date - now).total_seconds() / 3600
            if hours_left < self.min_hours_to_resolution:
                return "horizon"                  # lag is adverse here
        return None

    @staticmethod
    def _round_tripped_entry_uids(ordered: list[LeaderTrade]) -> set[str]:
        """UIDs of BUYs the leader has already fully exited *within this tape*.

        One `get_trades` window routinely holds both halves of a completed
        round-trip — increasingly so since the notional floor dropped to $50
        and began admitting fast in-game scalps. Replaying both halves in a
        single poll cycle opens and closes our copy at the same current price:
        we capture none of the leader's move and pay the spread twice. The age
        filter can't catch these — the entry really is minutes old, it is just
        already dead.

        Only FULL exits retire an entry. A leader who trimmed and still holds
        is still expressing conviction, and mirroring that is the strategy
        working as intended.
        """
        held: dict[str, float] = {}
        bought: dict[str, float] = {}          # gross shares opened in-window
        open_uids: dict[str, list[str]] = {}
        closed: set[str] = set()
        for t in ordered:
            prior = held.get(t.token_id, 0.0)
            if t.side is Side.BUY:
                held[t.token_id] = prior + t.shares
                bought[t.token_id] = bought.get(t.token_id, 0.0) + t.shares
                open_uids.setdefault(t.token_id, []).append(t.uid)
                continue
            # A sell whose position predates the window drives this negative;
            # clamp so it can't retire a later, unrelated entry.
            remaining = max(0.0, prior - t.shares)
            # A "full" exit undershoots by a truncation crumb (see
            # EXIT_CRUMB_SHARES), so an exact-zero test never fires. The old
            # 1e-6-relative epsilon was ~20x too tight to see one: a 183.314053
            # -share entry exits as 183.31, leaving 0.004053 against a 0.000183
            # threshold. Scale the tolerance for big positions, but never below
            # one crumb.
            if remaining <= max(EXIT_CRUMB_SHARES, 1e-4 * bought.get(t.token_id, 0.0)):
                # Snap to flat rather than carrying the crumb forward. Crumbs
                # accumulate — three round-trips on one token sum past any fixed
                # tolerance, and every later exit on that token then reads as
                # partial no matter how tidy it is.
                remaining = 0.0
                closed.update(open_uids.pop(t.token_id, []))
                bought.pop(t.token_id, None)
            held[t.token_id] = remaining
        return closed

    def generate(self) -> Iterable[Signal]:
        now = datetime.now(timezone.utc)
        self.skips.clear()
        watchlist = [(w, False) for w in self.leaders]
        watchlist += [(w, True) for w in self.exit_only_leaders]
        for leader, exit_only in watchlist:
            try:
                trades = self.data.get_trades(user=leader, limit=self.trades_per_leader)
            except Exception as e:
                log.debug("strategy: trades fetch failed for %s: %s", leader[:10], e)
                self.skips["tape_fetch_failed"] += 1
                continue
            # Oldest-first so leader position tracking replays in order.
            ordered = sorted(trades, key=lambda tr: tr.timestamp)
            round_tripped = (
                self._round_tripped_entry_uids(ordered)
                if self.skip_round_tripped_entries else set()
            )
            for t in ordered:
                try:
                    yield from self._signals_for_trade(
                        leader, exit_only, t, round_tripped, now
                    )
                except Exception as e:
                    # One trade must not take the watchlist down with it. Only
                    # `get_trades` was guarded before, so a ledger read that
                    # threw here ("database is locked" — the dashboard opens
                    # the same file every 20s) escaped `poll_once` into the
                    # engine's cycle backoff, and every leader after this one
                    # went unpolled for that cycle.
                    log.warning("strategy: skipped trade %s from %s: %s",
                                (t.uid or "?")[:12], leader[:10], e)

    def _signals_for_trade(
        self, leader: str, exit_only: bool, t: LeaderTrade,
        round_tripped: set[str], now: datetime,
    ) -> Iterable[Signal]:
        """Decide one observed leader trade: track it, then yield 0 or 1 signals."""
        key = (leader, t.token_id)
        if t.uid not in self._seen_uids:
            # Count toward leader position tracking exactly once per
            # trade uid — the same trade stays in the fetched window
            # across poll cycles, and re-applying it would double-count
            # the leader's position.
            prior_shares = self._leader_shares.get(key, 0.0)
            new_shares = observe_leader_fill(prior_shares, t.side, t.shares)
            self._seen_uids.add(t.uid)
            self._leader_shares[key] = new_shares
            if t.side is Side.SELL:
                self._seen_priors[t.uid] = prior_shares
            try:
                self.ledger.record_leader_observation(
                    leader, t.token_id, new_shares, t.uid, t.timestamp,
                    prior_shares=prior_shares if t.side is Side.SELL else None,
                )
            except Exception as e:
                # Both writes share one transaction, so a failure lands
                # neither and a restart replays this trade correctly —
                # but a DB that keeps failing must not do so silently.
                log.warning("strategy: leader-tracking persist failed: %s", e)
        else:
            # Already counted, and now being re-decided: a restart
            # (`_processed_uids` is in-memory), or an exit the
            # RiskManager rejected. Use the count recorded at first
            # observation. This used to reconstruct it as
            # `stored + t.shares`, which is only right for the LAST
            # sell on a token — with two sells in the window the
            # earlier partial trim read as a 100% exit.
            prior_shares = self._seen_priors.get(
                t.uid, self._leader_shares.get(key, 0.0)
            )

        if t.uid in self._processed_uids or self.ledger.has_copied(t.uid):
            return

        if exit_only and t.side is Side.BUY:
            self.skips["exit_only_buy"] += 1
            return                       # exit-only leader: mirror exits, never new entries

        if t.side is Side.BUY and t.uid in round_tripped:
            # Settled, and checked before the market lookup so a dead
            # entry costs no API call: the leader's exit is history and
            # can never un-happen, so this uid is never copyable.
            self._processed_uids.add(t.uid)
            self.skips["round_tripped"] += 1
            return

        market = self._market(t.market_id)

        if t.side is Side.BUY:
            # Entries need live market metadata; a TRANSIENT lookup
            # failure leaves the trade unprocessed so the next cycle
            # retries it instead of skipping it forever.
            if market is None:
                self.skips["market_lookup_failed"] += 1
                return
            # Entry-only filters: exits are never blocked by these —
            # mirroring a leader out of a position reduces our risk.
            reason = self._entry_reject_reason(t, market, now)
            if reason is not None:
                self._processed_uids.add(t.uid)   # settled: never copyable
                self.skips[reason] += 1
                return
            drift_ok = self._price_drift_ok(t.token_id, t.price)
            if drift_ok is None:
                self.skips["quote_unverified"] += 1
                return         # couldn't verify — transient, retry next cycle
            self._processed_uids.add(t.uid)       # verified: decision is final
            if not drift_ok:
                # Read this one against the lag: by the time the feed shows us
                # the trade it is ~150s old (`copy_feed_lag_seconds`), so this
                # counts prices that moved in those 150s, not in our 10s poll.
                self.skips["price_drift"] += 1
                return         # edge already arbitraged away
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
                self.skips["exit_market_closed"] += 1
                return
            our_position = self.ledger.get_position(t.token_id)
            if our_position is None or abs(our_position.shares) <= 1e-9:
                self._processed_uids.add(t.uid)
                self.skips["exit_no_position"] += 1
                return                   # nothing of ours to mirror-sell
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
                self.skips["exit_not_from_this_leader"] += 1
                return                   # we hold this token, but not from them
            # NB: a mirror-exit we yield is deliberately NOT retired
            # here. The RiskManager can still reject it (e.g. the slice
            # rounds under min_ticket_usd), and retiring it would drop
            # the exit for good — `has_copied` already dedupes the ones
            # that actually fill.
            sell_fraction = 1.0 if prior_shares <= 1e-9 else min(1.0, t.shares / prior_shares)
            sell_shares = copied * sell_fraction
            # The leader's exit ratio is rarely exactly 1.0, so a "full"
            # exit can leave a sub-cent crumb that the ledger counts as
            # an open position forever. Sweep it — but only up to
            # `copied`, this leader's own slice, never `held_total`:
            # snapping to the combined position is precisely how one
            # leader's exit would liquidate another's copy.
            if self.sweep_exit_dust:
                residual_value = (copied - sell_shares) * our_position.avg_price
                if 0 < residual_value < self.exit_dust_usd:
                    sell_shares = copied
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
