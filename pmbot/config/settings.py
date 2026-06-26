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

    # Poll loop.
    poll_interval_seconds: float = 15.0

    # Capital & sizing.
    bankroll_usd: float = 500.0
    copy_fraction: float = 0.05            # replicate this fraction of leader size
    max_per_market_usd: float = 50.0       # hard cap per market
    max_per_leader_usd: float = 150.0      # hard cap of exposure per leader
    min_market_liquidity_usd: float = 5000.0  # skip thin markets

    # Paper fill model.
    slippage_bps: float = 60.0             # assumed adverse slippage (0.60%)

    # Storage.
    db_path: str = "pmbot.db"

    # Strategy #4 horizon: only copy trades in markets resolving this far out.
    longterm_min_days_to_resolution: int = 7
    # Skip copying trades priced at the extremes (near-certain = little edge).
    copy_price_min: float = 0.05
    copy_price_max: float = 0.95

    # --- LIVE ONLY (optional; leave unset for paper/backtest) ---
    pk: str | None = None
    clob_api_key: str | None = None
    clob_api_secret: str | None = None
    clob_api_passphrase: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (reads .env / env vars once)."""
    return Settings()
