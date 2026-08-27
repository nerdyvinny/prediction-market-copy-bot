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
from urllib.parse import quote

import psutil
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pmbot.config.settings import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.data.price_cache import PriceCache
from pmbot.models import Market, Side
from pmbot.portfolio.ledger import Ledger, apply_fill

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
_data: PolymarketDataClient | None = None
# condition id -> (question, polymarket URL, resolution time ISO or None).
# All three come from one Gamma lookup.
_market_cache: dict[str, tuple[str, str | None, str | None]] = {}
# condition id -> last time we re-asked Gamma for an end date that had already
# passed. See _resolution_iso.
_end_recheck: dict[str, float] = {}
END_RECHECK_TTL = 600.0
_engine_check: tuple[float, bool] = (0.0, False)
# wallet -> (fetched_at, raw positions). Leader books move on trades, not ticks,
# so a short TTL keeps a click-happy user off the public API's rate limit.
_leader_pos_cache: dict[str, tuple[float, list[dict]]] = {}
LEADER_POS_TTL = 30.0
# wallet -> (fetched_at, raw trades), same bargain as the book above.
_leader_trade_cache: dict[str, tuple[float, list[dict]]] = {}
LEADER_TRADE_TTL = 30.0
LEADER_TRADE_LIMIT = 100


def _get_gamma() -> GammaClient:
    global _gamma
    if _gamma is None:
        _gamma = GammaClient()
    return _gamma


def _get_data() -> PolymarketDataClient:
    global _data
    if _data is None:
        _data = PolymarketDataClient()
    return _data


def _get_prices() -> PriceCache:
    global _prices
    if _prices is None:
        _prices = PriceCache(ttl_seconds=20.0)
    return _prices


POLYMARKET_BASE = "https://polymarket.com"


def _market_url(slug: object, event_slug: object) -> str | None:
    """Public polymarket.com address for a market, or None if unlinkable.

    An event groups sibling markets ("Game 2 winner", "1st half O/U 1.5"), so
    the event page alone lands on whichever one Polymarket picks — the
    two-segment form is what actually opens the market we copied. `/market/`
    is the fallback for the rare row Gamma returns without an event.

    Slugs are third-party strings that end up in an `href`, so they are
    percent-encoded: ordinary slugs pass through untouched and anything that
    could break out of the attribute does not.
    """
    s = quote(str(slug), safe="") if slug else ""
    e = quote(str(event_slug), safe="") if event_slug else ""
    if e and s:
        return f"{POLYMARKET_BASE}/event/{e}/{s}"
    if e:
        return f"{POLYMARKET_BASE}/event/{e}"
    if s:
        return f"{POLYMARKET_BASE}/market/{s}"
    return None


def _market_row(market_id: str) -> tuple[str, str | None, str | None]:
    """(question, URL, resolution ISO) for a condition id, cached for the process."""
    if market_id not in _market_cache:
        try:
            m = _get_gamma().get_market(market_id)
        except Exception:
            return ("", None, None)  # transient API failure: retry next poll, don't cache
        if m is None:
            # Not found *right now* is not an answer either — caching the blank
            # would leave the row unlabelled for the life of the process.
            return ("", None, None)
        _market_cache[market_id] = _row_of(m)
    return _market_cache[market_id]


def _row_of(m: Market) -> tuple[str, str | None, str | None]:
    return (
        m.question,
        _market_url(m.slug, m.event_slug),
        m.end_date.isoformat() if m.end_date else None,
    )


def _market_meta(market_id: str) -> tuple[str, str | None]:
    """(question, polymarket URL) for a condition id."""
    return _market_row(market_id)[:2]


def _resolution_iso(market_id: str) -> str | None:
    """When this market is scheduled to resolve, or None if Gamma has no date.

    The date is cached with the rest of the market like everything else here,
    with one exception: once it is in the *past* it gets re-asked on a slow
    timer. A postponed game moves its endDate, and a dashboard that runs for
    days would otherwise print the original kickoff forever. A market that has
    simply not settled yet re-reads the same past date, which is what the row
    should say: Polymarket can report a market closed for days before it
    publishes the outcome, and that wait is normal, not a stuck position.
    """
    end = _market_row(market_id)[2]
    when = _parse_iso(end) if end else None
    if when is None or when > datetime.now(timezone.utc):
        return end
    if time.time() - _end_recheck.get(market_id, 0.0) < END_RECHECK_TTL:
        return end
    _end_recheck[market_id] = time.time()
    try:
        m = _get_gamma().get_market(market_id)
    except Exception:
        return end  # keep the stale date rather than blanking the column
    if m is not None:
        # Replace wholesale: a failed refresh must never cost the row its
        # question or its link.
        _market_cache[market_id] = _row_of(m)
        return _market_cache[market_id][2]
    return end


def _parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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
            "bought_shares": 0.0,
            "sold_shares": 0.0,
            "first_ts": f["ts"],
            "last_ts": f["ts"],
        })
        if f["side"] == "BUY":
            t["invested_usd"] += f["size_usd"]
            t["bought_shares"] += abs(f["shares"])
        else:
            t["returned_usd"] += f["size_usd"]
            t["sold_shares"] += abs(f["shares"])
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
        question, url = _market_meta(t["market_id"])
        # Average prices, so a day cell can show what a trade went in at against
        # where it stands now. A closed trade's "now" is the price it left at —
        # a settlement pays 1.00 or 0.00, which is the honest last mark.
        entry = (t["invested_usd"] / t["bought_shares"]) if t["bought_shares"] else None
        exit_ = (t["returned_usd"] / t["sold_shares"]) if t["sold_shares"] else None
        out.append({
            **t,
            "question": question,
            "url": url,
            "status": "open" if is_open else "closed",
            "open_value_usd": round(open_value, 2) if open_value is not None else None,
            "net_usd": round(net, 2) if net is not None else None,
            "entry_price": round(entry, 4) if entry is not None else None,
            "exit_price": round(exit_, 4) if exit_ is not None else None,
            "cur_price": round(mid, 4) if mid is not None else None,
        })
    out.sort(key=lambda t: t["last_ts"], reverse=True)
    return out


def _annotate_realized(fills: list[dict]) -> None:
    """Stamp each fill (oldest first) with the realized P&L it booked.

    The ledger only stores the *running* realized total per token, so a daily
    breakdown has to replay the tape. This uses the same `apply_fill` the
    engine does, which means the per-fill deltas sum back to the ledger's
    realized total — the calendar and the hero tile can't drift apart.

    `net_realized_usd` also subtracts the fill's fee, so a day's column is what
    that day actually did to the bankroll: BUY days show their fee drag, SELL
    days show the closed P&L net of cost.
    """
    state: dict[str, tuple[float, float]] = {}  # token -> (shares, avg)
    for f in fills:
        shares, avg = state.get(f["token_id"], (0.0, 0.0))
        side = Side.BUY if f["side"] == "BUY" else Side.SELL
        eff = apply_fill(shares, avg, side, abs(f["shares"]), f["fill_price"])
        state[f["token_id"]] = (eff.new_shares, eff.new_avg)
        f["realized_usd"] = round(eff.realized_delta, 4)
        f["net_realized_usd"] = round(eff.realized_delta - (f["fee_usd"] or 0.0), 4)


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


def _leader_stats(trades: list[dict]) -> dict[str, dict]:
    """How the trades we copied from each leader have actually done.

    Keyed by lowercased wallet — `source_leader` carries whatever case the Data
    API handed us, and the follow list is written by a different path. Win rate
    is closed trades only, on the same 0.5c dead band as the hero tile, so a
    leader's rate can't wobble with every quote. `open_pnl_usd` is mark-to-
    market on what's still open, so the two columns add to the total.

    A trade the engine attributes to no leader (a manual or settled-only token)
    belongs to nobody and is left out rather than smeared across the lineup.
    """
    stats: dict[str, dict] = {}
    for t in trades:
        w = (t.get("leader") or "").lower()
        if not w:
            continue
        s = stats.setdefault(w, {
            "copied_trades": 0, "open_trades": 0, "closed_trades": 0,
            "wins": 0, "losses": 0, "flat": 0, "win_rate": None,
            "invested_usd": 0.0, "closed_pnl_usd": 0.0, "open_pnl_usd": 0.0,
            "net_usd": 0.0, "unpriced": 0,
        })
        s["copied_trades"] += 1
        s["invested_usd"] += t["invested_usd"]
        net = t["net_usd"]
        if t["status"] == "closed":
            s["closed_trades"] += 1
            s["closed_pnl_usd"] += net or 0.0
            if (net or 0.0) > 0.005:
                s["wins"] += 1
            elif (net or 0.0) < -0.005:
                s["losses"] += 1
            else:
                s["flat"] += 1
        else:
            s["open_trades"] += 1
            if net is None:
                s["unpriced"] += 1  # no quote: counted, but not in the P&L
            else:
                s["open_pnl_usd"] += net

    for s in stats.values():
        s["net_usd"] = round(s["closed_pnl_usd"] + s["open_pnl_usd"], 2)
        s["closed_pnl_usd"] = round(s["closed_pnl_usd"], 2)
        s["open_pnl_usd"] = round(s["open_pnl_usd"], 2)
        s["invested_usd"] = round(s["invested_usd"], 2)
        s["win_rate"] = (round(s["wins"] / s["closed_trades"], 4)
                         if s["closed_trades"] else None)
    return stats


def _leaders(followed: list[dict], exposure: dict, trades: list[dict]) -> list[dict]:
    """Who we're following, enriched for the dashboard.

    `followed` is the persisted follow list (wallet + score + when). We fold in
    live open exposure per leader plus how the copies off each one have done,
    and hand back a Polymarket profile URL so the row can be clicked through.
    """
    stats = _leader_stats(trades)
    out = []
    for row in followed:
        w = row["wallet"]
        s = stats.get(w.lower(), {})
        out.append({
            "wallet": w,
            "score": round(float(row["score"]), 3),
            "followed_at": row["followed_at"],
            "exposure_usd": round(exposure.get(w, 0.0), 2),
            "copied_trades": s.get("copied_trades", 0),
            "open_trades": s.get("open_trades", 0),
            "closed_trades": s.get("closed_trades", 0),
            "wins": s.get("wins", 0),
            "losses": s.get("losses", 0),
            "flat": s.get("flat", 0),
            "win_rate": s.get("win_rate"),
            "invested_usd": s.get("invested_usd", 0.0),
            "closed_pnl_usd": s.get("closed_pnl_usd", 0.0),
            "open_pnl_usd": s.get("open_pnl_usd", 0.0),
            "net_usd": s.get("net_usd", 0.0),
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
    _annotate_realized(all_fills)  # oldest-first order matters: this replays the tape
    open_pos = {p.token_id: p for p in positions}
    mids = {p.token_id: _mid_for(p.token_id, p.venue) for p in positions}
    # Leader stats are a roll-up of the same trade rows the tables show, so the
    # per-leader P&L can't disagree with the "Copied trades" card.
    trades_out = _group_trades(all_fills, open_pos, mids)
    leaders_out = _leaders(leaders_raw, leader_exposure, trades_out)

    # When we took each position: the first BUY on that token, not the last
    # fill — an add-on later doesn't make it a newer trade. all_fills is
    # oldest-first, so the first BUY seen wins.
    opened: dict[str, str] = {}
    for f in all_fills:
        if f["side"] == "BUY":
            opened.setdefault(f["token_id"], f["ts"])

    pos_out = []
    for p in positions:
        mid = mids.get(p.token_id)
        cost = p.shares * p.avg_price
        value = p.shares * mid if mid is not None else None
        question, url = _market_meta(p.market_id)
        pos_out.append({
            "market_id": p.market_id,
            "token_id": p.token_id,
            "question": question,
            "url": url,
            "outcome": p.outcome,
            "venue": p.venue,
            "shares": p.shares,
            "avg_price": p.avg_price,
            "mid": mid,
            "cost_usd": round(cost, 2),
            "value_usd": round(value, 2) if value is not None else None,
            "unrealized_usd": round(value - cost, 2) if value is not None else None,
            "anomaly": p.shares < 0,
            "opened_ts": opened.get(p.token_id),
            "resolves_at": _resolution_iso(p.market_id),
        })
    # Newest copy first. The page can re-sort by resolution time, but this is
    # the order it opens in, so it has to be right server-side too.
    pos_out.sort(key=lambda p: p["opened_ts"] or "", reverse=True)

    fills_out = []
    for f in reversed(all_fills):
        question, url = _market_meta(f["market_id"])
        fills_out.append({**f, "question": question, "url": url})

    unrealized = sum(
        p["unrealized_usd"] for p in pos_out if p["unrealized_usd"] is not None
    )
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


def _our_book_by_token(fills: list[dict], leader: str) -> dict[str, dict]:
    """Per-token roll-up of our own fills, tagged with whether this leader
    is the one we copied it from (the same market can reach us via two)."""
    book: dict[str, dict] = {}
    for f in fills:
        b = book.setdefault(f["token_id"], {
            "invested_usd": 0.0, "returned_usd": 0.0,
            "from_this_leader": False, "first_ts": f["ts"],
        })
        if f["side"] == "BUY":
            b["invested_usd"] += f["size_usd"]
        else:
            b["returned_usd"] += f["size_usd"]
        if (f["source_leader"] or "").lower() == leader:
            b["from_this_leader"] = True
    return book


def _our_side_of(key: str) -> tuple[dict[str, dict], dict[str, float]]:
    """(our book by token, open cost by token) for a followed leader.

    Restricted to wallets on the follow list on purpose: the dashboard binds
    0.0.0.0, and an unfiltered wallet parameter would turn it into an open
    proxy for the public Data API.
    """
    settings = get_settings()
    led = Ledger(settings.db_path)
    try:
        followed = {r["wallet"].lower() for r in led.followed_leaders_detail()}
        if key not in followed:
            raise HTTPException(status_code=404, detail="not a followed leader")
        rows = led.conn.execute(
            "SELECT ts, token_id, side, size_usd, source_leader FROM fills ORDER BY ts"
        ).fetchall()
        open_cost = {
            p.token_id: abs(p.shares * p.avg_price) for p in led.get_positions()
        }
    finally:
        led.close()
    return _our_book_by_token([dict(r) for r in rows], key), open_cost


def _our_status(ours: dict | None, open_cost: dict[str, float], token: str) -> str | None:
    """'open' / 'closed' for a token we copied, None for one we never touched."""
    if ours is None:
        return None
    return "open" if open_cost.get(token, 0.0) >= DUST_USD else "closed"


@app.get("/api/leader/{wallet}/trades")
def leader_trades(wallet: str) -> dict:
    """A followed leader's own recent fills, newest first.

    Their book (the other endpoint) says what they hold; this says what they
    did and when — which is the order the user reads a tape in. Sorted here
    rather than trusting the API's order, and ties broken by the transaction
    hash so a re-poll can't shuffle two fills that share a second.
    """
    key = wallet.lower()
    book, open_cost = _our_side_of(key)

    now = time.time()
    cached = _leader_trade_cache.get(key)
    if cached and now - cached[0] < LEADER_TRADE_TTL:
        raw, stale = cached[1], False
    else:
        try:
            raw = _get_data().get_raw_trades(user=wallet, limit=LEADER_TRADE_LIMIT)
            _leader_trade_cache[key] = (now, raw)
            stale = False
        except Exception as exc:
            if cached is None:
                raise HTTPException(status_code=502, detail=f"Data API: {exc}") from exc
            raw, stale = cached[1], True

    out = []
    for t in raw:
        token = str(t.get("asset", ""))
        ours = book.get(token)
        ts = int(_f(t.get("timestamp")))
        shares = _f(t.get("size"))
        price = _f(t.get("price"))
        out.append({
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "unix": ts,
            "token_id": token,
            "market_id": str(t.get("conditionId", "")),
            "title": t.get("title") or "",
            "url": _market_url(t.get("slug"), t.get("eventSlug")),
            "outcome": t.get("outcome") or "",
            "side": str(t.get("side", "")).upper(),
            "price": price,
            "shares": shares,
            "usd_size": round(shares * price, 2),
            "copied": ours is not None,
            "copied_from_this_leader": bool(ours and ours["from_this_leader"]),
            "our_status": _our_status(ours, open_cost, token),
        })
    out.sort(key=lambda t: (t["unix"], t["token_id"]), reverse=True)
    return {
        "wallet": wallet,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "stale": stale,
        "trades": out,
        "copied_count": sum(1 for t in out if t["copied"]),
        "buy_count": sum(1 for t in out if t["side"] == "BUY"),
    }


@app.get("/api/leader/{wallet}/positions")
def leader_positions(wallet: str) -> dict:
    """A followed leader's live Polymarket book, flagged with what we copied."""
    key = wallet.lower()
    book, open_cost = _our_side_of(key)

    now = time.time()
    cached = _leader_pos_cache.get(key)
    if cached and now - cached[0] < LEADER_POS_TTL:
        raw, stale = cached[1], False
    else:
        try:
            raw = _get_data().get_positions(wallet)
            _leader_pos_cache[key] = (now, raw)
            stale = False
        except Exception as exc:
            if cached is None:
                raise HTTPException(status_code=502, detail=f"Data API: {exc}") from exc
            raw, stale = cached[1], True  # serve the last good book rather than a blank panel

    out = []
    for p in raw:
        token = str(p.get("asset", ""))
        ours = book.get(token)
        out.append({
            "token_id": token,
            "market_id": str(p.get("conditionId", "")),
            "title": p.get("title") or "",
            # The Data API ships slugs inline, so their book links out for free.
            "url": _market_url(p.get("slug"), p.get("eventSlug")),
            "outcome": p.get("outcome") or "",
            "shares": _f(p.get("size")),
            "avg_price": _f(p.get("avgPrice")),
            "cur_price": _f(p.get("curPrice")),
            "value_usd": round(_f(p.get("currentValue")), 2),
            "pnl_usd": round(_f(p.get("cashPnl")), 2),
            "pct_pnl": round(_f(p.get("percentPnl")), 1),
            "end_date": p.get("endDate"),
            "redeemable": bool(p.get("redeemable")),
            "copied": ours is not None,
            "copied_from_this_leader": bool(ours and ours["from_this_leader"]),
            "our_status": _our_status(ours, open_cost, token),
            "our_invested_usd": round(ours["invested_usd"], 2) if ours else None,
            "our_first_ts": ours["first_ts"] if ours else None,
        })
    out.sort(key=lambda p: p["value_usd"], reverse=True)
    return {
        "wallet": wallet,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "stale": stale,
        "positions": out,
        "copied_count": sum(1 for p in out if p["copied"]),
        "total_value_usd": round(sum(p["value_usd"] for p in out), 2),
    }


def _f(v: object, default: float = 0.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


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
