"""Local web dashboard for pmbot.

Read-only view over the paper ledger (SQLite) plus live Polymarket quotes.
Serves a single page and a JSON state endpoint the page polls.

Run from the repo root:

    .venv/Scripts/python.exe -m pmbot.dashboard.app

Binds 0.0.0.0 so the page is reachable over LAN/Tailscale; the dashboard
never places orders and only reads public APIs + the local DB.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pmbot.config.settings import get_settings
from pmbot.data import GammaClient
from pmbot.data.price_cache import PriceCache
from pmbot.portfolio.ledger import Ledger

app = FastAPI(title="pmbot dashboard")


@app.middleware("http")
async def no_cache(request, call_next):
    # Local single-user dashboard: force revalidation so edits to the static
    # files show up on reload instead of being pinned by heuristic caching.
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache"
    return resp

_STATIC = Path(__file__).parent / "static"

# Lazy singletons: market questions never change, quotes have their own TTL.
_gamma: GammaClient | None = None
_prices: PriceCache | None = None
_question_cache: dict[str, str] = {}
_engine_check: tuple[float, bool] = (0.0, False)


def _get_gamma() -> GammaClient:
    global _gamma
    if _gamma is None:
        _gamma = GammaClient()
    return _gamma


def _get_prices() -> PriceCache:
    global _prices
    if _prices is None:
        _prices = PriceCache(ttl_seconds=20.0)
    return _prices


def _question_for(market_id: str) -> str:
    if market_id not in _question_cache:
        try:
            m = _get_gamma().get_market(market_id)
        except Exception:
            return ""  # transient API failure: retry next poll, don't cache
        if m is None:
            # Not found *right now* is not an answer either — caching the blank
            # would leave the row unlabelled for the life of the process.
            return ""
        _question_cache[market_id] = m.question
    return _question_cache[market_id]


def _engine_running() -> bool:
    """True if a `pmbot ... paper` loop process is alive. Cached 15s."""
    global _engine_check
    ts, val = _engine_check
    if time.time() - ts < 15:
        return val
    running = False
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "pmbot" in cmd and "paper" in cmd and "dashboard" not in cmd:
            running = True
            break
    _engine_check = (time.time(), running)
    return running


def _mid_for(token_id: str, venue: str) -> float | None:
    if venue != "polymarket":
        return None  # no public quote source wired for other venues
    try:
        return _get_prices().get_quote(token_id).mid
    except Exception:
        return None


# A "sell 100%" exit sizes the order from a re-derived share count, which can
# undershoot the held amount by a rounding crumb (seen live: 0.000676 shares
# left of 61.36, worth $0.0005). The ledger's open test is ABS(shares) > 1e-9,
# so that crumb keeps a fully-exited trade looking open forever. Anything worth
# less than a cent is dust, not a position.
DUST_USD = 0.01


def _group_trades(fills: list[dict], open_pos: dict, mids: dict) -> list[dict]:
    """Roll fills up per outcome token: money in, money back, what's left.

    One row = one copied trade (a position in one outcome). `returned_usd`
    includes settlement payouts because settlement writes SELL fills too.
    """
    trades: dict[str, dict] = {}
    for f in fills:
        t = trades.setdefault(f["token_id"], {
            "token_id": f["token_id"],
            "market_id": f["market_id"],
            "outcome": f["outcome"],
            "venue": f["venue"],
            "leader": None,
            "invested_usd": 0.0,
            "returned_usd": 0.0,
            "fees_usd": 0.0,
            "first_ts": f["ts"],
            "last_ts": f["ts"],
        })
        if f["side"] == "BUY":
            t["invested_usd"] += f["size_usd"]
        else:
            t["returned_usd"] += f["size_usd"]
        t["fees_usd"] += f["fee_usd"] or 0.0
        if t["leader"] is None and f["source_leader"]:
            t["leader"] = f["source_leader"]
        t["last_ts"] = f["ts"]

    out = []
    for t in trades.values():
        pos = open_pos.get(t["token_id"])
        is_open = pos is not None and abs(pos.shares * pos.avg_price) >= DUST_USD
        mid = mids.get(t["token_id"]) if is_open else None
        open_value = pos.shares * mid if (is_open and mid is not None) else None
        if is_open:
            # Net needs a live quote for the open remainder; without one it's unknown.
            net = (t["returned_usd"] + open_value - t["invested_usd"] - t["fees_usd"]
                   ) if open_value is not None else None
        else:
            net = t["returned_usd"] - t["invested_usd"] - t["fees_usd"]
        out.append({
            **t,
            "question": _question_for(t["market_id"]),
            "status": "open" if is_open else "closed",
            "open_value_usd": round(open_value, 2) if open_value is not None else None,
            "net_usd": round(net, 2) if net is not None else None,
        })
    out.sort(key=lambda t: t["last_ts"], reverse=True)
    return out


def _win_stats(trades: list[dict]) -> dict:
    """Win rate over *closed* trades only.

    Open trades are excluded: their net moves with the quote, so counting them
    would make the rate wobble every poll. The same 0.5c dead band the UI uses
    for colouring P&L splits wins from losses, so a trade that came back at
    cost is 'flat' rather than a win.
    """
    closed = [t for t in trades if t["status"] == "closed"]
    wins = sum(1 for t in closed if (t["net_usd"] or 0.0) > 0.005)
    losses = sum(1 for t in closed if (t["net_usd"] or 0.0) < -0.005)
    return {
        "closed_trades": len(closed),
        "wins": wins,
        "losses": losses,
        "flat": len(closed) - wins - losses,
        "win_rate": round(wins / len(closed), 4) if closed else None,
    }


POLYMARKET_PROFILE = "https://polymarket.com/profile/{}"


def _leaders(followed: list[dict], exposure: dict, fills: list[dict]) -> list[dict]:
    """Who we're following, enriched for the dashboard.

    `followed` is the persisted follow list (wallet + score + when). We fold in
    live open exposure per leader and how many trades we've copied from each,
    and hand back a Polymarket profile URL so the row can be clicked through.
    """
    copied: dict[str, int] = {}
    for f in fills:
        w = f["source_leader"]
        if w and f["side"] == "BUY":
            copied[w] = copied.get(w, 0) + 1
    out = []
    for row in followed:
        w = row["wallet"]
        out.append({
            "wallet": w,
            "score": round(float(row["score"]), 3),
            "followed_at": row["followed_at"],
            "exposure_usd": round(exposure.get(w, 0.0), 2),
            "copied_trades": copied.get(w, 0),
            "profile_url": POLYMARKET_PROFILE.format(w),
        })
    return out


@app.get("/api/state")
def state() -> dict:
    settings = get_settings()
    led = Ledger(settings.db_path)
    try:
        summary = led.summary()
        positions = led.get_positions()
        rows = led.conn.execute(
            "SELECT ts, market_id, token_id, outcome, side, fill_price,"
            "       size_usd, shares, reason, source_leader, venue, fee_usd "
            "FROM fills ORDER BY ts"
        ).fetchall()
        leaders_raw = led.followed_leaders_detail()
        leader_exposure = led.leader_exposures()
    finally:
        led.close()

    all_fills = [dict(r) for r in rows]
    open_pos = {p.token_id: p for p in positions}
    mids = {p.token_id: _mid_for(p.token_id, p.venue) for p in positions}
    leaders_out = _leaders(leaders_raw, leader_exposure, all_fills)

    pos_out = []
    for p in positions:
        mid = mids.get(p.token_id)
        cost = p.shares * p.avg_price
        value = p.shares * mid if mid is not None else None
        pos_out.append({
            "market_id": p.market_id,
            "question": _question_for(p.market_id),
            "outcome": p.outcome,
            "venue": p.venue,
            "shares": p.shares,
            "avg_price": p.avg_price,
            "mid": mid,
            "cost_usd": round(cost, 2),
            "value_usd": round(value, 2) if value is not None else None,
            "unrealized_usd": round(value - cost, 2) if value is not None else None,
            "anomaly": p.shares < 0,
        })
    pos_out.sort(key=lambda p: abs(p["cost_usd"]), reverse=True)

    fills_out = [
        {**f, "question": _question_for(f["market_id"])} for f in reversed(all_fills)
    ]

    unrealized = sum(
        p["unrealized_usd"] for p in pos_out if p["unrealized_usd"] is not None
    )
    trades_out = _group_trades(all_fills, open_pos, mids)
    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "engine_running": _engine_running(),
        "mode": settings.mode.value,
        "summary": {
            "open_pnl": round(unrealized, 2),
            "closed_pnl": round(summary["net_pnl"], 2),
            "total_pnl": round(unrealized + summary["net_pnl"], 2),
            "deployed_usd": round(summary["deployed_usd"], 2),
            "bankroll_usd": settings.bankroll_usd,
            "open_positions": summary["open_positions"],
            "fills": summary["fills"],
            "fees_usd": round(summary["fees_usd"], 2),
            **_win_stats(trades_out),
        },
        "positions": pos_out,
        "trades": trades_out,
        "fills": fills_out,
        "leaders": leaders_out,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


app.mount("/static", StaticFiles(directory=_STATIC), name="static")


if __name__ == "__main__":
    import os

    import uvicorn

    # 8090 by default; PORT lets a second copy run alongside an SSH tunnel
    # that already holds the usual port.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
