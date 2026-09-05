"""Backtest harnesses for the copy strategies.

`Backtester` replays leaders' historical BUY entries held to resolution
(Strategy #4 semantics). `ExactCopyBacktester` replays full leader tapes —
BUYs *and* proportional SELLs — matching what `ExactCopyStrategy` does live.
Both reuse the live RiskManager and ledger accounting, and free exposure as
positions resolve in time order — so bankroll/caps behave like they would live.

Only resolved markets with a decisive winner are included (that's the only way
to know the true payout). Use these to vet auto-selected leaders before the
optional live window.
"""

from __future__ import annotations

import heapq
import logging
import time
import threading
from bisect import bisect_left
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from pmbot.config import Settings, get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.models import Fill, LeaderTrade, Market, Side, Signal
from pmbot.portfolio.ledger import Ledger
from pmbot.risk import RiskManager
from pmbot.strategy.exact_copy import EXIT_CRUMB_SHARES, observe_leader_fill

log = logging.getLogger(__name__)

_MIN_PRICE = 1e-4
_MAX_PRICE = 1 - 1e-4

# What the live loop can actually see when it decides on an entry: it polls
# every `poll_interval_seconds` and fetches `trades_per_leader` trades.
_LIVE_POLL_SECONDS = 10.0
_LIVE_TAPE_DEPTH = 25


def observed_price(
    series: list[tuple[int, float]] | None, at: float, max_staleness: float
) -> float | None:
    """First sampled price at or after `at`, or None if nothing lands near it.

    The live bot quotes the book at the moment it acts; a backtest can only
    reach for the nearest sample of that token's price history. Returning None
    rather than the last price before `at` is deliberate — a stale earlier
    sample is the leader's own price wearing a disguise, which is exactly the
    free fill this is meant to remove.
    """
    if not series:
        return None
    i = bisect_left(series, (at,))
    if i >= len(series):
        return None
    ts, px = series[i]
    return px if (ts - at) <= max_staleness else None


def round_tripped_entry_uids(
    ordered: list[LeaderTrade],
    *,
    window_seconds: float | None = _LIVE_POLL_SECONDS,
    tape_depth: int | None = _LIVE_TAPE_DEPTH,
) -> set[str]:
    """BUY uids the live bot would skip as already-round-tripped.

    Share accounting (including the crumb tolerance) mirrors
    `ExactCopyStrategy._round_tripped_entry_uids` exactly — see that docstring
    for why only FULL exits retire an entry.

    The two limits here are what a whole-tape scan gets wrong. Live, an entry
    is evaluated at the FIRST poll after it appears and then marked seen, so it
    is skipped only if the leader's full exit has already landed by that poll:
    within one `poll_interval_seconds`, and still inside the 25-trade window
    the strategy fetches. Scanning a deep research tape instead retires entries
    the leader exited hours later — which the live bot copied long before that
    exit existed. That is the difference between "44% of entries are
    round-tripped" (measured on a deep tape) and the far smaller share the bot
    actually declines. Pass `None`/`None` to reproduce the whole-tape number.
    """
    held: dict[str, float] = {}
    bought: dict[str, float] = {}
    open_uids: dict[str, list[tuple[str, datetime, int]]] = {}
    closed: set[str] = set()
    for idx, t in enumerate(ordered):
        prior = held.get(t.token_id, 0.0)
        if t.side is Side.BUY:
            held[t.token_id] = prior + t.shares
            bought[t.token_id] = bought.get(t.token_id, 0.0) + t.shares
            open_uids.setdefault(t.token_id, []).append((t.uid, t.timestamp, idx))
            continue
        remaining = max(0.0, prior - t.shares)
        if remaining <= max(EXIT_CRUMB_SHARES, 1e-4 * bought.get(t.token_id, 0.0)):
            remaining = 0.0
            for uid, ts, i in open_uids.pop(t.token_id, []):
                if (window_seconds is not None
                        and (t.timestamp - ts).total_seconds() > window_seconds):
                    continue                       # we had already copied it
                if tape_depth is not None and (idx - i) >= tape_depth:
                    continue                       # entry had scrolled off the window
                closed.add(uid)
            bought.pop(t.token_id, None)
        held[t.token_id] = remaining
    return closed


def book_windows(
    moments: list[tuple[str, datetime]],
    *,
    lead_seconds: float = 300.0,
    trail_seconds: float = 900.0,
    max_span_seconds: float = 12 * 3600.0,
) -> dict[str, tuple[int, int]]:
    """One [start, end] fetch window per token, covering the instants we act.

    Trades on a token usually cluster within hours, so a single window at
    1-minute fidelity covers them all in one request. `max_span_seconds` caps
    the ones that don't: a token touched on day 1 and again on day 20 gets the
    first cluster at full fidelity rather than a 20-day window the API would
    only serve coarsely. Anything outside the window has no quote, and
    `simulate` then does what live does with no quote — nothing.
    """
    seen: dict[str, list[float]] = {}
    for token, ts in moments:
        if token:
            seen.setdefault(token, []).append(ts.timestamp())
    out: dict[str, tuple[int, int]] = {}
    for token, stamps in seen.items():
        lo = min(stamps) - lead_seconds
        hi = min(max(stamps) + trail_seconds, lo + max_span_seconds)
        out[token] = (int(lo), int(hi))
    return out


def fetch_book(
    moments: list[tuple[str, datetime]],
    price_cache,
    *,
    cache: dict | None = None,
    workers: int = 12,
    fidelity_minutes: int = 1,
    progress: "callable | None" = None,
) -> dict[str, list[tuple[int, float]]]:
    """Price history around every moment we would have quoted the book.

    This is the other half of a faithful `simulate` — without it the sim fills
    at the leader's own price, which the live executor refuses to do. One
    request per token at the finest fidelity the CLOB serves, threaded because
    a month of a few leaders runs to thousands of tokens.

    `cache` is read AND written in place ({token: [[ts, px], …]}), so a caller
    that persists it makes re-runs free.

    A FAILED fetch is deliberately left out of the cache, while a successful
    empty response is stored. The two are indistinguishable once written, and
    they mean opposite things: an empty series is "no quote", which `simulate`
    reads as "no trade" — so caching an error would silently delete real trades
    from every later run and understate the wallet, with nothing in the output
    to show for it. Left uncached, the token is simply retried next pass.
    Failures are counted and returned to the caller via the log.
    """
    windows = book_windows(moments)
    cache = {} if cache is None else cache
    todo = [(t, w) for t, w in windows.items() if t not in cache]
    if todo:
        local = threading.local()
        lock = threading.Lock()
        done, failed = [0], [0]

        def one(item):
            token, (lo, hi) = item
            pc = getattr(local, "pc", None)
            if pc is None:
                pc = local.pc = price_cache() if callable(price_cache) else price_cache
            try:
                hist = pc.get_price_history(token, start_ts=lo, end_ts=hi,
                                            fidelity_minutes=fidelity_minutes)
            except Exception as e:
                log.debug("backtest: book fetch failed for %s: %s", token[:12], e)
                with lock:
                    failed[0] += 1          # NOT cached: retried on the next pass
                return
            with lock:
                cache[token] = hist
                done[0] += 1
                if progress is not None and done[0] % 250 == 0:
                    progress(done[0], len(todo))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, todo))
        if failed[0]:
            log.warning(
                "backtest: %d/%d book fetches failed and were NOT cached — those "
                "tokens have no quote this pass, so their trades are skipped; "
                "re-run to pick them up", failed[0], len(todo),
            )
    return {
        t: sorted((int(a), float(b)) for a, b in (cache.get(t) or []))
        for t in windows
    }


@dataclass
class CopyResult:
    leader: str
    market_id: str
    outcome: str
    entry_ts: datetime
    resolve_ts: datetime
    entry_price: float
    size_usd: float
    shares: float
    won: bool
    pnl: float
    token_id: str = ""
    # "resolution" | "leader-exit" | "stop-loss" — how the tranche ended.
    closed_by: str = "resolution"


def max_drawdown(cumulative: list[float]) -> float:
    """Largest peak-to-trough drop along a cumulative-P&L curve."""
    if not cumulative:
        return 0.0
    peak = cumulative[0]
    mdd = 0.0
    for v in cumulative:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


@dataclass
class BacktestReport:
    results: list[CopyResult]
    starting_bankroll: float
    title: str = "Strategy #4 (long-term copy)"
    note: str = "assumes hold-to-resolution; only resolved markets included."
    # Why candidate BUYs were dropped, keyed by filter name. A backtest that
    # only reports what it took cannot be compared against live, where most of
    # the tape is refused — see `simulate`'s live-filter arguments.
    skipped: dict[str, int] = field(default_factory=dict)
    # (token_id, timestamp) for every BUY that cleared the *static* filters,
    # whether or not the RiskManager sized it. Callers pre-fetch entry prices
    # for these before a second pass turns the price-dependent filters on.
    candidate_entries: list[tuple[str, datetime]] = field(default_factory=list)

    def metrics(self) -> dict:
        n = len(self.results)
        invested = sum(r.size_usd for r in self.results)
        net = sum(r.pnl for r in self.results)
        wins = sum(1 for r in self.results if r.won)
        edges = [r.pnl / r.size_usd for r in self.results if r.size_usd > 0]
        by_time = sorted(self.results, key=lambda r: r.resolve_ts)
        cum, running = [], 0.0
        for r in by_time:
            running += r.pnl
            cum.append(running)
        by_leader: dict[str, list[float]] = {}
        for r in self.results:
            by_leader.setdefault(r.leader, []).append(r.pnl)
        return {
            "n_trades": n,
            "invested": invested,
            "net_pnl": net,
            "roi": (net / invested) if invested else 0.0,
            "win_rate": (wins / n) if n else 0.0,
            "avg_edge": (sum(edges) / len(edges)) if edges else 0.0,
            "max_drawdown": max_drawdown(cum),
            "by_leader": {k: (len(v), sum(v)) for k, v in by_leader.items()},
        }

    def summary_text(self) -> str:
        m = self.metrics()
        if m["n_trades"] == 0:
            return ("Backtest: 0 copyable resolved trades found.\n"
                    "Try a longer --lookback, a higher --limit, or different leaders.")
        lines = [
            f"=== Backtest: {self.title} ===",
            f"  copied trades : {m['n_trades']}",
            f"  invested      : ${m['invested']:,.2f}",
            f"  net P&L       : ${m['net_pnl']:,.2f}",
            f"  ROI (/deployed): {m['roi']*100:,.1f}%",
            f"  win rate      : {m['win_rate']*100:,.1f}%",
            f"  avg edge/trade: {m['avg_edge']*100:,.2f}%",
            f"  max drawdown  : ${m['max_drawdown']:,.2f}",
            "  by leader:",
        ]
        for leader, (cnt, pnl) in sorted(m["by_leader"].items(), key=lambda x: -x[1][1]):
            lines.append(f"    {leader[:12]}…  {cnt:>3d} trades  net ${pnl:,.2f}")
        lines.append(f"\nNote: {self.note}")
        return "\n".join(lines)


class Backtester:
    def __init__(
        self,
        data: PolymarketDataClient,
        gamma: GammaClient,
        settings: Settings | None = None,
        *,
        slippage_bps: float | None = None,
        lookback_days: int = 180,
        trades_limit: int = 500,
        min_days_to_resolution: int | None = None,
        min_liquidity: float | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
    ):
        s = settings or get_settings()
        self.s = s
        self.data = data
        self.gamma = gamma
        self.slip = (s.slippage_bps if slippage_bps is None else slippage_bps) / 10_000
        self.lookback_days = lookback_days
        self.trades_limit = trades_limit
        self.min_days = s.longterm_min_days_to_resolution if min_days_to_resolution is None else min_days_to_resolution
        # Liquidity is a *current* snapshot; resolved markets read ~0 after
        # settlement, so we don't filter on it in backtests by default.
        self.min_liquidity = 0.0 if min_liquidity is None else min_liquidity
        self.price_min = s.copy_price_min if price_min is None else price_min
        self.price_max = s.copy_price_max if price_max is None else price_max
        self._mkt_cache: dict[str, tuple[Market | None, str | None]] = {}
        # Quotes every `simulate` uses unless one is passed explicitly. Set via
    def _market(self, condition_id: str) -> tuple[Market | None, str | None]:
        if condition_id not in self._mkt_cache:
            try:
                self._mkt_cache[condition_id] = self.gamma.get_market_with_resolution(condition_id)
            except Exception:
                # Transient failure (rate limit, timeout): skip THIS call but do
                # NOT cache — a poisoned entry silently drops every trade in the
                # market for the process lifetime, gutting backtests and vetting.
                # (Same fix as LeaderSelector._resolver.)
                return (None, None)
        return self._mkt_cache[condition_id]

    def run(self, leaders: list[str], now: datetime | None = None) -> BacktestReport:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.lookback_days)

        # 1) Gather eligible historical entries (resolved markets only).
        entries: list[tuple] = []
        for leader in leaders:
            try:
                trades = self.data.get_trades(user=leader, limit=self.trades_limit)
            except Exception as e:
                log.debug("backtest: trades fetch failed for %s: %s", leader[:10], e)
                continue
            for t in trades:
                if t.side is not Side.BUY or t.timestamp < cutoff:
                    continue
                if not (self.price_min <= t.price <= self.price_max):
                    continue
                market, winner = self._market(t.market_id)
                if market is None or not market.closed or winner is None or market.end_date is None:
                    continue
                horizon = (market.end_date - t.timestamp).total_seconds() / 86_400
                if horizon < self.min_days:
                    continue
                if market.liquidity_usd is not None and market.liquidity_usd < self.min_liquidity:
                    continue
                entries.append((t, market, winner))

        return self._simulate(entries)

    def run_on_markets(self, condition_ids: list[str], now: datetime | None = None) -> BacktestReport:
        """Backtest every eligible BUY entry within specific resolved markets.

        Unlike `run` (which follows specific leaders), this copies all
        participants in the given markets — handy for validating the engine on
        real resolved outcomes and for studying a market's copyable edge.
        """
        entries: list[tuple] = []
        for cid in condition_ids:
            market, winner = self._market(cid)
            if market is None or not market.closed or winner is None or market.end_date is None:
                continue
            try:
                trades = self.data.get_trades(market=cid, limit=self.trades_limit)
            except Exception:
                continue
            for t in trades:
                if t.side is not Side.BUY or not (self.price_min <= t.price <= self.price_max):
                    continue
                horizon = (market.end_date - t.timestamp).total_seconds() / 86_400
                if horizon < self.min_days:
                    continue
                entries.append((t, market, winner))
        return self._simulate(entries)

    def _simulate(self, entries: list[tuple]) -> BacktestReport:
        entries.sort(key=lambda e: e[0].timestamp)

        # Event-driven sim: size with real caps, free exposure at resolution.
        ledger = Ledger(":memory:")
        risk = RiskManager(ledger, self.s)
        pending: list[tuple] = []   # heap: (resolve_ts, seq, signal, shares, payout)
        results: list[CopyResult] = []
        seq = 0

        def settle_until(ts: datetime) -> None:
            while pending and pending[0][0] <= ts:
                rts, _, sig, shares, payout = heapq.heappop(pending)
                ledger.record_fill(Fill(
                    signal=replace(sig, side=Side.SELL), fill_price=payout,
                    size_usd=payout * shares, shares=shares, timestamp=rts, mode="backtest",
                ))

        for t, market, winner in entries:
            settle_until(t.timestamp)
            sig = Signal(
                market_id=t.market_id, token_id=t.token_id, outcome=t.outcome,
                side=Side.BUY, target_price=t.price, size_usd=t.usd_size,
                reason="backtest", source_leader=t.leader, source_uid=t.uid,
            )
            sized = risk.size(sig)
            if sized is None:
                continue
            entry_price = min(max(t.price * (1 + self.slip), _MIN_PRICE), _MAX_PRICE)
            shares = sized.size_usd / entry_price
            ledger.record_fill(Fill(
                signal=sized, fill_price=entry_price, size_usd=sized.size_usd,
                shares=shares, timestamp=t.timestamp, mode="backtest",
            ))
            won = t.token_id == winner
            payout = 1.0 if won else 0.0
            results.append(CopyResult(
                leader=t.leader, market_id=t.market_id, outcome=t.outcome,
                entry_ts=t.timestamp, resolve_ts=market.end_date, entry_price=entry_price,
                size_usd=sized.size_usd, shares=shares, won=won,
                pnl=(payout - entry_price) * shares,
            ))
            heapq.heappush(pending, (market.end_date, seq, sized, shares, payout))
            seq += 1

        ledger.close()
        return BacktestReport(results=results, starting_bankroll=self.s.bankroll_usd)


def vet_weights(rois: dict[str, float]) -> dict[str, float]:
    """Turn per-leader copy-backtest ROIs into sizing multipliers.

    Mean-normalized (so the pack's total allocation stays roughly flat) —
    a leader at 2x the pack's average copy-ROI gets ~2x the base fraction.
    Clamping to the RiskManager's [0.5, 2.0] band happens downstream in
    `set_leader_weights`. Leaders absent from `rois` default to 1.0 there.
    """
    positive = {w: r for w, r in rois.items() if r > 0}
    if not positive:
        return {}
    mean = sum(positive.values()) / len(positive)
    if mean <= 0:
        return {}
    return {w: r / mean for w, r in positive.items()}


@dataclass
class _Tranche:
    """Our copied holding attributed to one (leader, token)."""

    market: Market
    outcome: str
    entry_ts: datetime
    shares: float = 0.0
    cost: float = 0.0          # total USD spent on remaining + sold shares
    bought: float = 0.0        # lifetime shares bought (for reporting)
    realized: float = 0.0

    @property
    def avg(self) -> float:
        return self.cost / self.bought if self.bought else 0.0


class ExactCopyBacktester:
    """Replay leader tapes with ExactCopyStrategy semantics.

    Mirrors BUYs (risk-sized) and mirrors SELLs proportionally to the fraction
    of the leader's position they exited, at the leader's price ± slippage.
    Whatever is still held when the market resolves settles at the payout.

    `fetch_tapes` does the network work once; `simulate` is offline apart from
    memoized market-resolution lookups, so parameter sweeps over the same
    tapes are cheap.
    """

    def __init__(
        self,
        data: PolymarketDataClient,
        gamma: GammaClient,
        settings: Settings | None = None,
        *,
        slippage_bps: float | None = None,
        trades_limit: int = 1000,
    ):
        s = settings or get_settings()
        self.s = s
        self.data = data
        self.gamma = gamma
        self.slip = (s.slippage_bps if slippage_bps is None else slippage_bps) / 10_000
        self.trades_limit = trades_limit
        self._mkt_cache: dict[str, tuple[Market | None, str | None]] = {}
        # Quotes every `simulate` uses unless one is passed explicitly. Set via
        # `attach_book` so a sweep can fetch once and leave its dozens of call
        # sites — most of them nested helpers — untouched.
        self._book: dict[str, list[tuple[int, float]]] | None = None

    def _market(self, condition_id: str) -> tuple[Market | None, str | None]:
        if condition_id not in self._mkt_cache:
            try:
                self._mkt_cache[condition_id] = self.gamma.get_market_with_resolution(condition_id)
            except Exception:
                # Transient failure (rate limit, timeout): skip THIS call but do
                # NOT cache — a poisoned entry silently drops every trade in the
                # market for the process lifetime, gutting backtests and vetting.
                # (Same fix as LeaderSelector._resolver.)
                return (None, None)
        return self._mkt_cache[condition_id]

    def fetch_tapes(
        self, leaders: list[str], *, page_attempts: int = 3
    ) -> dict[str, list[LeaderTrade]]:
        """Fetch each leader's trade tape (paginated, newest-first from API).

        Pagination stops ONLY on a definitive end-of-tape signal: an empty page,
        a page carrying no unseen uids, or `trades_limit` reached. A short page
        is NOT such a signal — the API returns one intermittently, and treating
        it as the end silently truncated a 3000-trade/113-day tape to 998
        trades/27 days, quietly changing backtest P&L by ~28%. The extra request
        that confirms the real end (an empty page) is worth far more than the
        wrong answer it prevents.

        A page that keeps failing is retried with backoff and then reported at
        WARNING, never swallowed at DEBUG: `_vet_leaders` keeps or drops live
        leaders off this tape, so a short one is a wrong trading decision, not
        just a thin report.
        """
        tapes: dict[str, list[LeaderTrade]] = {}
        chunk = 500
        for leader in leaders:
            trades: list[LeaderTrade] = []
            seen: set[str] = set()
            offset = 0
            failure: str | None = None
            while len(trades) < self.trades_limit:
                want = min(chunk, self.trades_limit - len(trades))
                page = None
                for attempt in range(1, page_attempts + 1):
                    try:
                        page = self.data.get_trades(user=leader, limit=want, offset=offset)
                        break
                    except Exception as e:
                        if attempt == page_attempts:
                            failure = f"{type(e).__name__}: {e}"
                            log.debug("backtest: tape page failed for %s at offset %d: %s",
                                      leader[:10], offset, e)
                        else:
                            time.sleep(min(0.5 * 2 ** (attempt - 1), 4.0))
                if page is None:
                    break                          # exhausted retries; reported below
                if not page:
                    break                          # empty page = true end of tape
                fresh = [t for t in page if t.uid not in seen]
                if not fresh:
                    break                          # only duplicates: not advancing
                seen.update(t.uid for t in fresh)
                trades.extend(fresh)
                offset += len(page)
            if failure is not None:
                log.warning(
                    "backtest: tape for %s TRUNCATED at %d trades after %d failed "
                    "attempts (%s) — this leader's window covers less history than "
                    "requested; vetting and backtests over it are understated",
                    leader[:10], len(trades), page_attempts, failure,
                )
            tapes[leader.lower()] = trades
        return tapes

    def prefetch_markets(
        self, condition_ids, *, workers: int = 16, progress=None
    ) -> int:
        """Resolve many markets in parallel, straight into `_market`'s memo.

        `_market` is serial and memoized, which is right for a replay — it asks
        for each market once, when it first matters. It is wrong for the first
        pass over a wide pool: one 25-wallet batch of feed-sourced wallets
        resolved 7,247 unseen markets, and at one request apiece that was 49 of
        the 50 minutes the batch took. Warming the same memo from a thread pool
        turns that into minutes and changes nothing else.

        Only misses are fetched. A failure is deliberately left UNMEMOIZED so
        the replay retries it, exactly as `_market` already does — caching the
        error would silently drop every trade in that market.
        """
        todo = [c for c in dict.fromkeys(condition_ids)
                if c and c not in self._mkt_cache]
        if not todo:
            return 0
        lock = threading.Lock()
        done = [0]

        def one(cid):
            try:
                got = self.gamma.get_market_with_resolution(cid)
            except Exception as e:
                log.debug("backtest: prefetch failed for %s: %s", cid[:12], e)
                return
            with lock:
                self._mkt_cache[cid] = got
                done[0] += 1
                if progress is not None and done[0] % 500 == 0:
                    progress(done[0], len(todo))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, todo))
        return len(todo)

    def attach_book(
        self, book: dict[str, list[tuple[int, float]]] | None
    ) -> "ExactCopyBacktester":
        """Use `book` for every later `simulate` that does not pass its own.

        A sweep calls `build_book` once and attaches the result; each arm then
        prices off real quotes without threading an argument through helpers
        that were written before quotes existed. Returns self so it chains.
        """
        self._book = book
        return self

    def build_book(
        self,
        tapes: dict[str, list[LeaderTrade]],
        *,
        price_cache=None,
        book_cache: dict | None = None,
        progress=None,
        **kw,
    ) -> dict[str, list[tuple[int, float]]]:
        """Quotes for everything any run over these tapes could trade.

        Two passes are unavoidable: the quotes we need depend on which trades
        clear the static filters, and that is only known after a replay. This
        is pass 1 — its fills are discarded, only its `candidate_entries`
        matter — plus the fetch. SELLs on candidate tokens are included, so a
        mirrored exit prices on the book too.

        Discovery deliberately runs at the LOOSEST filters: whole price band,
        no notional floor, no liquidity floor, no horizon, no round-trip skip.
        One book then covers a whole parameter sweep. Narrowing discovery to
        the current settings would leave a sweep's wider arms with no quotes,
        and `simulate` reads "no quote" as "no trade" — so those arms would
        silently score zero and the sweep would pick a winner by data coverage.

        For the same reason, a walk-forward should pass the WIDEST window in
        its grid (`now` at the latest fold's end, `lookback_days` spanning all
        of them); a narrower one leaves the other folds unquoted.
        """
        from pmbot.data import PriceCache

        loose = dict(kw)
        loose.update(
            price_min=0.0, price_max=1.0, min_leader_notional=0.0,
            min_liquidity=0.0, min_hours_to_resolution=0.0,
            max_hours_to_resolution=0.0, skip_price_band=None,
            skip_round_tripped_entries=False, stop_loss_frac=None,
            book=None, warn_no_book=False,
        )
        attached, self._book = self._book, None    # discovery must not reuse a book
        try:
            scout = self.simulate(tapes, **loose)
        finally:
            self._book = attached
        wanted = {tok for tok, _ in scout.candidate_entries}
        moments = list(scout.candidate_entries)
        moments += [
            (t.token_id, t.timestamp)
            for tape in tapes.values() for t in tape
            if t.side is Side.SELL and t.token_id in wanted
        ]
        return fetch_book(moments, price_cache or PriceCache(),
                          cache=book_cache, progress=progress)

    def simulate_live(
        self,
        tapes: dict[str, list[LeaderTrade]],
        *,
        price_cache=None,
        book_cache: dict | None = None,
        progress=None,
        **kw,
    ) -> BacktestReport:
        """`simulate` with the book fetched for you — the faithful entry point.

        Prefer this over calling `simulate` directly. `simulate` with no `book`
        fills at the leader's own price, which the live executor refuses to do,
        and every number that comes out of it is optimistic.

        For a SWEEP, call `build_book` once and pass `book=` to each
        `simulate`: this method rebuilds the discovery pass every call, which
        over a parameter grid is pure waste.
        """
        book = self.build_book(tapes, price_cache=price_cache,
                               book_cache=book_cache, progress=progress, **kw)
        return self.simulate(tapes, book=book, **kw)

    def run(
        self,
        leaders: list[str],
        *,
        lookback_days: int = 60,
        min_leader_notional: float = 0.0,
        price_min: float | None = None,
        price_max: float | None = None,
        now: datetime | None = None,
    ) -> BacktestReport:
        tapes = self.fetch_tapes(leaders)
        return self.simulate(
            tapes, lookback_days=lookback_days, min_leader_notional=min_leader_notional,
            price_min=price_min, price_max=price_max, now=now,
        )

    def simulate(
        self,
        tapes: dict[str, list[LeaderTrade]],
        *,
        lookback_days: int = 60,
        min_leader_notional: float = 0.0,
        price_min: float | None = None,
        price_max: float | None = None,
        now: datetime | None = None,
        settings: Settings | None = None,
        slippage_bps: float | None = None,
        leader_weights: dict[str, float] | None = None,
        min_hours_to_resolution: float = 0.0,
        max_hours_to_resolution: float = 0.0,
        stop_loss_frac: float | None = None,
        price_series: dict[str, list[tuple[int, float]]] | None = None,
        resolve_at: dict[str, datetime] | None = None,
        skip_round_tripped_entries: bool = False,
        skip_price_band: tuple[float, float] | None = None,
        min_liquidity: float | None = None,
        book: dict[str, list[tuple[int, float]]] | None = None,
        entry_lag_seconds: float | None = None,
        max_price_drift: float | None = None,
        book_max_staleness_seconds: float = 180.0,
        warn_no_book: bool = True,
    ) -> BacktestReport:
        """Simulate the tapes over the window [now - lookback_days, now].

        `now` also upper-bounds the tape, so walk-forward splits work: tune on
        an earlier window (pass a past `now`), validate on the recent one.
        `settings` overrides sizing knobs (copy_fraction, caps, bankroll) per
        run so allocation sweeps don't need a backtester per combination.
        `leader_weights` are per-leader copy_fraction multipliers (see
        `vet_weights`); `min_hours_to_resolution` skips entries placed within
        that many hours of the market's close (in-game/near-resolution trades
        are the ones our live lag copies worst); `max_hours_to_resolution`
        skips entries placed more than that many hours before it, capping how
        long a copy can tie up bankroll.

        Both horizon knobs read Gamma's `end_date`, never `resolve_at`: that
        stamp is the only horizon the LIVE bot has at entry time, so scoring
        them against a data-derived resolution would validate a rule we cannot
        actually run. The max knob is also unharmed by the start-of-day
        `end_date` quirk described below — that quirk makes `hours_left`
        negative, which sits under any positive ceiling, so in-game sports
        entries pass the max filter exactly as they do live.

        `skip_price_band` punches a hole in the middle of the copy window:
        entries priced inside [lo, hi) are refused while everything cheaper and
        everything dearer is still copied. `price_min`/`price_max` can only
        move the window's edges, so a "the middle is where we bleed" claim is
        not expressible with them — hence the separate knob. Half-open on the
        top so adjacent bands tile without double-counting a boundary price.

        `stop_loss_frac` arms a protective exit: if the token's observed price
        falls to `avg_entry * (1 - frac)` before the leader exits or the market
        resolves, the whole tranche is sold there. It needs `price_series`
        ({token_id: [(unix_ts, price), …]}, ascending) — the CLOB price history
        is hourly by default, so a stop can only fire as fast as that sampling
        and will MISS intra-hour spikes through the level. Both default to off,
        leaving live leader-vetting behaviour untouched.

        `resolve_at` ({token_id: datetime}) overrides Gamma's `end_date` as the
        settlement instant. Daily-sports markets stamp end_date at the START of
        the day, so an in-game entry "resolves" before it was opened and the
        tranche settles instantly — fine for P&L (the payout is still right),
        useless for anything time-dependent. A stop needs a real holding
        window, so pass a data-derived resolution for those markets.

        THE LIVE PATH
        -------------
        Everything above describes the leader's tape. This part describes *us*,
        and it is not optional: `min_liquidity`, `entry_lag_seconds` and
        `max_price_drift` all default to the values in `settings` — the same
        object the live bot reads — so an unconfigured call reproduces the
        deployed strategy instead of an optimistic cartoon of it. Pass a number
        only to sweep one deliberately.

        `book` is the missing half: {token_id: [(unix_ts, price), …]},
        ascending, each token's own price history. Live, both the strategy and
        the executor quote the CLOB at the moment they act, and every decision
        below flows from that quote. Build it with `fetch_book`.

        With a `book`, an entry is decided exactly as live decides it:

          1. The quote is the first sample at or after
             `trade_ts + entry_lag_seconds`, which defaults to
             `copy_feed_lag_seconds + poll_interval_seconds`. The second term
             is the loop; the FIRST is the one that matters. `/trades` is
             served from a 300s CDN cache over an origin that publishes in
             ~200s batches, so a leader's trade is ~150s old before any poll
             can see it (measured over 450 live fills: median 149s on entries,
             179s on exits, p90 ~285s). A sim run at the poll interval alone
             prices every fill 15x closer to the leader than we can fill.
          2. NO QUOTE MEANS NO TRADE. `PaperExecutor` refuses a copy buy it has
             no book for, deliberately: falling back to the leader's price
             grants a perfect fill exactly when we are blind. Same for a copy
             sell, which is then left to ride into resolution. Counted as
             `no_book` / `exit_no_book`.
          3. `max_price_drift` (`copy_max_price_drift`) refuses an entry whose
             quote has moved further than the budget from the leader's fill —
             the edge was arbitraged away before we could reach it.
          4. The fill is `quote * (1 + slippage)` for a buy and
             `quote * (1 - slippage)` for a sell, never the leader's price.
          5. `PaperExecutor`'s limit cap then refuses any buy whose FILL lands
             above `leader_price + copy_max_price_drift`. That is a second and
             tighter gate than (3), and it binds whenever the quote sits near
             the drift budget, because the slippage is added afterwards.
             Counted as `entry_limit`.

        Without a `book` this warns once and fills at the leader's price. That
        path exists for offline unit tests; any backtest whose number you mean
        to believe must supply one.

        `min_liquidity` mirrors `min_market_liquidity_usd`, including the live
        rule that an ABSENT liquidity figure passes — but read
        `liquidity_unknown` in `skipped` before trusting it, because Gamma drops
        `liquidityNum` when a market closes, so a resolved-market backtest sees
        the figure on very few markets. Correct where the data exists; the data
        usually does not.

        Two live behaviours are deliberately NOT modelled, having been measured
        to be no-ops rather than assumed to be: the 25-trade fetch window never
        binds (the busiest roster wallet peaks at 24 trades in a 10-second poll,
        so nothing scrolls off unseen), and `copy_max_trade_age_minutes` cannot
        bind in a continuous replay, where every trade is decided one poll after
        it appears. Both only bite after downtime, which a backtest has no way
        to know about.
        """
        s = settings or self.s
        slip = self.slip if slippage_bps is None else slippage_bps / 10_000
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=lookback_days)
        p_min = s.copy_price_min if price_min is None else price_min
        p_max = s.copy_price_max if price_max is None else price_max
        # The live path, taken from the same settings object the bot reads.
        book = self._book if book is None else book
        min_liq = s.min_market_liquidity_usd if min_liquidity is None else min_liquidity
        drift = s.copy_max_price_drift if max_price_drift is None else max_price_drift
        # Not the poll interval: the feed we poll is itself minutes behind the
        # leader (see `copy_feed_lag_seconds`), and polling faster than it
        # publishes buys nothing. Measured 2026-09-03 at ~150s end to end.
        lag = (s.copy_feed_lag_seconds + s.poll_interval_seconds
               if entry_lag_seconds is None else entry_lag_seconds)
        if book is None and warn_no_book:
            log.warning(
                "backtest: no `book` supplied — entries will fill at the leader's "
                "own price and neither the drift guard nor the executor's limit "
                "can run. The live bot never trades this way; supply fetch_book() "
                "output for any result you intend to act on."
            )

        # Detect per leader on that leader's own tape, and only on trades at or
        # before `now` — using an exit the live bot could not yet have seen
        # would leak the future into a walk-forward test.
        round_tripped: set[str] = set()
        if skip_round_tripped_entries:
            for tape in tapes.values():
                visible = sorted(
                    (t for t in tape if t.timestamp <= now), key=lambda t: t.timestamp
                )
                round_tripped |= round_tripped_entry_uids(visible)

        all_trades = [t for tape in tapes.values() for t in tape if cutoff <= t.timestamp <= now]
        all_trades.sort(key=lambda t: t.timestamp)

        ledger = Ledger(":memory:")
        risk = RiskManager(ledger, s)
        if leader_weights:
            risk.set_leader_weights(leader_weights)
        leader_pos: dict[tuple[str, str], float] = {}   # leader's own share count
        tranches: dict[tuple[str, str], _Tranche] = {}  # our copied holdings
        results: list[CopyResult] = []
        skipped: Counter[str] = Counter()
        report_candidates: list[tuple[str, datetime]] = []
        # heap: (resolve_ts, seq, key, winner) — settle leftovers at payout.
        pending: list[tuple] = []
        settled: set[tuple[str, str]] = set()
        seq = 0

        def _resolves(token: str, market: Market) -> datetime:
            """When this position actually settles (override beats Gamma)."""
            if resolve_at and token in resolve_at:
                return resolve_at[token]
            return market.end_date

        armed: dict[tuple[str, str], tuple[datetime, float]] = {}  # key -> (trigger_ts, price)

        def arm_stop(key: tuple[str, str], after: datetime) -> None:
            """(Re)compute when this tranche's stop would trigger, if ever."""
            if stop_loss_frac is None or not price_series:
                return
            armed.pop(key, None)
            tr = tranches.get(key)
            if tr is None or tr.shares <= 1e-9 or key in settled:
                return
            series = price_series.get(key[1])
            if not series:
                return
            threshold = tr.avg * (1 - stop_loss_frac)
            after_u, until_u = after.timestamp(), _resolves(key[1], tr.market).timestamp()
            for t, p in series:                     # ascending
                if t <= after_u:
                    continue
                if t > until_u:
                    break
                if p <= threshold:
                    armed[key] = (datetime.fromtimestamp(t, tz=timezone.utc), p)
                    return

        def stop_out(key: tuple[str, str], sts: datetime, price: float) -> None:
            """Sell the whole tranche at the stop price; tranche is then done."""
            tr = tranches[key]
            leader, token = key
            exit_price = min(max(price * (1 - slip), _MIN_PRICE), _MAX_PRICE)
            tr.realized += (exit_price - tr.avg) * tr.shares
            ledger.record_fill(Fill(
                signal=Signal(tr.market.market_id, token, tr.outcome, Side.SELL,
                              exit_price, exit_price * tr.shares, "backtest-stop",
                              source_leader=leader),
                fill_price=exit_price, size_usd=exit_price * tr.shares,
                shares=tr.shares, timestamp=sts, mode="backtest",
            ))
            tr.shares = 0.0
            settled.add(key)
            armed.pop(key, None)
            results.append(CopyResult(
                leader=leader, market_id=tr.market.market_id, outcome=tr.outcome,
                entry_ts=tr.entry_ts, resolve_ts=sts, entry_price=tr.avg,
                size_usd=tr.cost, shares=tr.bought, won=tr.realized > 0,
                pnl=tr.realized, token_id=token, closed_by="stop-loss",
            ))

        def finish(key: tuple[str, str], rts: datetime, winner: str) -> None:
            if key in settled:
                return
            settled.add(key)
            armed.pop(key, None)
            tr = tranches[key]
            leader, token = key
            held_to_resolution = tr.shares > 1e-9
            if held_to_resolution:
                payout = 1.0 if token == winner else 0.0
                tr.realized += (payout - tr.avg) * tr.shares
                ledger.record_fill(Fill(
                    signal=Signal(tr.market.market_id, token, tr.outcome, Side.SELL,
                                  payout, payout * tr.shares, "backtest-settle",
                                  source_leader=leader),
                    fill_price=payout, size_usd=payout * tr.shares,
                    shares=tr.shares, timestamp=rts, mode="backtest",
                ))
                tr.shares = 0.0
            results.append(CopyResult(
                leader=leader, market_id=tr.market.market_id, outcome=tr.outcome,
                entry_ts=tr.entry_ts, resolve_ts=rts, entry_price=tr.avg,
                size_usd=tr.cost, shares=tr.bought, won=tr.realized > 0,
                pnl=tr.realized, token_id=token,
                closed_by="resolution" if held_to_resolution else "leader-exit",
            ))

        def settle_until(ts: datetime) -> None:
            """Fire stops and resolutions up to `ts`, in true chronological order."""
            while True:
                nxt_settle = pending[0][0] if pending else None
                stop_key, stop_at = None, None
                for k, (sts, _px) in armed.items():
                    if stop_at is None or sts < stop_at:
                        stop_key, stop_at = k, sts
                events = []
                if nxt_settle is not None and nxt_settle <= ts:
                    events.append((nxt_settle, 1))
                if stop_at is not None and stop_at <= ts:
                    events.append((stop_at, 0))     # stop wins an exact tie
                if not events:
                    return
                if min(events)[1] == 0:
                    stop_out(stop_key, stop_at, armed[stop_key][1])
                else:
                    rts, _, key, winner = heapq.heappop(pending)
                    finish(key, rts, winner)

        for t in all_trades:
            if not t.token_id or not t.market_id:
                continue
            settle_until(t.timestamp)
            lkey = (t.leader.lower(), t.token_id)
            prior = leader_pos.get(lkey, 0.0)
            # Shared with ExactCopyStrategy so the two cannot drift: live used
            # to track this through `apply_fill` (which goes short) while the
            # sim clamped at zero, so identical tapes produced different exit
            # fractions and vetting judged a bot that did not exist.
            leader_pos[lkey] = observe_leader_fill(prior, t.side, t.shares)

            if t.side is Side.BUY:
                if t.uid in round_tripped:
                    skipped["round_tripped"] += 1
                    continue                       # leader already flat before our next poll
                if not (p_min <= t.price <= p_max):
                    skipped["price_band"] += 1
                    continue
                if skip_price_band is not None and (
                    skip_price_band[0] <= t.price < skip_price_band[1]
                ):
                    skipped["skip_band"] += 1
                    continue                       # punched-out middle band
                if t.usd_size < min_leader_notional:
                    skipped["leader_notional"] += 1
                    continue
                market, winner = self._market(t.market_id)
                if market is None or not market.closed or winner is None or market.end_date is None:
                    skipped["unresolved_market"] += 1
                    continue                       # only resolved markets have a known payout
                # Only enforce when the knob is ON (matches ExactCopyStrategy).
                # Daily-sports markets carry end_date at START of day, so every
                # in-game trade has NEGATIVE hours_left; with the knob at its
                # default 0 the unguarded compare silently dropped all of them,
                # while the live bot copies them — backtests wildly undercounted
                # for sports-heavy leaders.
                if min_hours_to_resolution > 0 or max_hours_to_resolution > 0:
                    hours_left = (market.end_date - t.timestamp).total_seconds() / 3600
                    if min_hours_to_resolution > 0 and hours_left < min_hours_to_resolution:
                        continue
                    # No symmetric guard needed on the max side: the same
                    # start-of-day stamp that makes hours_left negative for
                    # in-game trades keeps them under every ceiling, which is
                    # what we want — the ceiling is aimed at months-out macro
                    # markets, not at sports.
                    if max_hours_to_resolution > 0 and hours_left > max_hours_to_resolution:
                        skipped["horizon"] += 1
                        continue
                # Live rule, absence included: a market with no liquidity figure
                # passes. Gamma drops the field on close, so `liquidity_unknown`
                # is usually most of the tape — the counter exists so a caller
                # cannot mistake "not filtered" for "checked and fine".
                if min_liq > 0:
                    if market.liquidity_usd is None:
                        skipped["liquidity_unknown"] += 1
                    elif market.liquidity_usd < min_liq:
                        skipped["liquidity"] += 1
                        continue
                report_candidates.append((t.token_id, t.timestamp))
                # What the book showed one poll after the leader hit it. Live,
                # no book means no copy — never the leader's price (see
                # PaperExecutor._resolve_fill_price).
                quoted = None
                if book is not None:
                    quoted = observed_price(
                        book.get(t.token_id),
                        t.timestamp.timestamp() + lag,
                        book_max_staleness_seconds,
                    )
                    if quoted is None:
                        skipped["no_book"] += 1
                        continue
                if quoted is not None and drift > 0 and abs(quoted - t.price) > drift:
                    skipped["price_drift"] += 1
                    continue                       # edge already arbitraged away
                # Fill on the book we found, not on the leader's print.
                entry = (t.price if quoted is None else quoted) * (1 + slip)
                # The executor's limit: never pay more than the leader's price
                # plus the drift budget. Checked on the FILL, so it bites where
                # the drift guard alone does not — slippage is added after it.
                if quoted is not None and drift > 0 and entry > t.price + drift:
                    skipped["entry_limit"] += 1
                    continue
                sized = risk.size(Signal(
                    t.market_id, t.token_id, t.outcome, Side.BUY, t.price,
                    t.usd_size, "backtest", source_leader=t.leader.lower(), source_uid=t.uid,
                ))
                if sized is None:
                    skipped["bankroll_or_caps"] += 1
                    continue
                entry = min(max(entry, _MIN_PRICE), _MAX_PRICE)
                shares = sized.size_usd / entry
                ledger.record_fill(Fill(
                    signal=sized, fill_price=entry, size_usd=sized.size_usd,
                    shares=shares, timestamp=t.timestamp, mode="backtest",
                ))
                tr = tranches.get(lkey)
                if tr is None or lkey in settled:
                    settled.discard(lkey)
                    tr = _Tranche(market=market, outcome=t.outcome, entry_ts=t.timestamp)
                    tranches[lkey] = tr
                    heapq.heappush(pending, (_resolves(t.token_id, market), seq, lkey, winner))
                    seq += 1
                tr.shares += shares
                tr.bought += shares
                tr.cost += sized.size_usd
                arm_stop(lkey, t.timestamp)        # avg moved: recompute trigger
            else:
                tr = tranches.get(lkey)
                if tr is None or lkey in settled or tr.shares <= 1e-9:
                    continue                       # nothing of ours to mirror-sell
                quoted_exit = None
                if book is not None:
                    quoted_exit = observed_price(
                        book.get(t.token_id),
                        t.timestamp.timestamp() + lag,
                        book_max_staleness_seconds,
                    )
                    if quoted_exit is None:
                        # No bid to hit. Live leaves the uid unretired and the
                        # position rides into resolution, which is the honest
                        # answer: we could not have sold either.
                        skipped["exit_no_book"] += 1
                        continue
                fraction = 1.0 if prior <= 1e-9 else min(1.0, t.shares / prior)
                sell_shares = tr.shares * fraction
                # Mirrors ExactCopyStrategy's dust sweep. Tranches are keyed
                # (leader, token), so tr.shares is already this leader's own
                # slice — the multi-leader clamp the live path needs is
                # structural here.
                if s.sweep_exit_dust:
                    residual_value = (tr.shares - sell_shares) * tr.avg
                    if 0 < residual_value < s.exit_dust_usd:
                        sell_shares = tr.shares
                exit_base = t.price if quoted_exit is None else quoted_exit
                exit_price = min(max(exit_base * (1 - slip), _MIN_PRICE), _MAX_PRICE)
                tr.realized += (exit_price - tr.avg) * sell_shares
                tr.shares -= sell_shares
                ledger.record_fill(Fill(
                    signal=Signal(t.market_id, t.token_id, t.outcome, Side.SELL,
                                  exit_price, exit_price * sell_shares, "backtest-exit",
                                  source_leader=t.leader.lower(), source_uid=t.uid),
                    fill_price=exit_price, size_usd=exit_price * sell_shares,
                    shares=sell_shares, timestamp=t.timestamp, mode="backtest",
                ))
                arm_stop(lkey, t.timestamp)        # position shrank (maybe to zero)

        # Drain: stops still pending fire at their own time, ahead of resolution.
        if pending or armed:
            horizon = max(
                [p[0] for p in pending] + [sts for sts, _ in armed.values()]
            )
            settle_until(horizon)
        while pending:
            rts, _, key, winner = heapq.heappop(pending)
            finish(key, rts, winner)

        ledger.close()
        return BacktestReport(
            results=results, starting_bankroll=s.bankroll_usd,
            title="exact copy (entries + mirrored exits)",
            note="mirrors leader exits; leftovers settle at resolution; resolved markets only.",
            skipped=dict(skipped), candidate_entries=report_candidates,
        )
