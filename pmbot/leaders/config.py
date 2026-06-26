"""Loader for the leader-selection config (pmbot/config/leaders.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "leaders.yaml"


@dataclass
class SelectionConfig:
    top_n: int = 2
    rescore_interval_hours: float = 24.0


@dataclass
class FilterConfig:
    lookback_days: int = 90
    min_resolved_trades: int = 100        # min number of observed trades (sample size)
    min_win_rate: float = 0.55
    min_realized_pnl_usd: float = 0.0
    min_distinct_categories: int = 2      # breadth proxy (distinct event groups)
    min_avg_market_liquidity_usd: float = 5000.0
    max_position_concentration: float = 0.40


@dataclass
class LeaderConfig:
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "realized_pnl": 0.35, "win_rate": 0.25, "consistency": 0.20, "recency": 0.20
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
    return LeaderConfig(
        selection=SelectionConfig(
            top_n=int(sel.get("top_n", 2)),
            rescore_interval_hours=float(sel.get("rescore_interval_hours", 24)),
        ),
        filters=FilterConfig(
            lookback_days=int(flt.get("lookback_days", 90)),
            min_resolved_trades=int(flt.get("min_resolved_trades", 100)),
            min_win_rate=float(flt.get("min_win_rate", 0.55)),
            min_realized_pnl_usd=float(flt.get("min_realized_pnl_usd", 0)),
            min_distinct_categories=int(flt.get("min_distinct_categories", 2)),
            min_avg_market_liquidity_usd=float(flt.get("min_avg_market_liquidity_usd", 5000)),
            max_position_concentration=float(flt.get("max_position_concentration", 0.40)),
        ),
        weights={**LeaderConfig().weights, **(raw.get("weights") or {})},
        allowlist=[str(w).lower() for w in (raw.get("allowlist") or [])],
        blocklist=[str(w).lower() for w in (raw.get("blocklist") or [])],
    )
