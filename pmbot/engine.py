"""Engine: the poll loop tying discovery -> strategy -> risk -> executor -> ledger.

Paper-mode only for now (the executor it builds is the PaperExecutor). Live
execution is gated and added in Phase 6.

Components are injectable so the engine is testable and so a smoke script can
run it with small API caps.
"""

from __future__ import annotations

import logging
import threading
import time

from pmbot.backtest import ExactCopyBacktester, vet_weights
from pmbot.config import Settings, get_settings
from pmbot.data import (
    GammaClient,
    KalshiClient,
    PolymarketDataClient,
    PriceCache,
    ResolutionStore,
)
from pmbot.execution import PaperExecutor
from pmbot.execution.executor import TradeExecutor
from pmbot.leaders import load_leader_config
from pmbot.leaders.records import RecordStore
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
        self._resolution_store: ResolutionStore | None = None
        self._record_store: RecordStore | None = None
        if selector is None:
            self._resolution_store = ResolutionStore(self.settings.db_path)
            self._record_store = RecordStore(self.settings.db_path)
            selector = LeaderSelector(
                self.data, self.gamma,
                resolution_store=self._resolution_store,
                record_store=self._record_store,
            )
        self.selector = selector
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
        # Background-rescore state: the network-heavy scoring runs in a worker
        # thread so the copy loop never freezes for the ~15 minutes a full
        # rescore takes; the main loop applies the result between polls.
        self._rescore_retry_s = 900.0      # backoff after a failed/empty rescore
        self._next_rescore = 0.0           # monotonic deadline; 0 = due now
        self._rescore_thread: threading.Thread | None = None
        self._rescore_result: list[LeaderScore] | None = None
        self._rescore_error: Exception | None = None
        self._settle_interval = self.settings.settle_interval_hours * 3600
        self._last_settle = 0.0

    # -- steps -----------------------------------------------------------
    def rescore(self) -> list[LeaderScore]:
        """Synchronous re-rank + apply (CLI, tests, and the first-ever run).
        The live loop uses the background path in `run` instead."""
        ranked = self._compute_rescore(self._incumbents())
        self._apply_rescore(ranked)
        return ranked

    def _incumbents(self) -> list[str]:
        """Wallets that must always be deep-scored: currently followed leaders
        (in memory AND the follow list persisted by the last rescore), plus
        anyone we still hold copied positions from. Both sets survive restarts
        via the ledger — feed churn must never silently drop a known leader,
        even one we never got to copy a trade from. (Ledger access: main
        thread only.)"""
        incumbents = {r.wallet for r in self.leaders}
        try:
            incumbents |= set(self.ledger.leader_exposures())
            incumbents |= set(self.ledger.followed_leaders())
        except Exception as e:
            log.debug("rescore: incumbent seed from ledger failed: %s", e)
        return sorted(incumbents)

    def _compute_rescore(self, incumbents: list[str]) -> list[LeaderScore]:
        """The network-heavy part (selector + vetting). Touches no ledger
        state, so it is safe to run on the background rescore thread."""
        ranked = self.selector.select(incumbents=incumbents)
        if self.settings.copy_vet_leaders:
            ranked = self._vet_leaders(ranked)
        return ranked

    def _apply_rescore(self, ranked: list[LeaderScore]) -> bool:
        """Adopt a rescore result (main thread). Returns True if it was
        usable. An empty result (funnel starved or API trouble) keeps the
        current leaders AND the persisted list: copying continues on the last
        known-good set instead of stopping dead."""
        if not ranked:
            log.warning("rescore: empty result; keeping current leader set")
            if not self.leaders:
                self._restore_watchlist()
            return False
        self.leaders = ranked
        try:
            self.ledger.set_followed_leaders({r.wallet: r.score for r in ranked})
        except Exception as e:
            log.debug("rescore: persisting follow list failed: %s", e)
        # A leader who fell off the ranked list while we still hold positions
        # copied from them stays watched as exit-only: their SELLs are still
        # mirrored so those positions don't ride unmanaged to resolution, but
        # their BUYs never open new ones.
        held: set[str] = set()
        try:
            held = set(self.ledger.leader_exposures())
        except Exception as e:
            log.debug("rescore: exposure read failed: %s", e)
        followed = {r.wallet.lower() for r in ranked}
        exit_only = sorted(w.lower() for w in held if w.lower() not in followed)
        self.strategy.set_leaders([r.wallet for r in ranked], exit_only=exit_only)
        if exit_only:
            log.info("rescore: %d exit-only leader(s) with open positions: %s",
                     len(exit_only), ", ".join(w[:10] for w in exit_only))
        for r in ranked:
            st = r.stats
            log.info(
                "leader %s score=%.3f pnl=$%.0f win=%.0f%% mkts=%d/%d cats=%d copyable=%d",
                r.wallet[:10], r.score, st.realized_pnl, st.win_rate * 100,
                st.n_resolved_markets, st.n_markets, st.n_categories,
                st.n_copyable_trades,
            )
        return True

    def _restore_watchlist(self) -> bool:
        """Point the strategy at the persisted follow list (startup, or an
        empty rescore) so copying continues with the last known-good leaders
        while a fresh rescore runs. Returns True if anything was restored."""
        try:
            persisted = self.ledger.followed_leaders()
            held = set(self.ledger.leader_exposures())
        except Exception as e:
            log.debug("restore: ledger read failed: %s", e)
            return False
        if not persisted:
            return False
        followed = {w.lower() for w in persisted}
        exit_only = sorted(w.lower() for w in held if w.lower() not in followed)
        self.strategy.set_leaders(persisted, exit_only=exit_only)
        log.info("restored %d persisted leader(s) to the watchlist", len(persisted))
        return True

    def _vet_leaders(self, ranked: list[LeaderScore]) -> list[LeaderScore]:
        """Keep only leaders whose recent tape backtests profitably as a copy.

        Scoring measures the LEADER's profit; this measures OURS — after our
        sizing, caps and slippage. Leaders with no copyable resolved trades in
        the window pass through (no evidence either way), as do vetting errors
        (fail open: vetting is a refinement, not a gate that can brick the bot).
        """
        # Same tape depth as selection (trades_cap 1500): at 500 a heavy
        # trader's vet window spanned only days, came back "no copyable
        # resolved trades", and fail-open kept them — the most active leaders
        # were effectively unvetted.
        vetter = ExactCopyBacktester(self.data, self.gamma, self.settings, trades_limit=1500)
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

        Copy signals execute the moment the strategy yields them (streaming),
        never after the whole batch is collected: a leader's SELL can follow
        their BUY inside one batch, and it only finds our position to mirror
        if that BUY's fill is already in the ledger. (Batching here once
        consumed such SELLs unmirrored — the position then rode unmanaged to
        resolution.) Arbitrage legs still collect into leg groups and execute
        both-or-neither. Per-signal/group failures are isolated so one bad
        market can't abort the cycle.
        """
        fills = 0
        n_signals = 0
        groups: dict[str, list[Signal]] = {}

        def execute_single(sig: Signal) -> None:
            nonlocal fills
            try:
                sized = self.risk.size(sig)
                if sized is None:
                    return
                if self.executor.execute(sized) is not None:
                    fills += 1
            except Exception as e:
                log.warning("poll: failed on %s: %s", sig.token_id[:10], e)

        for sig in self.strategy.generate():
            n_signals += 1
            if sig.leg_group:
                groups.setdefault(sig.leg_group, []).append(sig)
            else:
                execute_single(sig)
        if self.arb_strategy is not None:
            try:
                for sig in self.arb_strategy.generate():
                    n_signals += 1
                    if sig.leg_group:
                        groups.setdefault(sig.leg_group, []).append(sig)
                    else:
                        execute_single(sig)
            except Exception as e:
                log.warning("poll: arb generate failed: %s", e)

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
        return fills, n_signals

    # -- background rescore ----------------------------------------------
    def _start_rescore(self) -> None:
        incumbents = self._incumbents()          # ledger reads on main thread
        self._rescore_result = None
        self._rescore_error = None

        def worker() -> None:
            try:
                self._rescore_result = self._compute_rescore(incumbents)
            except Exception as e:               # applied (with backoff) by the main loop
                self._rescore_error = e

        self._rescore_thread = threading.Thread(
            target=worker, name="pmbot-rescore", daemon=True
        )
        self._rescore_thread.start()

    def _rescore_tick(self) -> None:
        """Drive the background rescore from the main loop: adopt a finished
        result, and start a new run when one is due. Copying never pauses —
        a full rescore takes ~15 minutes and used to freeze the loop."""
        now = time.monotonic()
        t = self._rescore_thread
        if t is not None and not t.is_alive():
            self._rescore_thread = None
            if self._rescore_error is not None:
                log.error("rescore failed: %s", self._rescore_error)
                self._next_rescore = now + min(self._rescore_retry_s, self._rescore_interval)
            else:
                ok = self._apply_rescore(self._rescore_result or [])
                self._print_funnel_report()
                self._print_leaders()
                delay = self._rescore_interval if ok else min(self._rescore_retry_s,
                                                              self._rescore_interval)
                self._next_rescore = now + delay
        if self._rescore_thread is None and now >= self._next_rescore:
            print("· rescoring leaders in the background…")
            self._start_rescore()

    # -- loop ------------------------------------------------------------
    def run(self, max_cycles: int | None = None) -> None:
        print(f"[pmbot] {self.executor.mode} mode | bankroll ${self.settings.bankroll_usd:.0f} "
              f"| poll {self.settings.poll_interval_seconds:.0f}s")
        print("Reading public data only; no real orders are placed in paper mode.\n")

        # Keep copying from the last known-good leaders while the first
        # rescore runs in the background. Only a truly fresh start (nothing
        # persisted, nothing to copy) blocks on a synchronous first rescore.
        if not self.leaders and not self._restore_watchlist():
            print("· first rescore (no persisted leaders yet — takes a few minutes)…")
            ranked = self.rescore()
            self._print_funnel_report()
            self._print_leaders()
            self._next_rescore = time.monotonic() + (
                self._rescore_interval if ranked
                else min(self._rescore_retry_s, self._rescore_interval)
            )

        cycle = 0
        errors = 0
        try:
            while max_cycles is None or cycle < max_cycles:
                try:
                    now = time.monotonic()
                    self._rescore_tick()

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
        if self._rescore_thread is not None and self._rescore_thread.is_alive():
            # The daemon worker dies with the process; its half-done result is
            # discarded (an empty/failed rescore can't wipe the leader list).
            print("· background rescore was still in flight — abandoned; it reruns next start")

    def _print_funnel_report(self) -> None:
        """Show what each selection stage / filter rejected, so an empty pool
        points at the exact leaders.yaml knob to loosen instead of a guess."""
        rep = getattr(self.selector, "last_report", None)
        if not rep:
            return
        # rep["followed"] is pre-vetting; self.leaders is what survived it.
        print(f"  funnel: {rep['pool']} in pool -> {rep['deep_scored']} deep-scored "
              f"-> {rep['eligible']} eligible -> following {len(self.leaders)} after vet")
        if rep.get("record_wallets"):
            print(f"  records: {rep['record_wallets']} wallet(s) shortlistable from "
                  f"accumulated resolved-market history")
        rejects = rep.get("rejects") or {}
        if rejects:
            detail = ", ".join(f"{k}={v}" for k, v in
                               sorted(rejects.items(), key=lambda kv: -kv[1]))
            print(f"  rejected by: {detail}")

    def _print_leaders(self) -> None:
        if not self.leaders:
            print("  (no leaders passed the filters — loosen thresholds in leaders.yaml)")
            return
        print("  following:")
        for r in self.leaders:
            st = r.stats
            print(f"    {r.wallet}  score={r.score:.2f}  "
                  f"pnl=${st.realized_pnl:,.0f}  win={st.win_rate*100:.0f}%  "
                  f"trades={st.n_trades}  resolved_mkts={st.n_resolved_markets}  "
                  f"copyable={st.n_copyable_trades}")

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
        if self._resolution_store is not None:
            try:
                self._resolution_store.close()
            except Exception:
                pass
        if self._record_store is not None:
            try:
                self._record_store.close()
            except Exception:
                pass
