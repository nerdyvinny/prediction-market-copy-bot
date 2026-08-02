"""Env-driven configuration (pydantic-settings).

All values can be overridden via environment variables prefixed with `PMBOT_`
or a local `.env` file (see `.env.example`). Defaults are safe for paper mode.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class RunMode(str, Enum):
    PAPER = "paper"
    BACKTEST = "backtest"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PMBOT_",
        extra="ignore",
        case_sensitive=False,
    )

    mode: RunMode = RunMode.PAPER

    # Public, read-only endpoints (no auth required for paper/backtest).
    data_api_base: str = "https://data-api.polymarket.com"
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    clob_api_base: str = "https://clob.polymarket.com"
    kalshi_api_base: str = "https://api.elections.kalshi.com/trade-api/v2"

    # Poll loop. Every second of lag is edge lost to the drift guard, so poll
    # as fast as is polite to the public Data API (8 leader queries/cycle).
    poll_interval_seconds: float = 10.0
    # How often to check open positions for resolved markets (realize P&L).
    # Settling is cheap (one Gamma call per open position) and every hour a
    # resolved position sits unsettled is bankroll that can't fund new copies,
    # so check often.
    settle_interval_hours: float = 0.5

    # Capital & sizing. Walk-forward validated 2026-07 (tuned on the 45d
    # window ending 45d ago, validated on the most recent 45d): 0.10/100/400
    # netted ~2.9x the old 0.05/50/150 baseline out-of-sample ($1,471 vs $505
    # on a $500 bankroll), at a deeper max drawdown ($300 vs $130).
    bankroll_usd: float = 500.0
    copy_fraction: float = 0.10            # replicate this fraction of leader size
    max_per_market_usd: float = 100.0      # hard cap per market
    max_per_leader_usd: float = 400.0      # hard cap of exposure per leader
    # Redeploy realized profits: free bankroll is bankroll + realized P&L -
    # deployed, instead of a fixed starting amount. Off by default: in
    # validation the extra compounding-funded trades were net losers ($1,333
    # vs $1,471). Note: net realized LOSSES always shrink the free bankroll
    # regardless of this flag — a real account can't redeploy money it lost.
    compound_profits: bool = False
    min_market_liquidity_usd: float = 5000.0  # skip thin markets

    # Paper fill model.
    slippage_bps: float = 60.0             # assumed adverse slippage (0.60%)

    # Storage.
    db_path: str = "pmbot.db"

    # Strategy #4 horizon: only copy trades in markets resolving this far out.
    longterm_min_days_to_resolution: int = 7
    # Skip copying trades priced at the extremes (near-certain = little edge).
    # Swept 2026-07 over 8 vetted leaders' tapes: 0.15-0.85 beat 0.05-0.95 and
    # 0.10-0.90 at every lookback once paired with the $500 notional floor.
    copy_price_min: float = 0.15
    copy_price_max: float = 0.85
    # Strategy #5 (exact copy): skip mirroring a trade if the current quote has
    # already drifted this many probability points from the leader's own fill
    # price — the edge that made it worth copying has likely decayed. This also
    # bounds how far a live fill can deviate from what the backtests assume
    # (they fill at the leader's price ± slippage), so keep it tight.
    copy_max_price_drift: float = 0.03
    # Never copy a leader BUY older than this (minutes; 0 = off). Protects
    # against replaying stale history when a NEW leader is first followed (or
    # after downtime): their recent tape all looks "unseen", and the drift
    # guard alone lets through old trades whose price happens to be unchanged.
    # SELLs are never age-filtered — mirroring an exit reduces risk at any age.
    copy_max_trade_age_minutes: float = 60.0
    # Only copy leader BUYs at or above this notional: a leader's small probes
    # carry little conviction and our slice of them dies to slippage/dust caps.
    # SELLs are always mirrored (reducing risk should never be filtered).
    # Swept 2026-07: $500 floor roughly doubled ROI vs $0/$100 at every
    # lookback (12-17% vs 1-8%) — conviction filtering is the biggest lever.
    copy_min_leader_notional_usd: float = 500.0
    # A mirrored exit sizes off the leader's own exit ratio, which is rarely
    # exactly 1.0 — a leader who leaves 0.001% behind makes us leave a crumb
    # too, and the ledger's open test (ABS(shares) > 1e-9) then counts that
    # fully-closed trade as open forever. When on, an exit that would leave
    # less than `exit_dust_usd` of THIS leader's slice takes the whole slice.
    # Clamped to the leader's own slice, never the combined position, so it
    # can never liquidate another leader's copy of the same token.
    #
    # On by default: this is a hygiene fix, not a strategy knob. A backtest
    # cannot measure it (the harness settles every position, so dust never
    # survives) — 60d on the 8 live leaders was byte-identical at $5,032.48
    # net both ways while the branch fired 24 times for $0.016 total. Its real
    # risk is multi-leader safety, covered by tests/test_multi_leader_exits.py.
    # Set PMBOT_SWEEP_EXIT_DUST=false to fall back to the old behaviour.
    sweep_exit_dust: bool = True
    exit_dust_usd: float = 0.01
    # Don't copy a leader BUY when the same `get_trades` window already shows
    # them fully back out of it. Both halves of a completed round-trip land in
    # one poll cycle, so we'd open and close at the same current price —
    # capturing none of the leader's move and paying the spread twice. The age
    # filter can't catch these: the entry really is minutes old, just already
    # dead. Only FULL exits retire an entry; a leader who trimmed and still
    # holds is still expressing conviction. Set PMBOT_SKIP_ROUND_TRIPPED_ENTRIES
    # =false to fall back to the old behaviour.
    skip_round_tripped_entries: bool = True
    # After scoring, vet each would-be leader by backtesting an exact copy of
    # their recent tape; drop leaders whose copy P&L comes out below the floor.
    # (Scoring measures THEIR profit; vetting measures OURS, after our sizing,
    # caps and slippage — a leader can be profitable yet not copyable.)
    copy_vet_leaders: bool = True
    copy_vet_lookback_days: int = 30   # matches the 30d selection window
    copy_vet_min_pnl_usd: float = 0.0
    # EXPERIMENTAL: scale each leader's copy_fraction by their vet-backtest
    # ROI relative to the pack (clamped 0.5x-2x). Off by default: Data-API
    # tape depth couldn't reach the tune window for heavy traders, so this
    # never got a true out-of-sample validation — unproven, not disproven.
    copy_weight_by_vet: bool = False
    # Skip entries placed within this many hours of market close (0 = off).
    # Validated 2026-07 on the current leader set: 6h HALVES max drawdown
    # ($278->$114) and lifts ROI (24.3->27.0%) but drops ~37% of net P&L
    # ($491->$308) by skipping late trades. Off for max profit; set 6 for a
    # calmer equity curve.
    copy_min_hours_to_resolution: float = 0.0

    # --- Strategy #1: cross-platform arbitrage (Polymarket vs Kalshi) ---
    arb_enabled: bool = False
    # Minimum net edge per $1 contract pair AFTER Kalshi fees + slippage buffer.
    arb_min_edge: float = 0.015
    # Extra safety haircut per pair to absorb fill slippage on both legs.
    arb_slippage_buffer: float = 0.005
    arb_max_per_trade_usd: float = 50.0
    # Matching: candidates below this title similarity are never suggested.
    arb_match_min_similarity: float = 0.60
    # Candidate pair end dates must agree within this window. Venues stamp
    # dates differently (PM daily markets: start-of-day UTC; Kalshi: actual
    # event close), so same-day pairs can differ by ~23h. 30h keeps same-day
    # pairs while excluding adjacent days of daily-recurring series.
    arb_match_max_close_diff_hours: float = 30.0
    # Path to the confirmed-pairs file (only confirmed pairs are traded).
    arb_pairs_path: str = "pmbot/config/arb_pairs.yaml"
    # Kalshi series to scan for matching (comma-separated). The generic
    # market feeds are flooded with combo/parlay markets, so matching works
    # series-by-series over categories that overlap Polymarket.
    kalshi_series: str = (
        "KXWCGAME,KXWCADVANCE,KXMLBGAME,KXWTAMATCH,KXATPMATCH,"
        "KXBTCD,KXETHD,KXBTC,KXETH,KXNBA,KXNHL,KXUFCFIGHT,KXHIGHNY"
    )

    # --- LIVE ONLY (optional; leave unset for paper/backtest) ---
    pk: str | None = None
    clob_api_key: str | None = None
    clob_api_secret: str | None = None
    clob_api_passphrase: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (reads .env / env vars once)."""
    return Settings()
