"""The resolved-market ledger: persistent per-wallet win records.

The feed funnel (discovery.profile_candidates) only sees wallets active in the
top-volume market feeds *of the moment*, so consistent winners in niche
markets are invisible to it and discovery is per-run stochastic — two of the
best leaders ever found were only recoverable from old logs because their
markets never cracked the top-30 feeds.

This module fixes that by ACCUMULATING: every rescore, it fetches the trade
feeds of recently resolved markets it hasn't recorded yet and persists each
wallet's exact realized P&L there. Day after day the store builds a durable
win/loss history per wallet, and the selector shortlists from it alongside the
live feeds — so a wallet that keeps winning gets deep-scored even when today's
feeds no longer show it.

Records are shortlisting evidence, not eligibility: every wallet surfaced here
still goes through the same deep-scoring and filters as everyone else.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.leaders.discovery import (
    _f,
    _fetch_market_trades,
    _recent_resolved_markets,
    _winner_token,
)

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_records (
    wallet       TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    pnl          REAL NOT NULL,
    n_trades     INTEGER NOT NULL,
    resolved_at  TEXT,
    PRIMARY KEY (wallet, condition_id)
);
CREATE TABLE IF NOT EXISTS harvested_markets (
    condition_id TEXT PRIMARY KEY,
    harvested_at TEXT NOT NULL,
    resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_wallet_records_wallet ON wallet_records(wallet);
"""


class RecordStore:
    def __init__(self, db_path: str = "pmbot.db"):
        # check_same_thread=False: the store is only ever used by whichever
        # single thread runs the rescore (main synchronously, or the engine's
        # background rescore worker) — never concurrently.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def harvested_ids(self) -> set[str]:
        return {r["condition_id"] for r in
                self.conn.execute("SELECT condition_id FROM harvested_markets")}

    def add_market(
        self, condition_id: str, resolved_at: str | None,
        rows: dict[str, tuple[float, int]],
    ) -> None:
        """Record one resolved market's per-wallet P&L and mark it harvested."""
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.executemany(
                """INSERT OR REPLACE INTO wallet_records
                   (wallet, condition_id, pnl, n_trades, resolved_at)
                   VALUES (?,?,?,?,?)""",
                [(w, condition_id, pnl, n, resolved_at) for w, (pnl, n) in rows.items()],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO harvested_markets (condition_id, harvested_at, resolved_at)"
                " VALUES (?,?,?)",
                (condition_id, now, resolved_at),
            )

    def wallet_summaries(self, since_iso: str) -> dict[str, tuple[int, int, float]]:
        """{wallet: (wins, losses, total_pnl)} over markets resolved since
        `since_iso` (rows without a resolution date count as recent)."""
        rows = self.conn.execute(
            """SELECT wallet,
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,
                      SUM(pnl) AS pnl
               FROM wallet_records
               WHERE resolved_at IS NULL OR resolved_at >= ?
               GROUP BY wallet""",
            (since_iso,),
        ).fetchall()
        return {r["wallet"]: (int(r["wins"]), int(r["losses"]), float(r["pnl"]))
                for r in rows}

    def prune(self, keep_days: float = 90.0) -> None:
        """Drop records far older than any selection window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        with self.conn:
            self.conn.execute(
                "DELETE FROM wallet_records WHERE resolved_at IS NOT NULL AND resolved_at < ?",
                (cutoff,),
            )
            self.conn.execute(
                "DELETE FROM harvested_markets WHERE resolved_at IS NOT NULL AND resolved_at < ?",
                (cutoff,),
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "RecordStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def harvest_resolved_records(
    data: PolymarketDataClient,
    gamma: GammaClient,
    store: RecordStore,
    *,
    market_limit: int = 150,
    lookback_days: int = 30,
    per_market_trades: int = 1000,
) -> int:
    """Ingest up to `market_limit` recently resolved markets the store hasn't
    seen yet; returns how many were recorded. Feed-fetch failures are skipped
    WITHOUT marking the market harvested, so they retry next rescore."""
    store.prune()
    markets = _recent_resolved_markets(gamma, limit=market_limit, lookback_days=lookback_days)
    done = store.harvested_ids()
    n_new = 0
    for m in markets:
        if m.market_id in done:
            continue
        winner = _winner_token(m)
        if winner is None:                 # _recent_resolved_markets guarantees this, but be safe
            continue
        try:
            trades = _fetch_market_trades(data, m.market_id, per_market_trades)
        except Exception as e:
            log.debug("records: feed fetch failed for %s: %s", m.market_id[:12], e)
            continue
        tallies: dict[str, tuple[float, int]] = {}
        for t in trades:
            wallet = str(t.get("proxyWallet") or "").lower()
            token = str(t.get("asset") or "")
            if not wallet or not token:
                continue
            price = _f(t.get("price"))
            size = _f(t.get("size"))
            is_buy = str(t.get("side", "BUY")).upper() != "SELL"
            payout = 1.0 if token == winner else 0.0
            pnl = (payout - price) * size if is_buy else (price - payout) * size
            prev_pnl, prev_n = tallies.get(wallet, (0.0, 0))
            tallies[wallet] = (prev_pnl + pnl, prev_n + 1)
        store.add_market(
            m.market_id,
            m.end_date.isoformat() if m.end_date else None,
            tallies,
        )
        n_new += 1
    if n_new:
        log.info("records: harvested %d newly resolved market(s)", n_new)
    return n_new
