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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from pmbot.config import Settings, get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.models import Fill, LeaderTrade, Market, Side, Signal
from pmbot.portfolio.ledger import Ledger
from pmbot.risk import RiskManager

log = logging.getLogger(__name__)

_MIN_PRICE = 1e-4
_MAX_PRICE = 1 - 1e-4


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
        stop_loss_frac: float | None = None,
        price_series: dict[str, list[tuple[int, float]]] | None = None,
        resolve_at: dict[str, datetime] | None = None,
    ) -> BacktestReport:
        """Simulate the tapes over the window [now - lookback_days, now].

        `now` also upper-bounds the tape, so walk-forward splits work: tune on
        an earlier window (pass a past `now`), validate on the recent one.
        `settings` overrides sizing knobs (copy_fraction, caps, bankroll) per
        run so allocation sweeps don't need a backtester per combination.
        `leader_weights` are per-leader copy_fraction multipliers (see
        `vet_weights`); `min_hours_to_resolution` skips entries placed within
        that many hours of the market's close (in-game/near-resolution trades
        are the ones our live lag copies worst).

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
        """
        s = settings or self.s
        slip = self.slip if slippage_bps is None else slippage_bps / 10_000
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=lookback_days)
        p_min = s.copy_price_min if price_min is None else price_min
        p_max = s.copy_price_max if price_max is None else price_max

        all_trades = [t for tape in tapes.values() for t in tape if cutoff <= t.timestamp <= now]
        all_trades.sort(key=lambda t: t.timestamp)

        ledger = Ledger(":memory:")
        risk = RiskManager(ledger, s)
        if leader_weights:
            risk.set_leader_weights(leader_weights)
        leader_pos: dict[tuple[str, str], float] = {}   # leader's own share count
        tranches: dict[tuple[str, str], _Tranche] = {}  # our copied holdings
        results: list[CopyResult] = []
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
            delta = t.shares if t.side is Side.BUY else -t.shares
            leader_pos[lkey] = max(0.0, prior + delta)

            if t.side is Side.BUY:
                if not (p_min <= t.price <= p_max):
                    continue
                if t.usd_size < min_leader_notional:
                    continue
                market, winner = self._market(t.market_id)
                if market is None or not market.closed or winner is None or market.end_date is None:
                    continue                       # only resolved markets have a known payout
                # Only enforce when the knob is ON (matches ExactCopyStrategy).
                # Daily-sports markets carry end_date at START of day, so every
                # in-game trade has NEGATIVE hours_left; with the knob at its
                # default 0 the unguarded compare silently dropped all of them,
                # while the live bot copies them — backtests wildly undercounted
                # for sports-heavy leaders.
                if min_hours_to_resolution > 0:
                    hours_left = (market.end_date - t.timestamp).total_seconds() / 3600
                    if hours_left < min_hours_to_resolution:
                        continue
                sized = risk.size(Signal(
                    t.market_id, t.token_id, t.outcome, Side.BUY, t.price,
                    t.usd_size, "backtest", source_leader=t.leader.lower(), source_uid=t.uid,
                ))
                if sized is None:
                    continue
                entry = min(max(t.price * (1 + slip), _MIN_PRICE), _MAX_PRICE)
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
                exit_price = min(max(t.price * (1 - slip), _MIN_PRICE), _MAX_PRICE)
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
        )
