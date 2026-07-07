"""Engine: the poll loop tying discovery -> strategy -> risk -> executor -> ledger.

Paper-mode only for now (the executor it builds is the PaperExecutor). Live
execution is gated and added in Phase 6.

Components are injectable so the engine is testable and so a smoke script can
run it with small API caps.
"""

from __future__ import annotations

import logging
import time

from pmbot.backtest import ExactCopyBacktester, vet_weights
from pmbot.config import Settings, get_settings
from pmbot.data import GammaClient, KalshiClient, PolymarketDataClient, PriceCache
from pmbot.execution import PaperExecutor
from pmbot.execution.executor import TradeExecutor
from pmbot.leaders import load_leader_config
from pmbot.leaders.scoring import LeaderScore, LeaderSelector
from pmbot.models import Signal
from pmbot.portfolio.ledger import Ledger
from pmbot.portfolio.settlement import Settler
from pmbot.risk import RiskManager
from pmbot.strategy import ArbitrageStrategy, ExactCopyStrategy, Strategy

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        data: PolymarketDataClient | None = None,
        gamma: GammaClient | None = None,
        price_cache: PriceCache | None = None,
        ledger: Ledger | None = None,
        executor: TradeExecutor | None = None,
        risk: RiskManager | None = None,
        selector: LeaderSelector | None = None,
        strategy: Strategy | None = None,
        kalshi: KalshiClient | None = None,
        arb_strategy: ArbitrageStrategy | None = None,
    ):
        self.settings = settings or get_settings()
        self.data = data or PolymarketDataClient()
        self.gamma = gamma or GammaClient()
        self.price_cache = price_cache or PriceCache()
        self.ledger = ledger or Ledger(self.settings.db_path)
        self.executor = executor or PaperExecutor(self.ledger, self.price_cache)
        self.risk = risk or RiskManager(self.ledger, self.settings)
        self.selector = selector or LeaderSelector(
            self.data, self.gamma, top_markets=30, per_market=100
        )
        self.strategy = strategy or ExactCopyStrategy(
            self.data, self.gamma, self.ledger, leaders=[], price_cache=self.price_cache
        )
        self.kalshi = kalshi
        self.arb_strategy = arb_strategy
        if self.arb_strategy is None and self.settings.arb_enabled:
            self.kalshi = self.kalshi or KalshiClient()
            self.arb_strategy = ArbitrageStrategy(
                self.gamma, self.price_cache, self.kalshi, self.ledger
            )

        self.settler = Settler(self.ledger, self.gamma, self.kalshi)

        self.leaders: list[LeaderScore] = []
        self._rescore_interval = load_leader_config().selection.rescore_interval_hours * 3600
        self._last_rescore = 0.0
        self._settle_interval = self.settings.settle_interval_hours * 3600
        self._last_settle = 0.0

    # -- steps -----------------------------------------------------------
    def rescore(self) -> list[LeaderScore]:
        """Re-rank the leaderboard and update the strategy's watchlist."""
        ranked = self.selector.select()
        if self.settings.copy_vet_leaders:
            ranked = self._vet_leaders(ranked)
        self.leaders = ranked
        self.strategy.set_leaders([r.wallet for r in ranked])
        for r in ranked:
            st = r.stats
            log.info(
                "leader %s score=%.3f pnl=$%.0f win=%.0f%% mkts=%d/%d cats=%d",
                r.wallet[:10], r.score, st.realized_pnl, st.win_rate * 100,
                st.n_resolved_markets, st.n_markets, st.n_categories,
            )
        return ranked

    def _vet_leaders(self, ranked: list[LeaderScore]) -> list[LeaderScore]:
        """Keep only leaders whose recent tape backtests profitably as a copy.

        Scoring measures the LEADER's profit; this measures OURS — after our
        sizing, caps and slippage. Leaders with no copyable resolved trades in
        the window pass through (no evidence either way), as do vetting errors
        (fail open: vetting is a refinement, not a gate that can brick the bot).
        """
        vetter = ExactCopyBacktester(self.data, self.gamma, self.settings, trades_limit=500)
        kept: list[LeaderScore] = []
        rois: dict[str, float] = {}
        for r in ranked:
            try:
                rep = vetter.run(
                    [r.wallet],
                    lookback_days=self.settings.copy_vet_lookback_days,
                    min_leader_notional=self.settings.copy_min_leader_notional_usd,
                )
                m = rep.metrics()
            except Exception as e:
                log.warning("vet: backtest failed for %s (%s); keeping", r.wallet[:10], e)
                kept.append(r)
                continue
            if m["n_trades"] == 0:
                log.info("vet: %s no copyable resolved trades; keeping", r.wallet[:10])
                kept.append(r)
            elif m["net_pnl"] >= self.settings.copy_vet_min_pnl_usd:
                log.info("vet: %s copy-pnl $%.2f over %d trades; keeping",
                         r.wallet[:10], m["net_pnl"], m["n_trades"])
                kept.append(r)
                invested = m.get("invested", 0.0)
                if invested > 0:
                    rois[r.wallet] = m["net_pnl"] / invested
            else:
                log.info("vet: %s DROPPED (copy-pnl $%.2f over %d trades)",
                         r.wallet[:10], m["net_pnl"], m["n_trades"])

        if self.settings.copy_weight_by_vet:
            weights = vet_weights(rois)
            self.risk.set_leader_weights(weights)
            for w, mult in sorted(weights.items(), key=lambda x: -x[1]):
                log.info("vet: weight %.2fx for %s", mult, w[:10])
        return kept

    def poll_once(self) -> tuple[int, int]:
        """One cycle: generate -> size -> execute. Returns (fills, signals).

        Copy signals are sized individually; arbitrage legs arrive pre-sized
        in leg groups and execute both-or-neither. Per-signal/group failures
        are isolated so one bad market can't abort the cycle.
        """
        signals = list(self.strategy.generate())
        if self.arb_strategy is not None:
            try:
                signals.extend(self.arb_strategy.generate())
            except Exception as e:
                log.warning("poll: arb generate failed: %s", e)

        singles: list[Signal] = []
        groups: dict[str, list[Signal]] = {}
        for sig in signals:
            if sig.leg_group:
                groups.setdefault(sig.leg_group, []).append(sig)
            else:
                singles.append(sig)

        fills = 0
        for sig in singles:
            try:
                sized = self.risk.size(sig)
                if sized is None:
                    continue
                if self.executor.execute(sized) is not None:
                    fills += 1
            except Exception as e:
                log.warning("poll: failed on %s: %s", sig.token_id[:10], e)

        for group_id, legs in groups.items():
            try:
                if len(legs) < 2:
                    log.warning("poll: leg group %s incomplete; skipping", group_id[:20])
                    continue
                if not self.risk.check_group(legs):
                    continue
                done = self.executor.execute_group(legs)
                if done:
                    fills += len(done)
                    print(f"  ARB entered: {legs[0].reason}")
            except Exception as e:
                log.warning("poll: arb group %s failed: %s", group_id[:20], e)
        return fills, len(signals)

    # -- loop ------------------------------------------------------------
    def run(self, max_cycles: int | None = None) -> None:
        print(f"[pmbot] {self.executor.mode} mode | bankroll ${self.settings.bankroll_usd:.0f} "
              f"| poll {self.settings.poll_interval_seconds:.0f}s")
        print("Reading public data only; no real orders are placed in paper mode.\n")

        cycle = 0
        errors = 0
        try:
            while max_cycles is None or cycle < max_cycles:
                try:
                    now = time.monotonic()
                    if not self.leaders or (now - self._last_rescore) >= self._rescore_interval:
                        print("· rescoring leaders…")
                        self.rescore()
                        self._last_rescore = now
                        self._print_leaders()

                    if (now - self._last_settle) >= self._settle_interval:
                        n_settled = self.settler.settle_open_positions()
                        self._last_settle = now
                        if n_settled:
                            print(f"· settled {n_settled} resolved position(s)")

                    fills, n = self.poll_once()
                    s = self.ledger.summary()
                    print(f"· cycle {cycle}: {n} signals, {fills} paper fills | "
                          f"open={s['open_positions']} deployed=${s['deployed_usd']:,.0f} "
                          f"realized=${s['realized_pnl']:,.2f}")
                    errors = 0
                except Exception as e:
                    errors += 1
                    backoff = min(self.settings.poll_interval_seconds * errors, 120)
                    log.error("cycle %d failed (%s); backing off %.0fs", cycle, e, backoff)
                    time.sleep(backoff)
                    cycle += 1
                    continue

                cycle += 1
                if max_cycles is not None and cycle >= max_cycles:
                    break
                time.sleep(self.settings.poll_interval_seconds)
        except KeyboardInterrupt:
            print("\nStopped.")

    def _print_leaders(self) -> None:
        if not self.leaders:
            print("  (no leaders passed the filters — loosen thresholds in leaders.yaml)")
            return
        print("  following:")
        for r in self.leaders:
            st = r.stats
            print(f"    {r.wallet}  score={r.score:.2f}  "
                  f"pnl=${st.realized_pnl:,.0f}  win={st.win_rate*100:.0f}%  "
                  f"trades={st.n_trades}  resolved_mkts={st.n_resolved_markets}")

    def close(self) -> None:
        for c in (self.data, self.gamma, self.price_cache, self.kalshi):
            if c is None:
                continue
            try:
                c.close()
            except Exception:
                pass
        try:
            self.ledger.close()
        except Exception:
            pass
