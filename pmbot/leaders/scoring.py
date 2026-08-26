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
prior buys — clamped to the shares actually tracked in the window, since
Polymarket has no naked shorts and a sell-from-zero can only be the exit of a
pre-window position (scoring it as a short would fake losses on winners).
Positions still open in a *resolved* market are settled at the outcome payout
(1.0 winner / 0.0 loser). Win rate is over resolved markets.

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
from pmbot.leaders.discovery import _shrunk_win_frac, profile_candidates
from pmbot.leaders.records import RecordStore, harvest_resolved_records
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
_REJECT_TAPE_SPAN = "tape_span"
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
        last_ts = t.timestamp              # any trade counts as activity/recency
        shares, avg = pos.get(t.token_id, (0.0, 0.0))
        fill_shares = t.shares
        if t.side is Side.SELL:
            # Polymarket has no naked shorts: a SELL beyond the shares we've
            # tracked means the BUY predates this window. Realize only the
            # tracked part and never fabricate a short position — the old
            # behavior scored a pre-window winner's profitable exit as a
            # losing "short" bet, systematically failing exactly the patient
            # long-horizon wallets the filters are meant to find.
            fill_shares = min(t.shares, shares) if shares > 0 else 0.0
            if fill_shares <= 1e-9:
                continue                   # exit of an untracked position: unscoreable
        token_market[t.token_id] = t.market_id
        market_gross[t.market_id] += t.usd_size
        if t.event_slug:
            categories.add(t.event_slug)
        eff = apply_fill(shares, avg, t.side, fill_shares, t.price)
        pos[t.token_id] = (eff.new_shares, eff.new_avg)
        market_realized[t.market_id] += eff.realized_delta

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


def copyability(n_copyable: int, target: int) -> float:
    """How much of a wallet's tape we could actually mirror, on a 0..1 scale.

    Saturating on purpose. A wallet with 200 copyable BUYs in the window is a
    scalper, not five times more followable than one with 40 -- and the live
    tape bears that out: the highest-copyable wallet of the month was also the
    worst leader. Rewarding "enough to be worth following" beats rewarding raw
    volume.

    Deliberately NOT min-max normalised like the other ranking components.
    Those are relative to whoever else happens to be in the pool that day, so a
    wallet's score drifts when nothing about the wallet changed; an absolute
    scale keeps this term stable across rescores.
    """
    if target <= 0:
        return 0.0
    return min(float(n_copyable), float(target)) / float(target)


def rank_wallets(
    stats: list[WalletStats],
    weights: dict[str, float],
    *,
    copyable_target: int = 40,
) -> list[LeaderScore]:
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
    cop = [copyability(s.n_copyable_trades, copyable_target) for s in stats]

    out: list[LeaderScore] = []
    for i, s in enumerate(stats):
        score = (
            weights.get("realized_pnl", 0) * pnl[i]
            + weights.get("win_rate", 0) * wr[i]
            + weights.get("consistency", 0) * cons[i]
            + weights.get("recency", 0) * rec[i]
            + weights.get("copyability", 0) * cop[i]
        )
        out.append(LeaderScore(wallet=s.wallet, score=score, stats=s))
    out.sort(key=lambda x: x.score, reverse=True)
    return out


def seat_roster(
    ranked: list[LeaderScore],
    incumbents: list[str],
    top_n: int,
    *,
    keep_incumbents: bool = True,
    explore_slots: int = 0,
) -> list[LeaderScore]:
    """Pick the lineup from ranked candidates, seating incumbents first.

    Plain top-N is a relative cut: when the eligible pool grows, leaders that
    got no worse are evicted by strangers who merely scored higher that day.
    Seating incumbents first removes that churn without weakening any bar --
    everyone here has already passed the filters, and copy vetting still runs
    downstream. `explore_slots` seats stay contestable so the lineup can still
    turn over on merit.
    """
    if top_n <= 0:
        return []
    if not keep_incumbents:
        return ranked[:top_n]

    held = {w.lower() for w in incumbents}
    protected = max(top_n - max(explore_slots, 0), 0)
    seated = [r for r in ranked if r.wallet.lower() in held][:protected]
    seen = {r.wallet for r in seated}
    for r in ranked:                      # remaining seats go by rank, to
        if len(seated) >= top_n:          # newcomers and unseated incumbents
            break                         # alike
        if r.wallet not in seen:
            seated.append(r)
            seen.add(r.wallet)
    seated.sort(key=lambda r: r.score, reverse=True)
    return seated


class LeaderSelector:
    """Orchestrates profile -> shortlist -> deep-score -> filter -> rank (live)."""

    def __init__(
        self,
        data: PolymarketDataClient,
        gamma: GammaClient,
        config: LeaderConfig | None = None,
        *,
        resolution_store: ResolutionStore | None = None,
        # The resolved-market ledger: accumulated per-wallet win records that
        # keep winners shortlisted even when today's feeds don't show them.
        record_store: RecordStore | None = None,
        records_shortlist_n: int = 300,
        records_harvest_limit: int = 150,
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
        self.record_store = record_store
        self.records_shortlist_n = records_shortlist_n
        self.records_harvest_limit = records_harvest_limit
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
        # An allowlisted wallet is a manual override, so the early rejects below
        # have to be skipped too — they run BEFORE the filter stage that the
        # allowlist was documented to bypass, so a pinned wallet that was quiet
        # for a day (recency) or trades in a style the copy path dislikes
        # (copyable) was thrown out before the bypass could ever apply. Only an
        # empty tape still stops us: there is nothing to score.
        if wallet in self.config.allowlist:
            return (wallet, compute_wallet_stats(
                wallet, [t for t in trades if t.timestamp >= cutoff] or trades,
                self._resolver, now=now,
                copyable_notional_min=self.copy_notional_min,
                copyable_price_min=self.copy_price_min,
                copyable_price_max=self.copy_price_max,
            ), None)
        newest = max(t.timestamp for t in trades)
        if (now - newest).total_seconds() / 3600.0 > f.max_hours_since_last_trade:
            return (wallet, None, _REJECT_RECENCY)
        if len(trades) >= self.trades_cap:
            # Tape truncated at the cap: `trades` is NOT the whole window, and
            # how much time the cap covers is the wallet's pace. A capped tape
            # spanning days = active human; spanning hours = HFT bot whose
            # leaderboard win rate is latency we can't copy (and whose vet
            # window we can't even fetch). Uncapped tapes skip this check —
            # short span there just means few trades, judged by min_trades.
            oldest = min(t.timestamp for t in trades)
            span_days = (newest - oldest).total_seconds() / 86_400
            if span_days < f.min_tape_span_days:
                return (wallet, None, _REJECT_TAPE_SPAN)
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

        # Stage 1: profile the whole pool from feeds, and grow the persistent
        # resolved-market ledger. Feeds see only the loudest markets of the
        # moment; the accumulated records remember every winner they've ever
        # covered, so proven wallets stay visible through feed churn.
        profiles = profile_candidates(
            self.data,
            self.gamma,
            top_open_markets=self.top_open_markets,
            top_closed_markets=self.top_closed_markets,
            per_market_trades=self.per_market_trades,
            lookback_days=cfg.filters.lookback_days,
            now=now,
        )
        record_quality: dict[str, float] = {}
        if self.record_store is not None:
            try:
                harvest_resolved_records(
                    self.data, self.gamma, self.record_store,
                    market_limit=self.records_harvest_limit,
                    lookback_days=cfg.filters.lookback_days,
                    per_market_trades=self.per_market_trades,
                    now=now,
                )
                since = (now - timedelta(days=cfg.filters.lookback_days)).isoformat()
                for w, (wins, losses, pnl) in self.record_store.wallet_summaries(since).items():
                    if pnl > 0:            # only net winners earn shortlist slots
                        record_quality[w] = _shrunk_win_frac(wins, losses)
            except Exception as e:
                log.warning("records: harvest failed (%s); continuing without", e)
        pool = (set(profiles) | set(record_quality) | allow) - block

        # Stage 2: shortlist by estimated win quality — NOT by activity, which
        # would hand the deep-score slots to high-frequency market-maker bots.
        # Two evidence sources: today's feeds and the accumulated records.
        active = [
            w for w in pool
            if w in profiles and profiles[w].n_trades >= self.feed_min_trades
        ]
        shortlist = sorted(active, key=lambda w: profiles[w].quality, reverse=True)
        chosen = set(shortlist[: self.deep_score_limit])
        if record_quality:
            by_record = sorted(record_quality, key=record_quality.__getitem__, reverse=True)
            chosen |= set(by_record[: self.records_shortlist_n]) - block
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

        ranked = rank_wallets(
            eligible, cfg.weights, copyable_target=cfg.selection.copyable_target
        )
        top = seat_roster(
            ranked,
            list(incumbents or []),
            cfg.selection.top_n,
            keep_incumbents=cfg.selection.keep_incumbents,
            explore_slots=cfg.selection.explore_slots,
        )
        # top_n is the last place an allowlisted wallet could still be dropped:
        # it bypasses the filters only to be cut here if it ranks below the
        # line. A manual pin has to survive its own ranking.
        if allow:
            picked = {r.wallet for r in top}
            top = top + [r for r in ranked if r.wallet in allow and r.wallet not in picked]
        rejects = Counter(early)
        rejects.update(filter_fails)
        held = {w.lower() for w in (incumbents or [])}
        retained = sum(1 for r in top if r.wallet.lower() in held)
        self.last_report = {
            "pool": len(pool),
            "record_wallets": len(record_quality),
            "deep_scored": len(targets),
            "early_rejects": dict(early),
            "filter_rejects": dict(filter_fails),
            "rejects": dict(rejects),
            "eligible": len(eligible),
            "followed": len(top),
            # Churn, logged every run. The first month averaged 5.5 leaders a
            # rescore and still burned through 33 wallets, which is why no
            # leader ever accumulated a record worth reading.
            "retained": retained,
        }
        log.info(
            "scoring: %d pool -> %d deep-scored -> %d eligible -> "
            "%d followed (%d retained, %d new)",
            len(pool), len(targets), len(eligible), len(top),
            retained, len(top) - retained,
        )
        return top
