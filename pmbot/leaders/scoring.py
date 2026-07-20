"""Score candidate wallets and pick the top-N leaders to copy.

The selection funnel (LeaderSelector.select):

  1. PROFILE the whole pool from market feeds (discovery.profile_candidates)
     — a few hundred API calls regardless of pool size.
  2. SHORTLIST the wallets whose feed profile already shows winning behavior
     (plus incumbents, the allowlist, and a random exploration slice so we
     are never systematically blind to wallets the feeds under-sample).
  3. DEEP-SCORE the shortlist in parallel from each wallet's own full tape,
     with cheap early exits (dead or too-quiet wallets are rejected from the
     first page, before any resolution lookups).
  4. FILTER -> RANK -> top-N, and keep a rejection report (`last_report`)
     showing what each stage and each filter eliminated.

`compute_wallet_stats` is pure (inject a `resolver`) so the P&L
reconstruction is unit-testable offline.

Reconstruction approach: replay a wallet's trades through the same signed
average-cost accounting the ledger uses (`apply_fill`). Sells realize P&L vs.
prior buys; positions still open in a *resolved* market are settled at the
outcome payout (1.0 winner / 0.0 loser). Win rate is over resolved markets.

Honest caveats:
- "categories" uses distinct event groups (event_slug) as a breadth proxy.
- the Data API silently truncates deep tapes (~1-2k trades), so hyperactive
  wallets' windows can be partially covered even with pagination.
"""

from __future__ import annotations

import logging
import random
import threading
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.data.resolution_cache import ResolutionStore
from pmbot.leaders.config import FilterConfig, LeaderConfig, load_leader_config
from pmbot.leaders.discovery import profile_candidates
from pmbot.models import LeaderTrade, Side
from pmbot.portfolio.ledger import apply_fill

log = logging.getLogger(__name__)

# resolver(condition_id) -> (closed, winning_token_id)
Resolver = Callable[[str], tuple[bool, str | None]]

# Early-exit reject reasons (cheap checks that skip resolution lookups).
# They reuse filter names so the rejection report reads as one story.
_REJECT_TRADES = "min_trades"
_REJECT_RECENCY = "recency"
_REJECT_COPYABLE = "copyable_trades"
_REJECT_ERROR = "fetch_error"


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
    # Largest single market's profit / total profit (0 when not net-positive;
    # can exceed 1.0 when one big win is propping up net losers elsewhere).
    profit_concentration: float
    # BUYs the copy strategy would actually mirror (notional + price band).
    n_copyable_trades: int = 0


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
    copyable_notional_min: float = 0.0,
    copyable_price_min: float = 0.0,
    copyable_price_max: float = 1.0,
) -> WalletStats:
    now = now or datetime.now(timezone.utc)
    trades = sorted(trades, key=lambda t: t.timestamp)
    n_copyable = sum(
        1 for t in trades
        if t.side is Side.BUY
        and t.usd_size >= copyable_notional_min
        and copyable_price_min <= t.price <= copyable_price_max
    )

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
        if market not in resolution_cache:
            resolution_cache[market] = resolver(market)
        if resolution_cache[market][0]:
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
    best_market = max((v for v in market_realized.values() if v > 0), default=0.0)
    profit_concentration = best_market / realized_pnl if realized_pnl > 0 else 0.0
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
        profit_concentration=profit_concentration,
        n_copyable_trades=n_copyable,
    )


def failing_filters(st: WalletStats, f: FilterConfig) -> list[str]:
    """Every filter this wallet fails (empty = eligible). Names match the
    yaml knobs so the rejection report maps 1:1 to what you'd tune."""
    fails: list[str] = []
    if st.n_trades < f.min_trades:
        fails.append("min_trades")
    if st.recency_days * 24.0 > f.max_hours_since_last_trade:
        fails.append("recency")
    if st.realized_pnl < f.min_realized_pnl_usd:
        fails.append("pnl")
    if st.n_categories < f.min_distinct_categories:
        fails.append("categories")
    if st.n_resolved_markets < f.min_resolved_markets:
        fails.append("resolved_markets")
    if st.win_rate < f.min_win_rate:
        fails.append("win_rate")
    if st.profit_concentration > f.max_profit_concentration:
        fails.append("profit_concentration")
    if st.n_copyable_trades < f.min_copyable_trades:
        fails.append("copyable_trades")
    return fails


def passes_filters(st: WalletStats, f: FilterConfig) -> bool:
    return not failing_filters(st, f)


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
    """Orchestrates profile -> shortlist -> deep-score -> filter -> rank (live)."""

    def __init__(
        self,
        data: PolymarketDataClient,
        gamma: GammaClient,
        config: LeaderConfig | None = None,
        *,
        resolution_store: ResolutionStore | None = None,
        # 1000 (was 300): a qualifying wallet fell off the 350 cut between
        # two 2026-07-18 runs purely from feed churn. Early exits + the warm
        # resolution cache make the wider sweep cost ~a minute, not hours.
        deep_score_limit: int = 1000,
        explore_n: int = 50,
        feed_min_trades: int = 3,
        trades_page: int = 500,
        trades_cap: int = 1500,
        top_open_markets: int = 30,
        top_closed_markets: int = 30,
        per_market_trades: int = 1000,
        max_workers: int = 6,
        copy_notional_min: float | None = None,
        copy_price_min: float | None = None,
        copy_price_max: float | None = None,
    ):
        self.data = data
        self.gamma = gamma
        self.config = config or load_leader_config()
        # "Copyable" mirrors the strategy's own entry gates, so the leader
        # finder can't select wallets the strategy would then ignore.
        s = get_settings()
        self.copy_notional_min = (
            s.copy_min_leader_notional_usd if copy_notional_min is None else copy_notional_min
        )
        self.copy_price_min = s.copy_price_min if copy_price_min is None else copy_price_min
        self.copy_price_max = s.copy_price_max if copy_price_max is None else copy_price_max
        self.store = resolution_store
        self.deep_score_limit = deep_score_limit
        self.explore_n = explore_n
        self.feed_min_trades = feed_min_trades
        self.trades_page = trades_page
        self.trades_cap = trades_cap
        self.top_open_markets = top_open_markets
        self.top_closed_markets = top_closed_markets
        self.per_market_trades = per_market_trades
        self.max_workers = max_workers
        self.last_report: dict = {}
        self._memo: dict[str, tuple[bool, str | None]] = {}
        self._memo_lock = threading.Lock()
        self._new_terminal: dict[str, str] = {}

    # -- resolution lookups (thread-safe, staleness-proof) ----------------
    def _resolver(self, condition_id: str) -> tuple[bool, str | None]:
        with self._memo_lock:
            hit = self._memo.get(condition_id)
        if hit is not None:
            return hit
        try:
            res = self.gamma.get_resolution(condition_id)
        except Exception:
            # Transient failure: report "open" for this call but do NOT cache
            # it — a cached error would permanently hide a resolved market.
            return (False, None)
        with self._memo_lock:
            self._memo[condition_id] = res
            if res[0] and res[1] is not None:
                self._new_terminal[condition_id] = res[1]
        return res

    # -- deep scoring -----------------------------------------------------
    def _fetch_window(self, wallet: str, cutoff: datetime) -> list[LeaderTrade]:
        """Newest-first paginated tape, stopping once past the window."""
        out: list[LeaderTrade] = []
        offset = 0
        while len(out) < self.trades_cap:
            want = min(self.trades_page, self.trades_cap - len(out))
            page = self.data.get_trades(user=wallet, limit=want, offset=offset)
            if not page:
                break
            out.extend(page)
            if len(page) < want:
                break
            if min(t.timestamp for t in page) < cutoff:
                break                      # already reached beyond the window
            offset += len(page)
        return out

    def _deep_score(
        self, wallet: str, cutoff: datetime, now: datetime
    ) -> tuple[str, WalletStats | None, str | None]:
        """Return (wallet, stats, early_reject_reason). Early rejects skip
        all resolution lookups — that's what makes a wide shortlist cheap."""
        f = self.config.filters
        try:
            trades = self._fetch_window(wallet, cutoff)
        except Exception as e:
            log.debug("scoring: tape fetch failed for %s: %s", wallet[:10], e)
            return (wallet, None, _REJECT_ERROR)
        if not trades:
            return (wallet, None, _REJECT_TRADES)
        newest = max(t.timestamp for t in trades)
        if (now - newest).total_seconds() / 3600.0 > f.max_hours_since_last_trade:
            return (wallet, None, _REJECT_RECENCY)
        recent = [t for t in trades if t.timestamp >= cutoff]
        if len(recent) < f.min_trades:
            return (wallet, None, _REJECT_TRADES)
        n_copyable = sum(
            1 for t in recent
            if t.side is Side.BUY
            and t.usd_size >= self.copy_notional_min
            and self.copy_price_min <= t.price <= self.copy_price_max
        )
        if n_copyable < f.min_copyable_trades:
            # Tape-only check, so it runs BEFORE resolution lookups: the
            # hyperactive penny-bet bots are exactly the wallets with the
            # most markets to resolve — rejecting them here is the big saver.
            return (wallet, None, _REJECT_COPYABLE)
        st = compute_wallet_stats(
            wallet, recent, self._resolver, now=now,
            copyable_notional_min=self.copy_notional_min,
            copyable_price_min=self.copy_price_min,
            copyable_price_max=self.copy_price_max,
        )
        return (wallet, st, None)

    # -- the funnel -------------------------------------------------------
    def select(
        self, now: datetime | None = None, *, incumbents: list[str] | None = None
    ) -> list[LeaderScore]:
        now = now or datetime.now(timezone.utc)
        cfg = self.config
        allow = set(cfg.allowlist)
        block = set(cfg.blocklist)

        # Stage 1: profile the whole pool from feeds.
        profiles = profile_candidates(
            self.data,
            self.gamma,
            top_open_markets=self.top_open_markets,
            top_closed_markets=self.top_closed_markets,
            per_market_trades=self.per_market_trades,
            lookback_days=cfg.filters.lookback_days,
        )
        pool = (set(profiles) | allow) - block

        # Stage 2: shortlist by estimated win quality — NOT by activity, which
        # would hand the deep-score slots to high-frequency market-maker bots.
        active = [
            w for w in pool
            if w in profiles and profiles[w].n_trades >= self.feed_min_trades
        ]
        shortlist = sorted(active, key=lambda w: profiles[w].quality, reverse=True)
        chosen = set(shortlist[: self.deep_score_limit])
        chosen |= {w.lower() for w in (incumbents or [])} - block
        chosen |= allow
        rest = sorted(pool - chosen)
        if self.explore_n and rest:
            chosen |= set(random.sample(rest, min(self.explore_n, len(rest))))

        # Stage 3: deep-score in parallel. Rebuild the memo from the store
        # each run: terminal results persist forever, open markets re-check.
        self._memo = dict(self.store.load()) if self.store else {}
        self._new_terminal = {}
        cutoff = now - timedelta(days=cfg.filters.lookback_days)
        targets = sorted(chosen)
        if self.max_workers > 1 and len(targets) > 1:
            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                results = list(ex.map(lambda w: self._deep_score(w, cutoff, now), targets))
        else:
            results = [self._deep_score(w, cutoff, now) for w in targets]
        if self.store and self._new_terminal:
            self.store.save_many(self._new_terminal)

        # Stage 4: filter -> rank -> top-N, with the rejection report.
        early = Counter(reason for _, _, reason in results if reason)
        stats = [st for _, st, _ in results if st is not None]
        filter_fails: Counter[str] = Counter()
        eligible: list[WalletStats] = []
        for st in stats:
            fails = failing_filters(st, cfg.filters)
            if not fails:
                eligible.append(st)
            else:
                filter_fails.update(fails)
        # Allowlisted wallets bypass filters entirely (still need a tape).
        for st in stats:
            if st.wallet in allow and st not in eligible:
                eligible.append(st)

        ranked = rank_wallets(eligible, cfg.weights)
        top = ranked[: cfg.selection.top_n]
        rejects = Counter(early)
        rejects.update(filter_fails)
        self.last_report = {
            "pool": len(pool),
            "deep_scored": len(targets),
            "early_rejects": dict(early),
            "filter_rejects": dict(filter_fails),
            "rejects": dict(rejects),
            "eligible": len(eligible),
            "followed": len(top),
        }
        log.info(
            "scoring: %d pool -> %d deep-scored -> %d eligible -> top %d",
            len(pool), len(targets), len(eligible), len(top),
        )
        return top
