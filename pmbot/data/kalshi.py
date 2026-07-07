"""Client for Kalshi's public market-data API (read-only, no auth).

Base: https://api.elections.kalshi.com/trade-api/v2
  GET /markets                       ?status &series_ticker &event_ticker &limit &cursor
  GET /markets/{ticker}              single market
  GET /markets/{ticker}/orderbook    full book (bids only — see note)
  GET /series/{series}/markets/{ticker}/candlesticks   history (backtest)

Notes:
- Market objects embed top-of-book (`yes_bid_dollars`, `yes_ask_dollars`,
  `no_bid_dollars`, `no_ask_dollars`) plus size fields, so a scan over many
  markets needs only /markets calls, not per-market orderbooks.
- The orderbook endpoint returns *bids only* for both sides: buying YES is
  matched against NO bids, so ask_yes = 1 - best_no_bid. We expose that
  derivation on KalshiBook.
- Public market-data endpoints allow ~30 req/s; we stay far below that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
from pmbot.models import KalshiMarket

log = logging.getLogger(__name__)

_RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


@dataclass(frozen=True)
class KalshiBook:
    """Depth for one Kalshi market. Bids-only, ascending by price.

    Each level is (price_dollars, contracts).
    """

    ticker: str
    yes_bids: list[tuple[float, float]] = field(default_factory=list)
    no_bids: list[tuple[float, float]] = field(default_factory=list)

    @property
    def best_yes_bid(self) -> float | None:
        return self.yes_bids[-1][0] if self.yes_bids else None

    @property
    def best_no_bid(self) -> float | None:
        return self.no_bids[-1][0] if self.no_bids else None

    @property
    def yes_ask(self) -> float | None:
        """Cost to buy YES now = 1 - best NO bid (crossing the NO side)."""
        b = self.best_no_bid
        return round(1.0 - b, 4) if b is not None else None

    @property
    def no_ask(self) -> float | None:
        b = self.best_yes_bid
        return round(1.0 - b, 4) if b is not None else None

    def buy_depth(self, side: str, max_levels: int = 3) -> float:
        """Contracts purchasable on `side` ("yes"|"no") within top levels.

        Buying YES consumes NO bids (and vice versa), best levels first.
        """
        levels = self.no_bids if side == "yes" else self.yes_bids
        return sum(qty for _, qty in levels[-max_levels:])


class KalshiClient:
    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.kalshi_api_base).rstrip("/")
        self._client = client or httpx.Client(
            timeout=20.0, headers={"User-Agent": "pmbot/0.1 (+research)"}
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=8),
        retry=retry_if_exception_type(_RETRYABLE),
    )
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._client.get(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    # -- markets -----------------------------------------------------------
    def get_markets(
        self,
        *,
        status: str | None = "open",
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        limit: int = 200,
        max_pages: int = 5,
        exclude_multivariate: bool = True,
        **extra: Any,
    ) -> list[KalshiMarket]:
        """Fetch markets, following pagination up to `max_pages`.

        Multivariate combo markets (parlays; `mve_collection_ticker` set) are
        dropped by default: their titles are outcome soup and they flood the
        generic settled/open feeds, so they're useless for matching.
        """
        params: dict[str, Any] = {"limit": min(max(limit, 100), 1000)}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        params.update(extra)

        out: list[KalshiMarket] = []
        cursor: str | None = None
        for _ in range(max_pages):
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params)
            rows = data.get("markets") or []
            for m in rows:
                if exclude_multivariate and m.get("mve_collection_ticker"):
                    continue
                out.append(self._parse_market(m))
            cursor = data.get("cursor")
            if not cursor or not rows or len(out) >= limit:
                break
        return out[:limit]

    def get_markets_for_series(
        self,
        series_tickers: list[str],
        *,
        status: str | None = "open",
        limit_per_series: int = 200,
        max_pages: int = 3,
    ) -> list[KalshiMarket]:
        """Fetch markets across several series; per-series failures skipped."""
        out: list[KalshiMarket] = []
        for st in series_tickers:
            st = st.strip()
            if not st:
                continue
            try:
                out.extend(
                    self.get_markets(
                        status=status, series_ticker=st,
                        limit=limit_per_series, max_pages=max_pages,
                    )
                )
            except Exception as e:
                log.debug("kalshi: series %s fetch failed: %s", st, e)
        return out

    def get_market(self, ticker: str) -> KalshiMarket | None:
        try:
            data = self._get(f"/markets/{ticker}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        m = data.get("market")
        return self._parse_market(m) if m else None

    def get_orderbook(self, ticker: str, depth: int = 10) -> KalshiBook:
        data = self._get(f"/markets/{ticker}/orderbook", {"depth": depth})
        book = data.get("orderbook_fp") or data.get("orderbook") or {}
        return KalshiBook(
            ticker=ticker,
            yes_bids=_parse_levels(book.get("yes_dollars") or book.get("yes")),
            no_bids=_parse_levels(book.get("no_dollars") or book.get("no")),
        )

    # -- history (backtest) -------------------------------------------------
    def get_candlesticks(
        self,
        series_ticker: str,
        ticker: str,
        *,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
    ) -> list[dict]:
        """Raw candlesticks (period_interval minutes: 1, 60, or 1440)."""
        data = self._get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
        return data.get("candlesticks") or []

    # -- parsing -------------------------------------------------------------
    @staticmethod
    def _parse_market(m: dict) -> KalshiMarket:
        return KalshiMarket(
            ticker=str(m.get("ticker", "")),
            event_ticker=str(m.get("event_ticker", "")),
            title=str(m.get("title", "")),
            subtitle=str(m.get("yes_sub_title") or m.get("subtitle") or ""),
            status=str(m.get("status", "")),
            close_time=_parse_dt(m.get("close_time")),
            yes_bid=_dollars(m.get("yes_bid_dollars")),
            yes_ask=_dollars(m.get("yes_ask_dollars")),
            no_bid=_dollars(m.get("no_bid_dollars")),
            no_ask=_dollars(m.get("no_ask_dollars")),
            yes_ask_size=_to_float_or_none(m.get("yes_ask_size_fp")),
            # Buying NO consumes YES bids, so the size at the NO ask is the
            # YES bid size when the API doesn't report no_ask_size directly.
            no_ask_size=_to_float_or_none(
                m.get("no_ask_size_fp") or m.get("yes_bid_size_fp")
            ),
            volume_24h=_to_float_or_none(m.get("volume_24h_fp") or m.get("volume_24h")),
            liquidity_usd=_dollars(m.get("liquidity_dollars")),
            rules_primary=str(m.get("rules_primary", "")),
            result=str(m.get("result", "")),
        )


def _parse_levels(levels: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    if not isinstance(levels, list):
        return out
    for lvl in levels:
        if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            price = _to_float_or_none(lvl[0])
            qty = _to_float_or_none(lvl[1])
            if price is not None and qty is not None:
                out.append((price, qty))
    out.sort(key=lambda x: x[0])
    return out


def _dollars(v: Any) -> float | None:
    """Parse a dollars string like "0.7000"; treat 0 as 'no quote'."""
    f = _to_float_or_none(v)
    if f is None or f <= 0.0 or f >= 1.0:
        return None
    return f


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_float_or_none(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
