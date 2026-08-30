"""Engine: the poll loop tying roster -> strategy -> risk -> executor -> ledger.

Paper-mode only for now (the executor it builds is the PaperExecutor). Live
execution is gated and added in Phase 6.

The roster is STATIC. The bot no longer discovers, scores, ranks or vets
wallets while it runs: it copies the list in `leaders.yaml` and nothing else.

That is a deliberate reversal of the first design, and the reason is measured.
Over 34 days of paper trading (2026-07-27..08-30, 115 closed round-trips) the
correlation between a leader's trailing ROI *in our own fills* and their next
trade's ROI was -0.018. Wallets with a positive record with us went on to
return -11.3%; wallets with a negative record returned -9.6%. The automatic
lineup did not beat copying every candidate (p=0.717), and every wallet with
enough tape to split in half decayed from its first half to its second. The
selection funnel was ~1,250 lines predicting something that does not persist,
and it churned 33 wallets through 8 seats in a month, so nothing ever
accumulated enough evidence to judge.

Discovery and scoring still exist, as a research tool you run by hand
(`pmbot leaders`), and their output is advisory: a human edits the roster.
See `docs/roster.md`.

Components are injectable so the engine is testable and so a smoke script can
run it with small API caps.
"""

from __future__ import annotations

import logging
import time

from pmbot.config import Settings, get_settings
from pmbot.data import (
    GammaClient,
    KalshiClient,
    PolymarketDataClient,
    PriceCache,
)
from pmbot.execution import PaperExecutor
from pmbot.execution.executor import TradeExecutor
from pmbot.leaders import load_leader_config
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
        strategy: Strategy | None = None,
        kalshi: KalshiClient | None = None,
        arb_strategy: ArbitrageStrategy | None = None,
        roster: list[str] | None = None,
    ):
        self.settings = settings or get_settings()
        self.data = data or PolymarketDataClient()
        self.gamma = gamma or GammaClient()
        self.price_cache = price_cache or PriceCache()
        self.ledger = ledger or Ledger(self.settings.db_path)
        self.executor = executor or PaperExecutor(self.ledger, self.price_cache)
        self.risk = risk or RiskManager(self.ledger, self.settings)
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

        # The roster we copy. `roster=` is for tests and smoke scripts; the
        # live loop reads leaders.yaml.
        self._roster_override = roster
        self.leaders: list[str] = []
        self._settle_interval = self.settings.settle_interval_hours * 3600
        # None = never settled, so the first sweep is due immediately. This was
        # 0.0, compared against `time.monotonic()` — seconds since BOOT on
        # Linux — so on a freshly rebooted VM the first sweep was skipped until
        # the box had been up a full `settle_interval`, pinning the bankroll of
        # every already-resolved position for up to half an hour.
        self._last_settle: float | None = None

    # -- roster ----------------------------------------------------------
    def _configured_roster(self) -> list[str]:
        """The wallets we copy, lowercased and de-duplicated in file order."""
        if self._roster_override is not None:
            raw = self._roster_override
        else:
            raw = load_leader_config().roster
        seen: set[str] = set()
        out: list[str] = []
        for w in raw:
            k = w.strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(k)
        return out

    def install_roster(self) -> list[str]:
        """Point the strategy at the configured roster. Called once at startup.

        A wallet we still hold copied positions from, but which is no longer on
        the roster, becomes EXIT-ONLY: its SELLs are still mirrored so those
        positions don't ride unmanaged to resolution, but its BUYs never open
        new ones. That is what makes removing a line from leaders.yaml safe —
        you stop buying from them immediately and still get their exits.

        Editing the roster takes effect on restart; there is no background
        re-read, because the whole point of a static roster is that it only
        changes when a human changes it.
        """
        roster = self._configured_roster()
        held: set[str] = set()
        try:
            held = {w.lower() for w in self.ledger.leader_exposures()}
        except Exception as e:
            log.debug("roster: exposure read failed: %s", e)
        followed = set(roster)
        exit_only = sorted(w for w in held if w not in followed)

        self.leaders = roster
        self.strategy.set_leaders(roster, exit_only=exit_only)
        # The dashboard reads the follow list out of the ledger, so keep it in
        # step. Score is meaningless now — there is no ranking — so it is 0.
        try:
            self.ledger.set_followed_leaders({w: 0.0 for w in roster})
        except Exception as e:
            log.debug("roster: persisting follow list failed: %s", e)

        if not roster:
            log.warning("roster: leaders.yaml lists no wallets — nothing to copy")
        else:
            log.info("roster: copying %d leader(s): %s",
                     len(roster), ", ".join(w[:10] for w in roster))
        if exit_only:
            log.info("roster: %d exit-only leader(s) with open positions: %s",
                     len(exit_only), ", ".join(w[:10] for w in exit_only))
        return roster

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

    # -- loop ------------------------------------------------------------
    def run(self, max_cycles: int | None = None) -> None:
        print(f"[pmbot] {self.executor.mode} mode | bankroll ${self.settings.bankroll_usd:.0f} "
              f"| poll {self.settings.poll_interval_seconds:.0f}s")
        print("Reading public data only; no real orders are placed in paper mode.\n")

        self.install_roster()
        self._print_roster()

        cycle = 0
        errors = 0
        try:
            while max_cycles is None or cycle < max_cycles:
                try:
                    now = time.monotonic()

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

    def _print_roster(self) -> None:
        if not self.leaders:
            print("  (leaders.yaml lists no wallets — add some under `roster:`; "
                  "`pmbot leaders` suggests candidates)")
            return
        exit_only = getattr(self.strategy, "exit_only_leaders", []) or []
        print(f"  copying {len(self.leaders)} leader(s) from leaders.yaml:")
        for w in self.leaders:
            print(f"    {w}")
        if exit_only:
            print(f"  exit-only (held, no longer on the roster): {len(exit_only)}")
            for w in exit_only:
                print(f"    {w}")

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
