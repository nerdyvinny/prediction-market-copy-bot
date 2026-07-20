"""Persistent cache of market resolutions.

A resolved market never un-resolves, so terminal answers (closed with a
decisive winner) are cached in SQLite forever and survive restarts. Open or
undecided markets are deliberately NOT stored — they must be re-checked live
every rescore, otherwise a market that resolves later would stay "open" in
the cache and understate every wallet's win rate (the Bug 3 staleness trap).

This is also the main rescore speed lever: leaders cluster in the same daily
markets, so after the first rescore most resolution lookups are cache hits.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resolutions (
    condition_id TEXT PRIMARY KEY,
    winner       TEXT NOT NULL,
    checked_at   TEXT NOT NULL
);
"""


class ResolutionStore:
    def __init__(self, db_path: str = "pmbot.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def load(self) -> dict[str, tuple[bool, str | None]]:
        """All cached terminal resolutions as {condition_id: (True, winner)}."""
        rows = self.conn.execute("SELECT condition_id, winner FROM resolutions").fetchall()
        return {r["condition_id"]: (True, r["winner"]) for r in rows}

    def save_many(self, winners: dict[str, str]) -> None:
        """Persist newly observed terminal resolutions (idempotent)."""
        if not winners:
            return
        now = datetime.now(timezone.utc).isoformat()
        self.conn.executemany(
            "INSERT OR IGNORE INTO resolutions (condition_id, winner, checked_at) VALUES (?,?,?)",
            [(cid, winner, now) for cid, winner in winners.items()],
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ResolutionStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
