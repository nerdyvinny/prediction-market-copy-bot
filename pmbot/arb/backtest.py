"""Historical replay for Strategy #1: did cross-venue gaps exist, how big,
and what would entering them have earned?

For each RESOLVED pair, we rebuild an hourly timeline:

  Polymarket ask  ~ prices-history point + `pm_half_spread` (history is
                    last/mid, not ask — be honest about that)
  Kalshi yes ask  = candlestick yes_ask close;  no ask = 1 - yes_bid close

then apply the exact live rule (fees + buffer + min edge, one entry per
pair+direction) and settle at the venues' ACTUAL results.

The report also flags resolution mismatches — pairs where the venues did not
resolve consistently. Any hit there means the pair was NOT equivalent and
"arb" was actually two naked bets; treat it as a match-quality alarm, not a
P&L line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pmbot.arb.fees import size_pair
from pmbot.arb.matcher import ConfirmedPair
from pmbot.data import GammaClient, KalshiClient, PriceCache

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArbEntry:
    pair_uid: str
    ts: int
    pm_outcome: str
    kalshi_side: str
    pm_ask: float
    kalshi_ask: float
    contracts: float
    cost_usd: float
    fee_usd: float
    payout_usd: float          # from ACTUAL resolutions
    pnl_usd: float


@dataclass
class PairResult:
    pair: ConfirmedPair
    hours_observed: int = 0
    hours_with_gap: int = 0            # gross gap > 0 in either direction
    max_gross_gap: float = 0.0
    entries: list[ArbEntry] = field(default_factory=list)
    resolution_mismatch: bool = False
    skipped_reason: str | None = None

    @property
    def pnl(self) -> float:
        return sum(e.pnl_usd for e in self.entries)


@dataclass
class ArbBacktestReport:
    results: list[PairResult]
    lookback_days: int

    def summary_text(self) -> str:
        done = [r for r in self.results if r.skipped_reason is None]
        skipped = [r for r in self.results if r.skipped_reason is not None]
        entries = [e for r in done for e in r.entries]
        mismatches = [r for r in done if r.resolution_mismatch]
        total_cost = sum(e.cost_usd for e in entries)
        total_pnl = sum(e.pnl_usd for e in entries)
        total_fees = sum(e.fee_usd for e in entries)
        hours = sum(r.hours_observed for r in done)
        gap_hours = sum(r.hours_with_gap for r in done)

        lines = [
            f"=== Arb backtest ({self.lookback_days}d lookback) ===",
            f"  pairs replayed   : {len(done)} ({len(skipped)} skipped)",
            f"  hours observed   : {hours} ({gap_hours} with a positive gross gap, "
            f"{(gap_hours / hours * 100) if hours else 0:.1f}%)",
            f"  entries taken    : {len(entries)}",
            f"  capital used     : ${total_cost:,.2f} (fees ${total_fees:,.2f})",
            f"  simulated P&L    : ${total_pnl:,.2f}"
            + (f"  ({total_pnl / total_cost * 100:+.2f}% on capital)" if total_cost else ""),
        ]
        if mismatches:
            lines.append("  !! RESOLUTION MISMATCHES (pair NOT equivalent — do not trade):")
            for r in mismatches:
                lines.append(f"    - {r.pair.pm_market_id[:16]}… <> {r.pair.kalshi_ticker}")
        for r in done:
            tag = " !!MISMATCH" if r.resolution_mismatch else ""
            lines.append(
                f"  · {r.pair.kalshi_ticker:32s} gaps {r.hours_with_gap:>4d}/{r.hours_observed:<4d}h "
                f"max {r.max_gross_gap*100:4.1f}c  entries {len(r.entries)}  "
                f"P&L ${r.pnl:,.2f}{tag}"
            )
        for r in skipped:
            lines.append(f"  · {r.pair.kalshi_ticker:32s} skipped: {r.skipped_reason}")
        return "\n".join(lines)


class ArbBacktester:
    def __init__(
        self,
        gamma: GammaClient,
        prices: PriceCache,
        kalshi: KalshiClient,
        *,
        min_edge: float = 0.015,
        slippage_buffer: float = 0.005,
        max_per_trade_usd: float = 50.0,
        pm_half_spread: float = 0.01,
        lookback_days: int = 30,
        exclude_final_hours: int = 3,
    ):
        self.gamma = gamma
        self.prices = prices
        self.kalshi = kalshi
        self.min_edge = min_edge
        self.buffer = slippage_buffer
        self.max_usd = max_per_trade_usd
        self.pm_half_spread = pm_half_spread
        self.lookback_days = lookback_days
        # Skip the last hours before close: hourly snapshots from two venues
        # during a live event are minutes apart in reality, and fast in-play
        # moves make them look like gaps that were never executable.
        self.exclude_final_hours = exclude_final_hours

    def run(self, pairs: list[ConfirmedPair]) -> ArbBacktestReport:
        results = []
        for pair in pairs:
            try:
                results.append(self._replay_pair(pair))
            except Exception as e:
                log.warning("arb backtest: %s failed: %s", pair.kalshi_ticker, e)
                results.append(PairResult(pair=pair, skipped_reason=str(e)[:80]))
        return ArbBacktestReport(results=results, lookback_days=self.lookback_days)

    # -- per pair ------------------------------------------------------------
    def _replay_pair(self, pair: ConfirmedPair) -> PairResult:
        res = PairResult(pair=pair)

        pm, pm_winner = self.gamma.get_market_with_resolution(pair.pm_market_id)
        if pm is None:
            res.skipped_reason = "PM market not found"
            return res
        k = self.kalshi.get_market(pair.kalshi_ticker)
        if k is None:
            res.skipped_reason = "Kalshi market not found"
            return res
        if not pm.closed or pm_winner is None or k.result not in ("yes", "no"):
            res.skipped_reason = "not resolved yet (backtest needs settled pairs)"
            return res

        complement = next((o for o in pm.tokens if o != pair.pm_outcome), None)
        if complement is None or pair.pm_outcome not in pm.tokens:
            res.skipped_reason = "PM market not binary Yes/No"
            return res

        # Consistency audit: PM outcome won IFF the equivalent Kalshi side won.
        pm_out_won = pm.tokens[pair.pm_outcome] == pm_winner
        k_equiv = "yes" if pair.aligned else "no"
        k_equiv_won = k.result == k_equiv
        res.resolution_mismatch = pm_out_won != k_equiv_won

        # Timeline window: lookback ending at the market close.
        end_dt = k.close_time or datetime.now(timezone.utc)
        end_ts = int(end_dt.timestamp())
        start_ts = end_ts - self.lookback_days * 86400

        pm_hist = self.prices.get_price_history(
            pm.tokens[pair.pm_outcome], start_ts=start_ts, end_ts=end_ts
        )
        series = pair.kalshi_ticker.split("-")[0]
        candles = self.kalshi.get_candlesticks(
            series, pair.kalshi_ticker, start_ts=start_ts, end_ts=end_ts, period_interval=60
        )
        if not pm_hist or not candles:
            res.skipped_reason = "no overlapping history"
            return res

        pm_by_hour = {t // 3600: p for t, p in pm_hist}
        entered: set[str] = set()
        cutoff_hour = (end_ts // 3600) - self.exclude_final_hours

        for c in candles:
            hour = int(c.get("end_period_ts", 0)) // 3600
            if hour > cutoff_hour:
                continue                     # in-play window: snapshots unreliable
            pm_mid = pm_by_hour.get(hour)
            if pm_mid is None:
                continue
            k_yes_ask = _candle_dollars(c, "yes_ask")
            k_yes_bid = _candle_dollars(c, "yes_bid")
            if k_yes_ask is None or k_yes_bid is None:
                continue
            k_no_ask = round(1.0 - k_yes_bid, 4)
            res.hours_observed += 1

            # PM asks from mid + assumed half-spread, clamped to (0,1).
            pm_out_ask = min(pm_mid + self.pm_half_spread, 0.999)
            pm_comp_ask = min((1.0 - pm_mid) + self.pm_half_spread, 0.999)

            k_opp = "no" if pair.aligned else "yes"
            directions = [
                (pair.pm_outcome, pm_out_ask, k_opp,
                 k_no_ask if k_opp == "no" else k_yes_ask),
                (complement, pm_comp_ask, k_equiv,
                 k_no_ask if k_equiv == "no" else k_yes_ask),
            ]
            gap = max(1.0 - (pm_ask + k_ask) for _, pm_ask, _, k_ask in directions)
            if gap > 0:
                res.hours_with_gap += 1
                res.max_gross_gap = max(res.max_gross_gap, gap)

            for pm_outcome, pm_ask, k_side, k_ask in directions:
                if pm_outcome in entered or not (0 < pm_ask < 1 and 0 < k_ask < 1):
                    continue
                sized = size_pair(pm_ask, k_ask, max_usd=self.max_usd)
                if sized is None or sized.edge_per_pair - self.buffer < self.min_edge:
                    continue
                payout = self._payout(
                    sized.contracts, pm_outcome, pair, pm_winner, pm.tokens, k.result
                )
                res.entries.append(
                    ArbEntry(
                        pair_uid=pair.uid,
                        ts=hour * 3600,
                        pm_outcome=pm_outcome,
                        kalshi_side=k_side,
                        pm_ask=pm_ask,
                        kalshi_ask=k_ask,
                        contracts=sized.contracts,
                        cost_usd=sized.cost_usd,
                        fee_usd=sized.fee_usd,
                        payout_usd=payout,
                        pnl_usd=round(payout - sized.cost_usd, 4),
                    )
                )
                entered.add(pm_outcome)
        return res

    @staticmethod
    def _payout(
        contracts: float,
        pm_outcome: str,
        pair: ConfirmedPair,
        pm_winner: str,
        pm_tokens: dict[str, str],
        k_result: str,
    ) -> float:
        """Payout from the venues' ACTUAL resolutions (mismatch-safe).

        The Kalshi side held is the hedge of the PM outcome held: opposite of
        the equivalent side when pm_outcome == pair.pm_outcome, else the
        equivalent side itself.
        """
        k_equiv = "yes" if pair.aligned else "no"
        if pm_outcome == pair.pm_outcome:
            k_held = "no" if k_equiv == "yes" else "yes"
        else:
            k_held = k_equiv
        payout = 0.0
        if pm_tokens.get(pm_outcome) == pm_winner:
            payout += contracts
        if k_result == k_held:
            payout += contracts
        return payout


def _candle_dollars(c: dict, key: str) -> float | None:
    v = ((c.get(key) or {}).get("close_dollars"))
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 0.0 < f < 1.0 else None
