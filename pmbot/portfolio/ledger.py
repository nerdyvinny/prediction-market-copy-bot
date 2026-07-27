"""SQLite ledger: fills, positions, and P&L.

Positions use signed average-cost accounting (positive shares = long the
outcome token, negative = short). `apply_fill` is a pure function so the
accounting math is unit-testable without a database.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pmbot.db import connect as db_connect
from pmbot.models import Fill, Position, Side

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    mode          TEXT    NOT NULL,
    market_id     TEXT    NOT NULL,
    token_id      TEXT    NOT NULL,
    outcome       TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    fill_price    REAL    NOT NULL,
    size_usd      REAL    NOT NULL,
    shares        REAL    NOT NULL,
    slippage_bps  REAL    NOT NULL DEFAULT 0,
    reason        TEXT,
    source_leader TEXT,
    source_uid    TEXT,
    venue         TEXT    NOT NULL DEFAULT 'polymarket',
    fee_usd       REAL    NOT NULL DEFAULT 0,
    leg_group     TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    token_id      TEXT PRIMARY KEY,
    market_id     TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    shares        REAL NOT NULL DEFAULT 0,
    avg_price     REAL NOT NULL DEFAULT 0,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    venue         TEXT NOT NULL DEFAULT 'polymarket'
);
CREATE TABLE IF NOT EXISTS followed_leaders (
    wallet        TEXT PRIMARY KEY,
    score         REAL NOT NULL DEFAULT 0,
    followed_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leader_positions (
    leader        TEXT NOT NULL,
    token_id      TEXT NOT NULL,
    shares        REAL NOT NULL,
    PRIMARY KEY (leader, token_id)
);
CREATE TABLE IF NOT EXISTS seen_trades (
    uid           TEXT PRIMARY KEY,
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_source_uid ON fills(source_uid);
CREATE INDEX IF NOT EXISTS idx_fills_leader ON fills(source_leader);
"""

# Columns added after the v1 schema; applied to pre-existing DBs on open.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("fills", "venue", "ALTER TABLE fills ADD COLUMN venue TEXT NOT NULL DEFAULT 'polymarket'"),
    ("fills", "fee_usd", "ALTER TABLE fills ADD COLUMN fee_usd REAL NOT NULL DEFAULT 0"),
    ("fills", "leg_group", "ALTER TABLE fills ADD COLUMN leg_group TEXT"),
    ("positions", "venue", "ALTER TABLE positions ADD COLUMN venue TEXT NOT NULL DEFAULT 'polymarket'"),
]


@dataclass
class FillEffect:
    new_shares: float
    new_avg: float
    realized_delta: float


def apply_fill(shares: float, avg: float, side: Side, fill_shares: float, price: float) -> FillEffect:
    """Apply a fill to a signed position; return the new state + realized P&L delta.

    Convention: BUY adds positive shares, SELL adds negative shares.
    Realized P&L accrues only when a fill reduces/closes the existing position.
    """
    signed = fill_shares if side is Side.BUY else -fill_shares
    if shares == 0 or (shares > 0) == (signed > 0):
        # Opening or increasing in the same direction -> weighted average cost.
        total = shares + signed
        denom = abs(shares) + abs(signed)
        new_avg = (avg * abs(shares) + price * abs(signed)) / denom if denom else 0.0
        return FillEffect(total, new_avg, 0.0)

    # Opposite direction: close some/all of the existing position.
    closing = min(abs(shares), abs(signed))
    realized = (price - avg) * closing if shares > 0 else (avg - price) * closing
    remaining = abs(shares) - closing
    leftover = abs(signed) - closing
    if remaining > 0:                      # original position partially closed
        new_shares = remaining if shares > 0 else -remaining
        return FillEffect(new_shares, avg, realized)
    if leftover > 0:                       # flipped past zero into the other side
        new_shares = leftover if signed > 0 else -leftover
        return FillEffect(new_shares, price, realized)
    return FillEffect(0.0, 0.0, realized)  # exactly flat


class Ledger:
    def __init__(self, db_path: str = "pmbot.db"):
        self.db_path = db_path
        # WAL + a real busy timeout: the dashboard opens its own connection to
        # this same file every 20s while the loop is writing (see pmbot.db).
        self.conn = db_connect(db_path)
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        for table, column, ddl in _MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(ddl)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes ----------------------------------------------------------
    def record_fill(self, fill: Fill) -> None:
        s = fill.signal
        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO fills (ts, mode, market_id, token_id, outcome, side,
                                  fill_price, size_usd, shares, slippage_bps,
                                  reason, source_leader, source_uid,
                                  venue, fee_usd, leg_group)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fill.timestamp.isoformat(), fill.mode, s.market_id, s.token_id,
                s.outcome, s.side.value, fill.fill_price, fill.size_usd, fill.shares,
                fill.slippage_bps, s.reason, s.source_leader, s.source_uid,
                s.venue, fill.fee_usd, s.leg_group,
            ),
        )
        row = cur.execute(
            "SELECT shares, avg_price, realized_pnl FROM positions WHERE token_id=?",
            (s.token_id,),
        ).fetchone()
        shares = row["shares"] if row else 0.0
        avg = row["avg_price"] if row else 0.0
        realized = row["realized_pnl"] if row else 0.0

        eff = apply_fill(shares, avg, s.side, fill.shares, fill.fill_price)
        realized += eff.realized_delta
        cur.execute(
            """INSERT INTO positions (token_id, market_id, outcome, shares, avg_price, realized_pnl, venue)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(token_id) DO UPDATE SET
                   shares=excluded.shares,
                   avg_price=excluded.avg_price,
                   realized_pnl=excluded.realized_pnl""",
            (s.token_id, s.market_id, s.outcome, eff.new_shares, eff.new_avg, realized, s.venue),
        )
        self.conn.commit()

    # -- reads -----------------------------------------------------------
    def get_position(self, token_id: str) -> Position | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE token_id=?", (token_id,)
        ).fetchone()
        return self._row_to_position(row) if row else None

    def get_positions(self, *, nonzero: bool = True) -> list[Position]:
        q = "SELECT * FROM positions"
        if nonzero:
            q += " WHERE ABS(shares) > 1e-9"
        return [self._row_to_position(r) for r in self.conn.execute(q).fetchall()]

    def has_copied(self, source_uid: str) -> bool:
        """True if we already recorded a fill for this leader-trade uid (dedupe)."""
        if not source_uid:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM fills WHERE source_uid=? LIMIT 1", (source_uid,)
        ).fetchone()
        return row is not None

    def exposure_for_leader(self, leader: str) -> float:
        """USD still deployed by following this leader, over OPEN positions only.

        Joining fills to nonzero positions is what frees a leader's budget
        when a market settles: settlement zeroes the position, its token
        drops out of the join, and the leader's cap headroom returns. (The
        old all-fills sum counted settled positions forever, so every leader
        eventually looked "full" and new copies were silently skipped.)
        Per-token nets are clamped at zero so an over-sold token can't grant
        negative exposure that hides real deployment elsewhere.
        """
        rows = self.conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN f.side='BUY' THEN f.size_usd ELSE -f.size_usd END), 0) AS net
               FROM fills f JOIN positions p ON p.token_id = f.token_id
               WHERE f.source_leader=? AND ABS(p.shares) > 1e-9
               GROUP BY f.token_id""",
            (leader,),
        ).fetchall()
        return sum(max(0.0, float(r["net"] or 0.0)) for r in rows)

    def exposure_for_market(self, market_id: str) -> float:
        """Current cost-basis exposure across all outcome tokens in a market."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(ABS(shares * avg_price)), 0) AS exp FROM positions WHERE market_id=?",
            (market_id,),
        ).fetchone()
        return float(row["exp"] or 0.0)

    def realized_pnl_total(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS p FROM positions"
        ).fetchone()
        return float(row["p"] or 0.0)

    def fill_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM fills").fetchone()["c"])

    def deployed_usd(self) -> float:
        """Total cost-basis currently deployed across open positions."""
        return sum(abs(p.shares * p.avg_price) for p in self.get_positions())

    def leader_exposures(self) -> dict[str, float]:
        """USD still deployed per leader, over open positions only (same
        open-position join and per-token clamp as `exposure_for_leader`)."""
        rows = self.conn.execute(
            """SELECT f.source_leader AS leader, f.token_id AS token,
                      COALESCE(SUM(CASE WHEN f.side='BUY' THEN f.size_usd ELSE -f.size_usd END), 0) AS net
               FROM fills f JOIN positions p ON p.token_id = f.token_id
               WHERE f.source_leader IS NOT NULL AND ABS(p.shares) > 1e-9
               GROUP BY f.source_leader, f.token_id"""
        ).fetchall()
        out: dict[str, float] = {}
        for r in rows:
            out[r["leader"]] = out.get(r["leader"], 0.0) + max(0.0, float(r["net"] or 0.0))
        return out

    def set_followed_leaders(self, wallets: dict[str, float]) -> None:
        """Replace the persisted follow list (wallet -> score).

        The engine writes this after every rescore so a restart remembers who
        it was following even before the first copied trade reaches `fills` —
        full addresses live nowhere else (logs truncate them)."""
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute("DELETE FROM followed_leaders")
            self.conn.executemany(
                "INSERT INTO followed_leaders (wallet, score, followed_at) VALUES (?,?,?)",
                [(w, s, now) for w, s in wallets.items()],
            )

    def followed_leaders(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT wallet FROM followed_leaders ORDER BY score DESC, wallet"
        ).fetchall()
        return [r["wallet"] for r in rows]

    def followed_leaders_detail(self) -> list[dict]:
        """Full follow list with score + when we started following, best first.

        Same order as `followed_leaders`; used by the dashboard to list who we
        copy (the wallet is the Polymarket profile address)."""
        rows = self.conn.execute(
            "SELECT wallet, score, followed_at FROM followed_leaders "
            "ORDER BY score DESC, wallet"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- leader-observation tracking (ExactCopyStrategy persistence) -------
    def record_leader_observation(
        self, leader: str, token_id: str, shares: float, uid: str, ts: datetime
    ) -> None:
        """Persist one observed leader trade: their running share count for the
        token plus the trade uid, in a single transaction. Restarts then keep
        proportional exits proportional (an unknown prior position degrades a
        leader's 10% trim into a full exit of ours) without ever re-applying a
        trade the strategy already counted."""
        with self.conn:
            self.conn.execute(
                """INSERT INTO leader_positions (leader, token_id, shares)
                   VALUES (?,?,?)
                   ON CONFLICT(leader, token_id) DO UPDATE SET shares=excluded.shares""",
                (leader, token_id, shares),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO seen_trades (uid, ts) VALUES (?,?)",
                (uid, ts.isoformat()),
            )

    def load_leader_positions(self) -> dict[tuple[str, str], float]:
        rows = self.conn.execute(
            "SELECT leader, token_id, shares FROM leader_positions"
        ).fetchall()
        return {(r["leader"], r["token_id"]): float(r["shares"]) for r in rows}

    def load_seen_uids(self) -> set[str]:
        return {r["uid"] for r in self.conn.execute("SELECT uid FROM seen_trades")}

    def prune_seen_trades(self, older_than_days: float = 30.0) -> None:
        """Drop seen-trade uids old enough to have left every fetch window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self.conn:
            self.conn.execute("DELETE FROM seen_trades WHERE ts < ?", (cutoff,))

    def fees_total(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(fee_usd), 0) AS f FROM fills"
        ).fetchone()
        return float(row["f"] or 0.0)

    def summary(self) -> dict:
        positions = self.get_positions()
        fees = self.fees_total()
        realized = self.realized_pnl_total()
        return {
            "fills": self.fill_count(),
            "open_positions": len(positions),
            "deployed_usd": sum(abs(p.shares * p.avg_price) for p in positions),
            "realized_pnl": realized,
            "fees_usd": fees,
            "net_pnl": realized - fees,
            "leaders": len(self.leader_exposures()),
        }

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Position:
        return Position(
            market_id=row["market_id"],
            token_id=row["token_id"],
            outcome=row["outcome"],
            shares=row["shares"],
            avg_price=row["avg_price"],
            realized_pnl=row["realized_pnl"],
            venue=row["venue"] if "venue" in row.keys() else "polymarket",
        )
