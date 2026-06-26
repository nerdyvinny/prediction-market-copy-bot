"""Backtest harness for Strategy #4.

Replays leaders' *historical* BUY entries in markets that have since resolved,
as if we had copied them (our sizing + slippage, held to resolution). It reuses
the live RiskManager and ledger accounting, and frees exposure as positions
resolve in time order — so bankroll/caps behave like they would live.

Only resolved markets with a decisive winner are included (that's the only way
to know the true payout). Use this to vet auto-selected leaders before the
optional live window.
"""

from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from pmbot.config import Settings, get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.models import Fill, Market, Side, Signal
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
            "=== Backtest: Strategy #4 (long-term copy) ===",
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
        lines.append("\nNote: assumes hold-to-resolution; only resolved markets included.")
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
                self._mkt_cache[condition_id] = (None, None)
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
