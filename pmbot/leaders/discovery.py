"""Leader discovery: profile every wallet visible in the top market feeds.

Polymarket's leaderboard endpoint isn't publicly documented/stable, so we
build our own candidate pool from market trade feeds. This module is the
first stage of the selection funnel:

  1. Pull deep trade feeds from the most active OPEN markets and the most
     recently RESOLVED high-activity markets (a few hundred API calls total,
     independent of how many wallets exist).
  2. Aggregate a `FeedProfile` per wallet: activity, recency, and an
     *estimated win quality* — exact realized outcomes on the resolved
     feed markets, plus mark-to-market edge (is the market currently past
     their entry?) on the open ones.

Scoring (`scoring.py`) then spends the expensive per-wallet deep scoring
only on the wallets whose feed profile already looks like winning — instead
of on whoever happens to sort first alphabetically or trade the loudest.

The profile is a SHORTLISTING signal, not a filter: feeds only show a
wallet's trades in the sampled markets, so numbers here are partial. All
real eligibility checks happen in the deep-scoring pass on the wallet's own
full tape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.models import Market

log = logging.getLogger(__name__)

# Quality blend: exact resolved outcomes are worth more than mark-to-market
# paper edge (which can still reverse before resolution).
_W_RESOLVED = 0.7
_W_MTM = 0.3

_PAGE = 500  # Data API max page size for /trades


def _shrunk_win_frac(wins: int, losses: int) -> float:
    """Laplace-smoothed win fraction: no data -> 0.5 (neutral), and a 2-0
    record (~0.75) can't outrank a 20-2 record (~0.88) on luck alone."""
    return (wins + 1) / (wins + losses + 2)


@dataclass
class FeedProfile:
    """What the market feeds show about one wallet (partial by design)."""

    wallet: str
    n_trades: int = 0
    notional_usd: float = 0.0
    last_ts: datetime | None = None
    resolved_wins: int = 0       # feed markets that resolved in their favor
    resolved_losses: int = 0
    resolved_pnl: float = 0.0    # exact P&L on feed-visible resolved trades
    mtm_wins: int = 0            # open feed markets currently past their entry
    mtm_losses: int = 0
    mtm_pnl: float = 0.0

    @property
    def quality(self) -> float:
        """Estimated win quality in [0, 1]; the funnel's shortlist key."""
        resolved = _shrunk_win_frac(self.resolved_wins, self.resolved_losses)
        mtm = _shrunk_win_frac(self.mtm_wins, self.mtm_losses)
        return _W_RESOLVED * resolved + _W_MTM * mtm


@dataclass
class _MarketTally:
    """Per-(wallet, market) P&L accumulator, collapsed to a win/loss later."""

    pnl: float = 0.0
    trades: int = 0


def _fetch_market_trades(
    data: PolymarketDataClient, condition_id: str, cap: int
) -> list[dict]:
    """Newest-first raw trades for a market, paginated up to `cap`."""
    out: list[dict] = []
    offset = 0
    while len(out) < cap:
        want = min(_PAGE, cap - len(out))
        page = data.get_raw_trades(market=condition_id, limit=want, offset=offset)
        out.extend(page)
        if len(page) < want:
            break
        offset += len(page)
    return out


def _winner_token(m: Market) -> str | None:
    """Winning token id of a decisively resolved market, from final prices."""
    if not m.closed or not m.outcome_prices:
        return None
    token, price = max(m.outcome_prices.items(), key=lambda kv: kv[1])
    return token if price >= 0.99 else None


def _recent_resolved_markets(
    gamma: GammaClient, *, limit: int, lookback_days: int,
    now: datetime | None = None,
) -> list[Market]:
    """Recently, decisively resolved markets — the exact-outcome sample.

    `now` is injectable so callers (and tests) can pin the window; the rest of
    the funnel already threads a clock this way. Reading the wall clock here
    unconditionally made any test with dated fixtures pass only until real time
    drifted `lookback_days` past them.
    """
    if limit <= 0:
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=lookback_days)
    try:
        # endDate ordering surfaces the freshest resolutions; fall back to
        # volume ordering if Gamma rejects the order key.
        try:
            markets = gamma.get_markets(
                limit=limit * 3, closed=True, active=None,
                order="endDate", ascending=False,
            )
        except Exception:
            markets = gamma.get_markets(limit=limit * 3, closed=True, active=None)
    except Exception as e:
        log.warning("discovery: failed to list resolved markets: %s", e)
        return []

    keep: list[Market] = []
    for m in markets:
        if not m.market_id or _winner_token(m) is None:
            continue
        if m.end_date is not None and m.end_date < cutoff:
            continue
        keep.append(m)
        if len(keep) >= limit:
            break
    return keep


def profile_candidates(
    data: PolymarketDataClient,
    gamma: GammaClient,
    *,
    top_open_markets: int = 30,
    top_closed_markets: int = 30,
    per_market_trades: int = 1000,
    lookback_days: int = 30,
    now: datetime | None = None,
) -> dict[str, FeedProfile]:
    """Build a FeedProfile for every wallet seen in the sampled feeds."""
    profiles: dict[str, FeedProfile] = {}
    resolved_tally: dict[tuple[str, str], _MarketTally] = {}
    mtm_tally: dict[tuple[str, str], _MarketTally] = {}

    try:
        open_markets = gamma.get_markets(limit=top_open_markets)
    except Exception as e:
        log.warning("discovery: failed to list open markets: %s", e)
        open_markets = []
    closed_markets = _recent_resolved_markets(
        gamma, limit=top_closed_markets, lookback_days=lookback_days, now=now
    )

    def profile(wallet: str) -> FeedProfile:
        p = profiles.get(wallet)
        if p is None:
            p = profiles[wallet] = FeedProfile(wallet=wallet)
        return p

    def ingest(m: Market, *, winner: str | None) -> None:
        try:
            trades = _fetch_market_trades(data, m.market_id, per_market_trades)
        except Exception as e:
            log.debug("discovery: feed fetch failed for %s: %s", m.market_id[:12], e)
            return
        for t in trades:
            wallet = str(t.get("proxyWallet") or "").lower()
            token = str(t.get("asset") or "")
            if not wallet or not token:
                continue
            price = _f(t.get("price"))
            size = _f(t.get("size"))
            is_buy = str(t.get("side", "BUY")).upper() != "SELL"
            ts = int(t.get("timestamp") or 0)

            p = profile(wallet)
            p.n_trades += 1
            p.notional_usd += size * price
            when = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
            if when and (p.last_ts is None or when > p.last_ts):
                p.last_ts = when

            if winner is not None:
                payout = 1.0 if token == winner else 0.0
                pnl = (payout - price) * size if is_buy else (price - payout) * size
                tally = resolved_tally.setdefault((wallet, m.market_id), _MarketTally())
            else:
                cur = (m.outcome_prices or {}).get(token)
                if cur is None:
                    continue
                pnl = (cur - price) * size if is_buy else (price - cur) * size
                tally = mtm_tally.setdefault((wallet, m.market_id), _MarketTally())
            tally.pnl += pnl
            tally.trades += 1

    for m in closed_markets:
        ingest(m, winner=_winner_token(m))
    for m in open_markets:
        if m.market_id:
            ingest(m, winner=None)

    # Collapse per-market tallies to win/loss counts (a market is one "bet").
    for (wallet, _mkt), tally in resolved_tally.items():
        p = profiles[wallet]
        p.resolved_pnl += tally.pnl
        if tally.pnl > 0:
            p.resolved_wins += 1
        elif tally.pnl < 0:
            p.resolved_losses += 1
    for (wallet, _mkt), tally in mtm_tally.items():
        p = profiles[wallet]
        p.mtm_pnl += tally.pnl
        if tally.pnl > 0:
            p.mtm_wins += 1
        elif tally.pnl < 0:
            p.mtm_losses += 1

    log.info(
        "discovery: profiled %d wallets from %d open + %d resolved market feeds",
        len(profiles), len(open_markets), len(closed_markets),
    )
    return profiles


def harvest_candidates(
    data: PolymarketDataClient,
    gamma: GammaClient,
    *,
    top_markets: int = 8,
    per_market: int = 50,
) -> set[str]:
    """Legacy shallow harvest (addresses only). Kept for research scripts;
    the live selector uses `profile_candidates`."""
    wallets: set[str] = set()
    try:
        markets = gamma.get_markets(limit=top_markets)
    except Exception as e:
        log.warning("discovery: failed to list markets: %s", e)
        return wallets

    for m in markets:
        if not m.market_id:
            continue
        try:
            for w in data.harvest_wallets_from_market(m.market_id, limit=per_market):
                if w:
                    wallets.add(w.lower())
        except Exception as e:
            log.debug("discovery: harvest failed for %s: %s", m.market_id[:12], e)
            continue

    log.info("discovery: harvested %d candidate wallets from %d markets",
             len(wallets), len(markets))
    return wallets


def _f(v: object, default: float = 0.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
