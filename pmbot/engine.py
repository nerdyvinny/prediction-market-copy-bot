"""Engine: the poll loop tying discovery -> strategy -> risk -> executor -> ledger.

Paper-mode only for now (the executor it builds is the PaperExecutor). Live
execution is gated and added in Phase 6.

Components are injectable so the engine is testable and so a smoke script can
run it with small API caps.
"""

from __future__ import annotations

import logging
import time

from pmbot.config import Settings, get_settings
from pmbot.data import GammaClient, PolymarketDataClient, PriceCache
from pmbot.execution import PaperExecutor
from pmbot.execution.executor import TradeExecutor
from pmbot.leaders import load_leader_config
from pmbot.leaders.scoring import LeaderScore, LeaderSelector
from pmbot.portfolio.ledger import Ledger
from pmbot.risk import RiskManager
from pmbot.strategy import LongTermCopyStrategy

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
        strategy: LongTermCopyStrategy | None = None,
    ):
        self.settings = settings or get_settings()
        self.data = data or PolymarketDataClient()
        self.gamma = gamma or GammaClient()
        self.price_cache = price_cache or PriceCache()
        self.ledger = ledger or Ledger(self.settings.db_path)
        self.executor = executor or PaperExecutor(self.ledger, self.price_cache)
        self.risk = risk or RiskManager(self.ledger, self.settings)
        self.selector = selector or LeaderSelector(self.data, self.gamma)
        self.strategy = strategy or LongTermCopyStrategy(
            self.data, self.gamma, self.ledger, leaders=[]
        )

        self.leaders: list[LeaderScore] = []
        self._rescore_interval = load_leader_config().selection.rescore_interval_hours * 3600
        self._last_rescore = 0.0

    # -- steps -----------------------------------------------------------
    def rescore(self) -> list[LeaderScore]:
        """Re-rank the leaderboard and update the strategy's watchlist."""
        ranked = self.selector.select()
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

    def poll_once(self) -> tuple[int, int]:
        """One cycle: generate -> size -> execute. Returns (fills, signals)."""
        signals = list(self.strategy.generate())
        fills = 0
        for sig in signals:
            sized = self.risk.size(sig)
            if sized is None:
                continue
            if self.executor.execute(sized) is not None:
                fills += 1
        return fills, len(signals)

    # -- loop ------------------------------------------------------------
    def run(self, max_cycles: int | None = None) -> None:
        print(f"[pmbot] {self.executor.mode} mode | bankroll ${self.settings.bankroll_usd:.0f} "
              f"| poll {self.settings.poll_interval_seconds:.0f}s")
        print("Reading public data only; no real orders are placed in paper mode.\n")

        cycle = 0
        try:
            while max_cycles is None or cycle < max_cycles:
                now = time.monotonic()
                if not self.leaders or (now - self._last_rescore) >= self._rescore_interval:
                    print("· rescoring leaders…")
                    self.rescore()
                    self._last_rescore = now
                    self._print_leaders()

                fills, n = self.poll_once()
                realized = self.ledger.realized_pnl_total()
                open_positions = len(self.ledger.get_positions())
                print(f"· cycle {cycle}: {n} signals, {fills} paper fills | "
                      f"open={open_positions} realized=${realized:,.2f}")

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
        for c in (self.data, self.gamma, self.price_cache):
            try:
                c.close()
            except Exception:
                pass
        try:
            self.ledger.close()
        except Exception:
            pass
