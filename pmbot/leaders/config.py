"""Loader for the leader-selection config (pmbot/config/leaders.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "leaders.yaml"


@dataclass
class SelectionConfig:
    top_n: int = 8
    rescore_interval_hours: float = 24.0
    # top_n is a RELATIVE cut, so a leader that got no worse is evicted the
    # moment the eligible pool grows. With this on, a leader we already follow
    # keeps its seat instead of being out-ranked by a newcomer; it still has to
    # pass every filter and the copy vetting, so only failing a test loses a
    # seat.
    keep_incumbents: bool = True
    # ...but incumbency must never fill the whole lineup, or the funnel stops
    # discovering. This many seats stay contestable on every rescore.
    explore_slots: int = 2
    # Copyable BUYs that count as "enough of this tape is mirrorable". The
    # copyability score saturates here rather than rewarding raw volume --
    # see rank_wallets.
    copyable_target: int = 40
    # Absolute anchors for the realized_pnl term, log-spaced between them.
    # OFF by default (0 = fall back to pool-relative min-max) -- the scale is
    # sound but measured WORSE on live outcomes; see leaders.yaml.
    pnl_floor_usd: float = 0.0
    pnl_target_usd: float = 0.0


@dataclass
class FilterConfig:
    # One window for everything: profit, win rate, activity, concentration.
    lookback_days: int = 30
    min_trades: int = 50                  # activity floor within the window
    min_resolved_markets: int = 25        # sample size behind the win rate
    min_win_rate: float = 0.80            # over resolved markets, always enforced
    min_realized_pnl_usd: float = 0.0     # must be net-positive
    min_distinct_categories: int = 1      # single-market specialists allowed
    # Largest single market's profit as a share of total profit. Catches
    # "one lucky whale bet" wallets that volume-based concentration missed.
    max_profit_concentration: float = 0.40
    # Dead-account guard: last trade must be at most this old.
    max_hours_since_last_trade: float = 48.0
    # The style gate: BUYs in the window that the copy side would actually
    # mirror (>= copy_min_leader_notional_usd, inside the price band). A
    # 100%-win-rate penny-grinder bot with zero such trades is unfollowable —
    # every one of its bets dies at our conviction floor.
    min_copyable_trades: int = 5
    # HFT guard: if a wallet's fetched tape hit the trades cap AND spans fewer
    # days than this, it trades far too fast to be a market-picker — its edge
    # is latency (in-game sports sniping, market making), which cannot survive
    # our polling delay, and a tape that shallow can't be vetted either. An
    # UNCAPPED short tape is just a quiet wallet and is not rejected here.
    min_tape_span_days: float = 7.0


@dataclass
class LeaderConfig:
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "realized_pnl": 0.25, "win_rate": 0.25, "consistency": 0.0,
            "recency": 0.20, "copyability": 0.30,
        }
    )
    allowlist: list[str] = field(default_factory=list)
    blocklist: list[str] = field(default_factory=list)


def load_leader_config(path: str | Path | None = None) -> LeaderConfig:
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        return LeaderConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    sel = raw.get("selection", {}) or {}
    flt = raw.get("filters", {}) or {}
    d = FilterConfig()
    return LeaderConfig(
        selection=SelectionConfig(
            top_n=int(sel.get("top_n", 8)),
            rescore_interval_hours=float(sel.get("rescore_interval_hours", 24)),
            keep_incumbents=bool(sel.get("keep_incumbents", True)),
            explore_slots=int(sel.get("explore_slots", 2)),
            copyable_target=int(sel.get("copyable_target", 40)),
            pnl_floor_usd=float(sel.get("pnl_floor_usd", 0.0)),
            pnl_target_usd=float(sel.get("pnl_target_usd", 0.0)),
        ),
        filters=FilterConfig(
            lookback_days=int(flt.get("lookback_days", d.lookback_days)),
            min_trades=int(flt.get("min_trades", d.min_trades)),
            min_resolved_markets=int(
                flt.get("min_resolved_markets", d.min_resolved_markets)
            ),
            min_win_rate=float(flt.get("min_win_rate", d.min_win_rate)),
            min_realized_pnl_usd=float(
                flt.get("min_realized_pnl_usd", d.min_realized_pnl_usd)
            ),
            min_distinct_categories=int(
                flt.get("min_distinct_categories", d.min_distinct_categories)
            ),
            max_profit_concentration=float(
                flt.get("max_profit_concentration", d.max_profit_concentration)
            ),
            max_hours_since_last_trade=float(
                flt.get("max_hours_since_last_trade", d.max_hours_since_last_trade)
            ),
            min_copyable_trades=int(
                flt.get("min_copyable_trades", d.min_copyable_trades)
            ),
            min_tape_span_days=float(
                flt.get("min_tape_span_days", d.min_tape_span_days)
            ),
        ),
        weights={**LeaderConfig().weights, **(raw.get("weights") or {})},
        allowlist=[str(w).lower() for w in (raw.get("allowlist") or [])],
        blocklist=[str(w).lower() for w in (raw.get("blocklist") or [])],
    )
