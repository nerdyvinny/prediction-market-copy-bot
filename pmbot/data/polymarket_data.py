"""Client for Polymarket's public, read-only Data API (no auth required).

Confirmed endpoints (base https://data-api.polymarket.com):
  GET /trades     ?user=<wallet> | ?market=<conditionId>  &limit &offset
  GET /positions  ?user=<wallet>
  GET /value      ?user=<wallet>
  GET /holders    ?market=<conditionId>

`/trades` is the copy-trading discovery layer: it returns each wallet's
individual fills, which is exactly what mirroring a leader needs.
"""

from __future__ import annotations

import logging
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


class PolymarketDataClient:
    """Thin, typed wrapper over the public Data API."""

    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.data_api_base).rstrip("/")
        self._client = client or httpx.Client(
            timeout=20.0, headers={"User-Agent": "pmbot/0.1 (+research)"}
        )
        self._owns_client = client is None

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
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._client.get(f"{self.base_url}{path}", params=params)
        # 4xx (except 429) are not retried — they signal a bad request.
        raise_for_status_smart(resp)
        return resp.json()

    # -- trades ----------------------------------------------------------
    def get_raw_trades(
        self,
        *,
        user: str | None = None,
        market: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Raw trade dicts. Provide `user` (a wallet) and/or `market` (condition id)."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if user:
            params["user"] = user
        if market:
            params["market"] = market
        data = self._get("/trades", params)
        return data if isinstance(data, list) else []

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
            event_slug=t.get("eventSlug"),
        )

    # -- positions / value / holders ------------------------------------
    def get_positions(self, user: str, limit: int = 500) -> list[dict]:
        data = self._get("/positions", {"user": user, "limit": limit})
        return data if isinstance(data, list) else []

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

        Returns the raw [{token, holders:[...]}] structure.
        """
        data = self._get("/holders", {"market": market, "limit": limit})
        return data if isinstance(data, list) else []

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
