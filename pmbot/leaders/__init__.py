"""Leaders: feed-profiled discovery + wallet scoring (auto-selection)."""

from pmbot.leaders.config import FilterConfig, LeaderConfig, SelectionConfig, load_leader_config
from pmbot.leaders.discovery import FeedProfile, harvest_candidates, profile_candidates
from pmbot.leaders.scoring import (
    LeaderScore,
    LeaderSelector,
    WalletStats,
    compute_wallet_stats,
    copyability,
    failing_filters,
    passes_filters,
    pnl_score,
    rank_wallets,
    recency_score,
    seat_roster,
    win_rate_score,
)

__all__ = [
    "FilterConfig",
    "LeaderConfig",
    "SelectionConfig",
    "load_leader_config",
    "FeedProfile",
    "harvest_candidates",
    "profile_candidates",
    "LeaderScore",
    "LeaderSelector",
    "WalletStats",
    "compute_wallet_stats",
    "copyability",
    "failing_filters",
    "passes_filters",
    "pnl_score",
    "rank_wallets",
    "recency_score",
    "seat_roster",
    "win_rate_score",
]
