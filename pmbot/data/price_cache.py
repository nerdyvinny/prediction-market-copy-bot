"""Top-of-book quotes from the Polymarket CLOB, with a short TTL cache.

Base: https://clob.polymarket.com
  GET /book   ?token_id=<id>   -> {bids:[{price,size}], asks:[...], ...}
  GET /price  ?token_id=<id>&side=buy|sell -> {price: "..."}

The cache keeps us from hammering the CLOB when several strategies ask for the
same token within a poll cycle.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from pmbot.config import get_settings
from pmbot.models import Quote

log = logging.getLogger(__name__)

_RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


class PriceCache:
    def __init__(
        self,
        base_url: str | None = None,
        ttl_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ):
        settings = get_settings()
        self.base_url = (base_url or settings.clob_api_base).rstrip("/")
        self.ttl = ttl_seconds
        self._client = client or httpx.Client(
            timeout=20.0, headers={"User-Agent": "pmbot/0.1 (+research)"}
        )
        self._owns_client = client is None
        self._cache: dict[str, tuple[float, Quote]] = {}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PriceCache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=8),
        retry=retry_if_exception_type(_RETRYABLE),
    )
    def _get(self, path: str, params: dict[str, Any]) -> Any:
        resp = self._client.get(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_quote(self, token_id: str, *, force: bool = False) -> Quote:
        """Top-of-book Quote for an outcome token (cached for `ttl` seconds)."""
        now = time.monotonic()
        if not force:
            hit = self._cache.get(token_id)
            if hit and (now - hit[0]) < self.ttl:
                return hit[1]
        quote = self._fetch_quote(token_id)
        self._cache[token_id] = (now, quote)
        return quote

    def _fetch_quote(self, token_id: str) -> Quote:
        book = self._get("/book", {"token_id": token_id})
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = max((_to_float(b.get("price")) for b in bids), default=None)
        best_ask = min((_to_float(a.get("price")) for a in asks), default=None)
        return Quote(token_id=token_id, bid=best_bid, ask=best_ask)

    def get_price(self, token_id: str, side: str = "buy") -> float | None:
        """Single-sided price (CLOB midpoint-ish). `side` is 'buy' or 'sell'."""
        data = self._get("/price", {"token_id": token_id, "side": side})
        return _to_float(data.get("price")) if isinstance(data, dict) else None


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
