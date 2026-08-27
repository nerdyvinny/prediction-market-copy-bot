"""Shared SQLite connection setup for every store in the bot.

Three stores open the same file (`Ledger`, `ResolutionStore`, `RecordStore`),
and the paper loop is not the only process holding it: the dashboard opens a
fresh `Ledger` per `/api/state` request every 20s, and the docker-compose
setup runs the loop and the dashboard as two containers over one volume.
SQLite's defaults make that combination a live hazard:

  - the rollback journal lets exactly one connection touch the file at a time,
    so a dashboard read can block a fill mid-cycle;
  - the 5-second busy timeout then raises "database is locked", which
    propagates out of `ExactCopyStrategy.generate()` and aborts the whole poll
    cycle (the engine backs off, but that cycle's signals are gone).

WAL lets readers and the single writer proceed concurrently, and a longer busy
timeout makes the rare writer-vs-writer overlap wait its turn instead of
raising. `synchronous=NORMAL` is the standard companion to WAL: still durable
across a process crash, only at risk from an OS-level crash — the right trade
for a paper ledger whose inputs are all replayable public data.

In-memory databases (`:memory:`, used by the backtesters and most tests)
always run journal_mode=MEMORY; the WAL pragma is a documented no-op there,
not an error, so the same helper serves both.
"""

from __future__ import annotations

import sqlite3

# Long enough to outlast any write this bot makes (all are small and fast),
# short enough that a genuinely wedged lock still surfaces instead of hanging.
BUSY_TIMEOUT_MS = 30_000


def connect(db_path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a tuned connection: WAL, a real busy timeout, `Row` access.

    `check_same_thread=False` is for the stores driven by whichever single
    thread runs the rescore (never concurrently); the ledger keeps the default
    guard, since it is main-thread-only by design.
    """
    conn = sqlite3.connect(
        db_path,
        timeout=BUSY_TIMEOUT_MS / 1000.0,
        check_same_thread=check_same_thread,
    )
    conn.row_factory = sqlite3.Row
    # journal_mode is persistent (stored in the file); the rest are per
    # connection, so every opener has to set them.
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
