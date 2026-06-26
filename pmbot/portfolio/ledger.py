"""SQLite ledger: fills, positions, and P&L.

Positions use signed average-cost accounting (positive shares = long the
outcome token, negative = short). `apply_fill` is a pure function so the
accounting math is unit-testable without a database.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

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
    source_uid    TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    token_id      TEXT PRIMARY KEY,
    market_id     TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    shares        REAL NOT NULL DEFAULT 0,
    avg_price     REAL NOT NULL DEFAULT 0,
    realized_pnl  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fills_source_uid ON fills(source_uid);
CREATE INDEX IF NOT EXISTS idx_fills_leader ON fills(source_leader);
"""


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
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

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
                                  reason, source_leader, source_uid)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fill.timestamp.isoformat(), fill.mode, s.market_id, s.token_id,
                s.outcome, s.side.value, fill.fill_price, fill.size_usd, fill.shares,
                fill.slippage_bps, s.reason, s.source_leader, s.source_uid,
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
            """INSERT INTO positions (token_id, market_id, outcome, shares, avg_price, realized_pnl)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(token_id) DO UPDATE SET
                   shares=excluded.shares,
                   avg_price=excluded.avg_price,
                   realized_pnl=excluded.realized_pnl""",
            (s.token_id, s.market_id, s.outcome, eff.new_shares, eff.new_avg, realized),
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
        """Net USD deployed by following this leader (BUY size - SELL size)."""
        row = self.conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN size_usd ELSE -size_usd END), 0) AS net
               FROM fills WHERE source_leader=?""",
            (leader,),
        ).fetchone()
        return float(row["net"] or 0.0)

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

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Position:
        return Position(
            market_id=row["market_id"],
            token_id=row["token_id"],
            outcome=row["outcome"],
            shares=row["shares"],
            avg_price=row["avg_price"],
            realized_pnl=row["realized_pnl"],
        )
