"""Local web dashboard for pmbot.

Read-only view over the paper ledger (SQLite) plus live Polymarket quotes.
Serves a single dark-mode page and a JSON state endpoint the page polls.

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
import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pmbot.config.settings import get_settings
from pmbot.data import GammaClient
from pmbot.data.price_cache import PriceCache
from pmbot.models import Side
from pmbot.portfolio.ledger import Ledger, apply_fill

app = FastAPI(title="pmbot dashboard")

_STATIC = Path(__file__).parent / "static"
_LEADERS_YAML = Path(__file__).parent.parent / "config" / "leaders.yaml"

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
            _question_cache[market_id] = m.question if m else ""
        except Exception:
            return ""  # transient API failure: retry next poll, don't cache
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


def _pnl_timeline(fills: list[dict]) -> list[dict]:
    """Replay fills in order and emit cumulative net realized P&L points.

    Uses the same apply_fill math as the ledger so settlement SELLs and
    partial exits realize exactly what the ledger realized.
    """
    book: dict[str, tuple[float, float]] = {}  # token_id -> (shares, avg)
    cum = 0.0
    points = []
    for f in fills:
        shares, avg = book.get(f["token_id"], (0.0, 0.0))
        side = Side.BUY if f["side"] == "BUY" else Side.SELL
        fill_shares = f["shares"] if f["shares"] else (
            f["size_usd"] / f["fill_price"] if f["fill_price"] else 0.0
        )
        eff = apply_fill(shares, avg, side, fill_shares, f["fill_price"])
        book[f["token_id"]] = (eff.new_shares, eff.new_avg)
        cum += eff.realized_delta - (f["fee_usd"] or 0.0)
        points.append({"ts": f["ts"], "net_pnl": round(cum, 2)})
    return points


@app.get("/api/state")
def state(fills_limit: int = 100) -> dict:
    settings = get_settings()
    led = Ledger(settings.db_path)
    try:
        summary = led.summary()
        positions = led.get_positions()
        leader_exposure = led.leader_exposures()
        rows = led.conn.execute(
            "SELECT ts, mode, market_id, token_id, outcome, side, fill_price,"
            "       size_usd, shares, reason, source_leader, venue, fee_usd "
            "FROM fills ORDER BY ts"
        ).fetchall()
    finally:
        led.close()

    all_fills = [dict(r) for r in rows]
    timeline = _pnl_timeline(all_fills)

    # Enrich open positions with market names + live quotes.
    pos_out = []
    for p in positions:
        mid = None
        if p.venue == "polymarket":
            try:
                mid = _get_prices().get_quote(p.token_id).mid
            except Exception:
                mid = None
        cost = p.shares * p.avg_price
        value = p.shares * mid if mid is not None else None
        pos_out.append({
            "market_id": p.market_id,
            "question": _question_for(p.market_id),
            "outcome": p.outcome,
            "venue": p.venue,
            "shares": p.shares,
            "avg_price": p.avg_price,
            "cost_usd": cost,
            "mid": mid,
            "value_usd": value,
            "unrealized_usd": (value - cost) if value is not None else None,
            "anomaly": p.shares < 0,
        })

    fills_out = []
    for f in reversed(all_fills[-fills_limit:]):
        fills_out.append({**f, "question": _question_for(f["market_id"])})

    try:
        leaders_cfg = yaml.safe_load(_LEADERS_YAML.read_text()) or {}
    except Exception:
        leaders_cfg = {}

    unrealized = sum(
        p["unrealized_usd"] for p in pos_out if p["unrealized_usd"] is not None
    )
    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "engine_running": _engine_running(),
        "mode": settings.mode.value,
        "summary": {
            **summary,
            "unrealized_pnl": round(unrealized, 2),
            "bankroll_usd": settings.bankroll_usd,
        },
        "settings": {
            "poll_interval_seconds": settings.poll_interval_seconds,
            "copy_fraction": settings.copy_fraction,
            "max_per_market_usd": settings.max_per_market_usd,
            "max_per_leader_usd": settings.max_per_leader_usd,
            "copy_price_min": settings.copy_price_min,
            "copy_price_max": settings.copy_price_max,
            "copy_min_leader_notional_usd": settings.copy_min_leader_notional_usd,
            "copy_max_price_drift": settings.copy_max_price_drift,
            "min_market_liquidity_usd": settings.min_market_liquidity_usd,
            "slippage_bps": settings.slippage_bps,
            "arb_enabled": settings.arb_enabled,
            "db_path": settings.db_path,
        },
        "leaders": {
            "exposure": leader_exposure,
            "top_n": (leaders_cfg.get("selection") or {}).get("top_n"),
            "lookback_days": (leaders_cfg.get("filters") or {}).get("lookback_days"),
            "min_win_rate": (leaders_cfg.get("filters") or {}).get("min_win_rate"),
        },
        "positions": pos_out,
        "pnl_timeline": timeline,
        "fills": fills_out,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


app.mount("/static", StaticFiles(directory=_STATIC), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8090)
