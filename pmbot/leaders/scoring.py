"""Score candidate wallets and pick the top-N leaders to copy.

`compute_wallet_stats` is pure (inject a `resolver`) so the P&L reconstruction
is unit-testable offline. `LeaderSelector` wires it to the live APIs.

Reconstruction approach: replay a wallet's trades through the same signed
average-cost accounting the ledger uses (`apply_fill`). Sells realize P&L vs.
prior buys; positions still open in a *resolved* market are settled at the
outcome payout (1.0 winner / 0.0 loser). Win rate is over resolved markets.

Honest caveats (refined in Strategy #2):
- "categories" uses distinct event groups (event_slug) as a breadth proxy.
- win-rate is only enforced once enough markets have resolved.
- only the wallet's most recent `trades_limit` trades are considered.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.leaders.config import FilterConfig, LeaderConfig, load_leader_config
from pmbot.leaders.discovery import harvest_candidates
from pmbot.models import LeaderTrade
from pmbot.portfolio.ledger import apply_fill

log = logging.getLogger(__name__)

# resolver(condition_id) -> (closed, winning_token_id)
Resolver = Callable[[str], tuple[bool, str | None]]


@dataclass
class WalletStats:
    wallet: str
    n_trades: int
    n_markets: int
    n_resolved_markets: int
    realized_pnl: float
    win_rate: float
    n_categories: int
    recency_days: float
    concentration: float


@dataclass
class LeaderScore:
    wallet: str
    score: float
    stats: WalletStats


def compute_wallet_stats(
    wallet: str,
    trades: list[LeaderTrade],
    resolver: Resolver,
    now: datetime | None = None,
    *,
    copyable_trades_only: bool = True,
    price_min: float = 0.05,
    price_max: float = 0.95,
) -> WalletStats:
    now = now or datetime.now(timezone.utc)
    trades = sorted(trades, key=lambda t: t.timestamp)

    # Filter to copyable trades only (Strategy #4 criteria: BUY side + price band)
    if copyable_trades_only:
        from pmbot.models import Side
        trades_filtered = []
        for t in trades:
            if t.side is not Side.BUY:
                continue
            if not (price_min <= t.price <= price_max):
                continue
            trades_filtered.append(t)
        trades = trades_filtered

    pos: dict[str, tuple[float, float]] = {}      # token -> (shares, avg)
    token_market: dict[str, str] = {}
    market_realized: dict[str, float] = defaultdict(float)
    market_gross: dict[str, float] = defaultdict(float)
    categories: set[str] = set()
    last_ts: datetime | None = None

    for t in trades:
        if not t.token_id or not t.market_id:
            continue
        token_market[t.token_id] = t.market_id
        market_gross[t.market_id] += t.usd_size
        if t.event_slug:
            categories.add(t.event_slug)
        shares, avg = pos.get(t.token_id, (0.0, 0.0))
        eff = apply_fill(shares, avg, t.side, t.shares, t.price)
        pos[t.token_id] = (eff.new_shares, eff.new_avg)
        market_realized[t.market_id] += eff.realized_delta
        last_ts = t.timestamp

    # Settle still-open positions in resolved markets; mark resolved markets.
    resolved: set[str] = set()
    resolution_cache: dict[str, tuple[bool, str | None]] = {}
    for market in market_gross:
        closed, winner = resolution_cache.setdefault(market, resolver(market))
        if closed:
            resolved.add(market)
    for token, (shares, avg) in pos.items():
        if abs(shares) <= 1e-9:
            continue
        market = token_market[token]
        closed, winner = resolution_cache.get(market, (False, None))
        if closed and winner is not None:
            payout = 1.0 if token == winner else 0.0
            market_realized[market] += (payout - avg) * shares

    realized_pnl = sum(market_realized.values())
    n_resolved = len(resolved)
    wins = sum(1 for m in resolved if market_realized[m] > 0)
    win_rate = wins / n_resolved if n_resolved else 0.0
    total_gross = sum(market_gross.values())
    concentration = (max(market_gross.values()) / total_gross) if total_gross else 0.0
    recency_days = (now - last_ts).total_seconds() / 86_400 if last_ts else 1e9

    return WalletStats(
        wallet=wallet,
        n_trades=len(trades),
        n_markets=len(market_gross),
        n_resolved_markets=n_resolved,
        realized_pnl=realized_pnl,
        win_rate=win_rate,
        n_categories=len(categories),
        recency_days=recency_days,
        concentration=concentration,
    )


def passes_filters(st: WalletStats, f: FilterConfig) -> bool:
    if st.n_trades < f.min_resolved_trades:
        return False
    # Require a real resolved track record (rejects high-volume wallets with no
    # settled outcomes — they have $0 realized P&L and an unverifiable win rate).
    if st.n_resolved_markets < f.min_resolved_markets:
        return False
    if st.realized_pnl < f.min_realized_pnl_usd:
        return False
    if st.n_categories < f.min_distinct_categories:
        return False
    if st.concentration > f.max_position_concentration:
        return False
    # Only enforce win-rate once we have a meaningful resolved sample.
    if st.n_resolved_markets >= 5 and st.win_rate < f.min_win_rate:
        return False
    return True


def rank_wallets(stats: list[WalletStats], weights: dict[str, float]) -> list[LeaderScore]:
    if not stats:
        return []

    def norm(values: list[float]) -> list[float]:
        lo, hi = min(values), max(values)
        if hi - lo < 1e-12:
            return [0.5] * len(values)
        return [(v - lo) / (hi - lo) for v in values]

    pnl = norm([s.realized_pnl for s in stats])
    wr = norm([s.win_rate for s in stats])
    cons = norm([float(s.n_categories) for s in stats])
    rec = norm([-s.recency_days for s in stats])   # more recent -> higher

    out: list[LeaderScore] = []
    for i, s in enumerate(stats):
        score = (
            weights.get("realized_pnl", 0) * pnl[i]
            + weights.get("win_rate", 0) * wr[i]
            + weights.get("consistency", 0) * cons[i]
            + weights.get("recency", 0) * rec[i]
        )
        out.append(LeaderScore(wallet=s.wallet, score=score, stats=s))
    out.sort(key=lambda x: x.score, reverse=True)
    return out


class LeaderSelector:
    """Orchestrates discovery -> stats -> filter -> rank -> top-N (live)."""

    def __init__(
        self,
        data: PolymarketDataClient,
        gamma: GammaClient,
        config: LeaderConfig | None = None,
        *,
        max_candidates: int = 30,
        trades_limit: int = 300,
        top_markets: int = 8,
        per_market: int = 50,
    ):
        self.data = data
        self.gamma = gamma
        self.config = config or load_leader_config()
        self.max_candidates = max_candidates
        self.trades_limit = trades_limit
        self.top_markets = top_markets
        self.per_market = per_market
        self._resolution_memo: dict[str, tuple[bool, str | None]] = {}

    def _resolver(self, condition_id: str) -> tuple[bool, str | None]:
        if condition_id in self._resolution_memo:
            return self._resolution_memo[condition_id]
        try:
            res = self.gamma.get_resolution(condition_id)
        except Exception:
            res = (False, None)
        self._resolution_memo[condition_id] = res
        return res

    def select(self, now: datetime | None = None) -> list[LeaderScore]:
        now = now or datetime.now(timezone.utc)
        cfg = self.config

        candidates = harvest_candidates(
            self.data, self.gamma, top_markets=self.top_markets, per_market=self.per_market
        )
        candidates |= set(cfg.allowlist)
        candidates -= set(cfg.blocklist)
        ordered = sorted(candidates)[: self.max_candidates]

        cutoff = now - timedelta(days=cfg.filters.lookback_days)
        stats: list[WalletStats] = []
        for wallet in ordered:
            try:
                trades = self.data.get_trades(user=wallet, limit=self.trades_limit)
            except Exception as e:
                log.debug("scoring: trades fetch failed for %s: %s", wallet[:10], e)
                continue
            trades = [t for t in trades if t.timestamp >= cutoff]
            if not trades:
                continue
            stats.append(compute_wallet_stats(
                wallet, trades, self._resolver, now=now,
                copyable_trades_only=cfg.filters.copyable_trades_only,
                price_min=cfg.filters.copy_price_min,
                price_max=cfg.filters.copy_price_max,
            ))

        eligible = [s for s in stats if passes_filters(s, cfg.filters)]
        # Allowlisted wallets bypass filters entirely.
        allow = set(cfg.allowlist)
        for s in stats:
            if s.wallet in allow and s not in eligible:
                eligible.append(s)

        ranked = rank_wallets(eligible, cfg.weights)
        log.info("scoring: %d candidates -> %d eligible -> top %d",
                 len(stats), len(eligible), cfg.selection.top_n)
        return ranked[: cfg.selection.top_n]
