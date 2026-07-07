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
        best_bid = _best_level(bids, want_max=True)
        best_ask = _best_level(asks, want_max=False)
        return Quote(
            token_id=token_id,
            bid=best_bid[0] if best_bid else None,
            ask=best_ask[0] if best_ask else None,
            bid_size=best_bid[1] if best_bid else None,
            ask_size=best_ask[1] if best_ask else None,
        )

    def get_price(self, token_id: str, side: str = "buy") -> float | None:
        """Single-sided price (CLOB midpoint-ish). `side` is 'buy' or 'sell'."""
        data = self._get("/price", {"token_id": token_id, "side": side})
        return _to_float(data.get("price")) if isinstance(data, dict) else None

    def get_price_history(
        self, token_id: str, *, start_ts: int, end_ts: int, fidelity_minutes: int = 60
    ) -> list[tuple[int, float]]:
        """Historical (unix_ts, price) points from the CLOB. Uncached.

        `fidelity_minutes` is the sampling interval. Prices are last/mid-ish,
        not asks — backtests should add a spread assumption on top.
        """
        data = self._get(
            "/prices-history",
            {"market": token_id, "startTs": start_ts, "endTs": end_ts,
             "fidelity": fidelity_minutes},
        )
        out = []
        for row in (data.get("history") or []) if isinstance(data, dict) else []:
            t, p = row.get("t"), _to_float(row.get("p"))
            if t is not None and p is not None:
                out.append((int(t), p))
        return out


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _best_level(levels: list, *, want_max: bool) -> tuple[float, float] | None:
    """Best (price, size) from a CLOB book side; None if side is empty."""
    parsed = [
        (p, _to_float(lvl.get("size")) or 0.0)
        for lvl in levels
        if (p := _to_float(lvl.get("price"))) is not None
    ]
    if not parsed:
        return None
    return max(parsed) if want_max else min(parsed)
