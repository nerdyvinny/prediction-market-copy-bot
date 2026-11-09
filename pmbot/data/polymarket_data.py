"""Client for Polymarket's public, read-only Data API (no auth required).

Speaks Data API **v2** (base https://data-api.polymarket.com, routes under
/v2). v1 was retired on 2026-11-02 and now answers 410 Gone on every route;
see https://docs.polymarket.com/migrate/data-api-v1-to-v2.

  GET /v2/trades     ?user=<wallet> | ?condition=<conditionId>  &limit &cursor
  GET /v2/positions  ?user=<wallet>
  GET /v2/value      ?user=<wallet>
  GET /v2/holders    ?condition=<conditionId>

v2 differs from v1 in three ways this module absorbs, so callers still see
the v1 shapes they were written against:

* every response is wrapped in ``{"data": ..., "pagination": ...}``;
* fields are snake_case (``proxy_wallet``), and a few are renamed outright
  (``token_id`` was ``asset``, ``current_size`` was ``size``) — rows are
  returned with the v1 camelCase keys added alongside the v2 ones;
* paging is cursor-only and an ``offset`` parameter is a 400. The
  ``offset=`` argument is kept and emulated (see ``_cursor_for_offset``).

`/v2/trades` is the copy-trading discovery layer: it returns each wallet's
individual fills, which is exactly what mirroring a leader needs.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from pmbot.config import get_settings
from pmbot.data.errors import raise_for_status_smart
from pmbot.models import LeaderTrade, Side

log = logging.getLogger(__name__)

_RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)

API_PREFIX = "/v2"

# v2 keys whose v1 name is not just the camelCase spelling of the v2 one.
_TRADE_RENAMES = {"token_id": "asset"}
_POSITION_RENAMES = {
    "token_id": "asset",
    "current_size": "size",
    "current_price": "curPrice",
    "current_value": "currentValue",
    "unrealized_pnl": "cashPnl",
    "entry_cost_usdc": "initialValue",
    "total_size": "totalBought",
    "opposite_token_id": "oppositeAsset",
}
_HOLDER_GROUP_RENAMES = {"token_id": "token"}

MAX_PAGE = 1000          # largest page /v2/trades serves
_MAX_PAGE_WALK = 200     # bounds the offset emulation so a bad offset can't page forever


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def to_v1_row(row: dict, renames: dict[str, str] | None = None) -> dict:
    """A v2 row with its v1 (camelCase / renamed) keys added.

    The v2 keys are kept too, so code that has moved to v2 names still works.
    A new dict is returned; the input is not modified.
    """
    renames = renames or {}
    out = dict(row)
    for key, value in row.items():
        out.setdefault(renames.get(key) or _camel(key), value)
    return out


class PolymarketDataClient:
    """Thin, typed wrapper over the public Data API (v2)."""

    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None,
                 *, bypass_cache: bool | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.data_api_base).rstrip("/")
        # See `copy_bypass_feed_cache`: /trades is served through a 300s CDN
        # cache, and that cache is measurably the whole of our copy lag.
        self.bypass_cache = (settings.copy_bypass_feed_cache
                             if bypass_cache is None else bypass_cache)
        self._client = client or httpx.Client(
            timeout=20.0, headers={"User-Agent": "pmbot/0.1 (+research)"}
        )
        self._owns_client = client is None
        # (user, market, offset) -> the cursor that starts at that offset.
        # Lets sequential offset paging cost one request per page.
        self._cursor_at: dict[tuple[str | None, str | None, int], str] = {}

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PolymarketDataClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low-level -------------------------------------------------------
    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=8),
        retry=retry_if_exception_type(_RETRYABLE),
    )
    def _get_envelope(self, path: str, params: dict[str, Any] | None = None) -> dict:
        resp = self._client.get(f"{self.base_url}{API_PREFIX}{path}", params=params)
        # 4xx (except 429) are not retried — they signal a bad request.
        raise_for_status_smart(resp)
        body = resp.json()
        return body if isinstance(body, dict) else {"data": body}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """The `data` payload of a v2 response."""
        return self._get_envelope(path, params).get("data")

    # -- trades ----------------------------------------------------------
    def _trades_page(self, base: dict[str, Any], cursor: str | None,
                     limit: int) -> tuple[list[dict], str | None]:
        """One /v2/trades page and the cursor for the next (None at the end)."""
        params = dict(base, limit=limit)
        if cursor:
            params["cursor"] = cursor      # an empty cursor is a 400
        if self.bypass_cache:
            # A parameter the API ignores but the CDN keys on, so this request
            # can only be answered by the origin. Applied to /trades alone —
            # it is the only endpoint whose staleness costs us anything, and
            # the others are polled rarely enough for the cache to be a
            # kindness rather than a tax.
            params["_"] = uuid.uuid4().hex
        body = self._get_envelope("/trades", params)
        rows = body.get("data")
        pagination = body.get("pagination") or {}
        next_cursor = pagination.get("next_cursor") if pagination.get("has_more") else None
        return (rows if isinstance(rows, list) else []), next_cursor

    def _cursor_for_offset(self, user: str | None, market: str | None,
                           base: dict[str, Any], offset: int) -> tuple[str | None, bool]:
        """(cursor that starts at `offset`, whether the feed reaches that far).

        v2 has no offset. Callers page with offset=0, n, 2n, ...; each page
        records the cursor for the offset that follows it, so the common case
        is a dictionary hit. Anything else walks forward from the start.
        """
        if offset <= 0:
            return None, True
        hit = self._cursor_at.get((user, market, offset))
        if hit:
            return hit, True
        cursor: str | None = None
        seen = 0
        for _ in range(_MAX_PAGE_WALK):
            rows, cursor = self._trades_page(base, cursor, min(MAX_PAGE, offset - seen))
            seen += len(rows)
            if not cursor:
                return None, False          # feed ended at or before `offset`
            if seen >= offset:
                return cursor, True
        log.warning("offset %d not reached after %d pages", offset, _MAX_PAGE_WALK)
        return None, False

    def get_raw_trades(
        self,
        *,
        user: str | None = None,
        market: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Raw trade dicts (v1 key names). Provide `user` (a wallet) and/or
        `market` (condition id)."""
        base: dict[str, Any] = {}
        if user:
            base["user"] = user
        if market:
            base["condition"] = market
        cursor, reachable = self._cursor_for_offset(user, market, base, offset)
        if not reachable:
            return []
        rows, next_cursor = self._trades_page(base, cursor, limit)
        if next_cursor:
            self._cursor_at[(user, market, offset + len(rows))] = next_cursor
        return [to_v1_row(r, _TRADE_RENAMES) for r in rows]

    def get_trades(
        self,
        *,
        user: str | None = None,
        market: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LeaderTrade]:
        """Parsed `LeaderTrade`s, newest first (as returned by the API)."""
        return [self._parse_trade(t) for t in self.get_raw_trades(
            user=user, market=market, limit=limit, offset=offset
        )]

    @staticmethod
    def _parse_trade(t: dict) -> LeaderTrade:
        t = to_v1_row(t, _TRADE_RENAMES)   # accepts a v1 or a v2 row
        size = _to_float(t.get("size"))
        price = _to_float(t.get("price"))
        ts = int(t.get("timestamp") or 0)
        side_raw = str(t.get("side", "BUY")).upper()
        return LeaderTrade(
            leader=str(t.get("proxyWallet", "")),
            market_id=str(t.get("conditionId", "")),
            token_id=str(t.get("asset", "")),
            outcome=str(t.get("outcome", "")),
            side=Side(side_raw) if side_raw in Side.__members__ else Side.BUY,
            price=price,
            shares=size,
            usd_size=size * price,
            timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
            tx_hash=t.get("transactionHash"),
            event_slug=t.get("eventSlug") or None,   # v2 sends "" for none
        )

    # -- positions / value / holders ------------------------------------
    def get_positions(self, user: str, limit: int = 500) -> list[dict]:
        """Open positions (v1 key names: asset, size, curPrice, cashPnl, ...)."""
        data = self._get("/positions", {"user": user, "limit": limit})
        rows = data if isinstance(data, list) else []
        return [to_v1_row(r, _POSITION_RENAMES) for r in rows]

    def get_value(self, user: str) -> float:
        """Total current portfolio value (USDC) for a wallet."""
        data = self._get("/value", {"user": user})
        if isinstance(data, list) and data:
            return _to_float(data[0].get("value"))
        if isinstance(data, dict):
            return _to_float(data.get("value"))
        return 0.0

    def get_holders(self, market: str, limit: int = 100) -> list[dict]:
        """Top holders per outcome token for a market (condition id).

        Returns the v1 [{token, holders:[{proxyWallet, ...}]}] structure.
        """
        data = self._get("/holders", {"condition": market, "limit": limit})
        out = []
        for g in data if isinstance(data, list) else []:
            group = to_v1_row(g, _HOLDER_GROUP_RENAMES)
            group["holders"] = [to_v1_row(h, _TRADE_RENAMES) for h in g.get("holders") or []]
            out.append(group)
        return out

    def harvest_wallets_from_market(self, market: str, limit: int = 100) -> set[str]:
        """Collect candidate leader wallets active in a market (holders + recent trades)."""
        wallets: set[str] = set()
        for group in self.get_holders(market, limit=limit):
            for h in group.get("holders", []):
                w = h.get("proxyWallet")
                if w:
                    wallets.add(w)
        for t in self.get_raw_trades(market=market, limit=limit):
            w = t.get("proxyWallet")
            if w:
                wallets.add(w)
        return wallets


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
