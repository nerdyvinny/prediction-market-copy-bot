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
from datetime import datetime, timedelta, timezone

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
        # Dedicated HTTP clients for the rescore path. Scoring fans out to 6
        # threads and vetting pages whole tapes, all of it on the background
        # worker — sharing the copy loop's clients put thousands of scoring
        # requests through the same connection pool as the latency-critical
        # poll, for ~15 minutes a day. Every second of 429 backoff there comes
        # straight out of the 0.03 drift budget that decides whether a trade is
        # still copyable. Only built when we own the selector (an injected one
        # brings its own clients, as tests do).
        self._rescore_data: PolymarketDataClient | None = None
        self._rescore_gamma: GammaClient | None = None
        if selector is None:
            self._resolution_store = ResolutionStore(self.settings.db_path)
            self._record_store = RecordStore(self.settings.db_path)
            self._rescore_data = PolymarketDataClient()
            self._rescore_gamma = GammaClient()
            selector = LeaderSelector(
                self._rescore_data, self._rescore_gamma,
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
        # None = never settled, so the first sweep is due immediately. This was
        # 0.0, compared against `time.monotonic()` — seconds since BOOT on
        # Linux — so on a freshly rebooted VM the first sweep was skipped until
        # the box had been up a full `settle_interval`, pinning the bankroll of
        # every already-resolved position for up to half an hour.
        self._last_settle: float | None = None
        # Per-leader sizing weights computed during a background rescore and
        # applied on the main thread (see `_apply_rescore`).
        self._pending_weights: dict[str, float] | None = None

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
        # Install vet weights only once the result is actually adopted.
        if self._pending_weights is not None:
            self.risk.set_leader_weights(self._pending_weights)
            for w, mult in sorted(self._pending_weights.items(), key=lambda x: -x[1]):
                log.info("vet: weight %.2fx for %s", mult, w[:10])
            self._pending_weights = None
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
        """Keep only leaders PROVEN copyable — recently, and before that too.

        Scoring measures the LEADER's profit; this measures OURS, after our
        sizing, caps and slippage. Three things now have to hold, and a wallet
        that cannot demonstrate all three is dropped rather than kept:

        1. Enough copyable resolved trades to judge at all (`min_trades`).
           This used to fail OPEN — "no evidence either way, keep" — which let
           a wallet that makes zero trades we would mirror sit in the lineup
           indefinitely, and let brand-new wallets in with no record at all.
        2. Recent copy P&L at or above the floor.
        3. An OLDER, non-overlapping window that does not lose more than
           `oos_floor` (one maximum position by default). One window cannot
           separate skill from a hot streak: the same 30-45 days that surface a
           wallet are then used to score it, so the test is in-sample by
           construction and a lucky month passes it. Requiring the preceding
           window to hold up is the cheapest available out-of-sample check.

           That window is judged with SLACK, not against zero. It slides
           forward daily — dropping trades off the back, picking up newer ones
           at the front — so its P&L wanders even when the leader has not
           changed. Against a $0 floor that wander became a firing pin: it
           dropped 0x5cd5c8d7 at -$17.13 on the same day its recent window hit
           a best-ever +$127.75, and 0x06a22231 at -$4.51. Every leader the
           rule kept sat between +$14 and +$1,570, so zero was only ever
           clipping noise.
        4. Both windows TOGETHER at or above the recent floor, so the slack in
           (3) cannot let a hot streak paper over a genuinely bad history —
           which is the entire reason for looking back.

        Allowlisted wallets skip all of it: the pin exists precisely for the
        case where this judgement was wrong, so letting vetting overturn it
        would make the override useless.

        The lineup is whatever survives — `selection.top_n` is a ceiling, not a
        quota, so a day with three proven wallets follows three.
        """
        # Same tape depth as selection (trades_cap 1500): at 500 a heavy
        # trader's vet window spanned only days, came back "no copyable
        # resolved trades", and fail-open kept them — the most active leaders
        # were effectively unvetted. The consistency window needs more history
        # still, so the tape has to cover oos_lookback_days, not just lookback.
        s = self.settings
        # Runs on the rescore worker, so it uses the rescore clients too (see
        # __init__); falls back to the shared ones when a selector was injected.
        vetter = ExactCopyBacktester(
            self._rescore_data or self.data,
            self._rescore_gamma or self.gamma,
            s, trades_limit=4000,
        )
        kept: list[LeaderScore] = []
        rois: dict[str, float] = {}
        now = datetime.now(timezone.utc)
        recent_days = s.copy_vet_lookback_days
        # Slack on the sliding older window, in dollars. Defaults to one
        # maximum position: a prior window that lost less than a single bet
        # cannot distinguish a bad leader from window drift.
        oos_floor = (
            -abs(s.max_per_market_usd) if s.copy_vet_oos_min_pnl_usd is None
            else s.copy_vet_oos_min_pnl_usd
        )
        # Allowlisted wallets are a deliberate manual override — they already
        # bypass the selection filters, and bypassing scoring only to be
        # dropped by vetting made the override useless in the one case it
        # exists for (re-adding a leader the automation got wrong).
        allow = {w.lower() for w in load_leader_config().allowlist}
        for r in ranked:
            if r.wallet.lower() in allow:
                log.info("vet: %s KEPT (allowlisted — vetting bypassed)", r.wallet[:10])
                kept.append(r)
                continue
            try:
                tapes = vetter.fetch_tapes([r.wallet])

                def _sim(days: int, end: datetime) -> dict:
                    return vetter.simulate(
                        tapes, lookback_days=days, now=end,
                        min_leader_notional=s.copy_min_leader_notional_usd,
                        skip_round_tripped_entries=s.skip_round_tripped_entries,
                    ).metrics()

                m = _sim(recent_days, now)
            except Exception as e:
                if s.copy_vet_fail_open:
                    log.warning("vet: backtest failed for %s (%s); keeping", r.wallet[:10], e)
                    kept.append(r)
                else:
                    log.warning("vet: backtest failed for %s (%s); SKIPPING this "
                                "rescore (unproven)", r.wallet[:10], e)
                continue

            if m["n_trades"] < s.copy_vet_min_trades:
                log.info("vet: %s DROPPED (only %d copyable resolved trades, need %d)",
                         r.wallet[:10], m["n_trades"], s.copy_vet_min_trades)
                continue
            if m["net_pnl"] < s.copy_vet_min_pnl_usd:
                log.info("vet: %s DROPPED (copy-pnl $%.2f over %d trades)",
                         r.wallet[:10], m["net_pnl"], m["n_trades"])
                continue

            if s.copy_vet_require_consistency:
                # Older window: [now - oos_lookback, now - lookback]. Ending it
                # at the start of the recent window keeps the two disjoint, so
                # a single streak cannot satisfy both.
                span = max(1, s.copy_vet_oos_lookback_days - recent_days)
                try:
                    prior = _sim(span, now - timedelta(days=recent_days))
                except Exception as e:
                    log.warning("vet: %s prior-window backtest failed (%s); "
                                "SKIPPING this rescore", r.wallet[:10], e)
                    continue
                if prior["n_trades"] < s.copy_vet_oos_min_trades:
                    log.info("vet: %s DROPPED (no track record before the scoring "
                             "window — %d trades in the prior %dd)",
                             r.wallet[:10], prior["n_trades"], span)
                    continue
                if prior["net_pnl"] < oos_floor:
                    log.info("vet: %s DROPPED (recent $%.2f, but lost $%.2f over "
                             "the prior %dd — past the $%.2f slack)",
                             r.wallet[:10], m["net_pnl"], prior["net_pnl"],
                             span, oos_floor)
                    continue
                combined = m["net_pnl"] + prior["net_pnl"]
                if s.copy_vet_require_combined and combined < s.copy_vet_min_pnl_usd:
                    log.info("vet: %s DROPPED (recent $%.2f + prior $%.2f = $%.2f "
                             "across both windows — not profitable overall)",
                             r.wallet[:10], m["net_pnl"], prior["net_pnl"], combined)
                    continue
                log.info("vet: %s KEPT (recent $%.2f/%dt, prior $%.2f/%dt)",
                         r.wallet[:10], m["net_pnl"], m["n_trades"],
                         prior["net_pnl"], prior["n_trades"])
            else:
                log.info("vet: %s KEPT (copy-pnl $%.2f over %d trades)",
                         r.wallet[:10], m["net_pnl"], m["n_trades"])

            kept.append(r)
            invested = m.get("invested", 0.0)
            if invested > 0:
                rois[r.wallet] = m["net_pnl"] / invested

        log.info("vet: %d of %d candidates proven copyable", len(kept), len(ranked))

        # Stage the weights; `_apply_rescore` installs them on the MAIN thread.
        # Writing straight to `self.risk` here breaks the promise in
        # `_compute_rescore`'s docstring — this runs on the background rescore
        # worker — and applied weights even when the result was then discarded.
        if self.settings.copy_weight_by_vet:
            self._pending_weights = vet_weights(rois)
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
        self._pending_weights = None             # drop any from a rejected run

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
            retry_delay = min(self._rescore_retry_s, self._rescore_interval)
            if self._rescore_error is not None:
                log.error("rescore failed: %s", self._rescore_error)
                self._next_rescore = now + retry_delay
            else:
                # The deadline is armed in `finally`: if adopting the result or
                # printing the report throws, an un-advanced deadline would
                # start a fresh ~15-minute network-heavy rescore on EVERY 10s
                # cycle instead of backing off once.
                ok = False
                try:
                    ok = self._apply_rescore(self._rescore_result or [])
                    self._print_funnel_report()
                    self._print_leaders()
                finally:
                    self._next_rescore = now + (
                        self._rescore_interval if ok else retry_delay
                    )
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

                    if (self._last_settle is None
                            or (now - self._last_settle) >= self._settle_interval):
                        # Arm the next deadline BEFORE sweeping: a throw here
                        # would otherwise leave it un-advanced and retry the
                        # whole sweep every cycle instead of every interval.
                        self._last_settle = now
                        n_settled = self.settler.settle_open_positions()
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
        for c in (self.data, self.gamma, self.price_cache, self.kalshi,
                  self._rescore_data, self._rescore_gamma):
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
