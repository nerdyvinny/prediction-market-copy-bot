"""Client for Polymarket's public Gamma API (market metadata).

Base: https://gamma-api.polymarket.com
  GET /markets   ?limit &closed &active &order &ascending &condition_ids

Gamma returns several fields as JSON-encoded strings (`outcomes`,
`outcomePrices`, `clobTokenIds`) which we decode here so the rest of the bot
gets clean Python types.
"""

from __future__ import annotations

import json
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
from pmbot.models import Market

log = logging.getLogger(__name__)

_RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


class GammaClient:
    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.gamma_api_base).rstrip("/")
        self._client = client or httpx.Client(
            timeout=20.0, headers={"User-Agent": "pmbot/0.1 (+research)"}
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GammaClient":
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

    def get_markets(
        self,
        *,
        limit: int = 50,
        closed: bool | None = False,
        active: bool | None = True,
        order: str = "volume24hr",
        ascending: bool = False,
        **extra: Any,
    ) -> list[Market]:
        """Fetch markets, by default the most active open ones."""
        params: dict[str, Any] = {"limit": limit, "order": order, "ascending": str(ascending).lower()}
        if closed is not None:
            params["closed"] = str(closed).lower()
        if active is not None:
            params["active"] = str(active).lower()
        params.update(extra)
        data = self._get("/markets", params)
        rows = data if isinstance(data, list) else data.get("data", [])
        return [self._parse_market(m) for m in rows]

    def _fetch_market_raw(self, condition_id: str) -> dict | None:
        """Fetch one market's raw dict, whether it's open OR closed.

        Gamma's /markets defaults to non-closed results; closed markets only
        come back when `closed=true` is passed. So we try the default first
        (finds open markets) and fall back to closed=true.
        """
        for params in (
            {"condition_ids": condition_id, "limit": 1},
            {"condition_ids": condition_id, "limit": 1, "closed": "true"},
        ):
            data = self._get("/markets", params)
            rows = data if isinstance(data, list) else data.get("data", [])
            if rows:
                return rows[0]
        return None

    def get_market(self, condition_id: str) -> Market | None:
        """Fetch a single market by its condition id (open or closed)."""
        m = self._fetch_market_raw(condition_id)
        return self._parse_market(m) if m else None

    def get_resolution(self, condition_id: str) -> tuple[bool, str | None]:
        """Return (closed, winning_token_id) for a market.

        winning_token_id is set only when the market is closed AND one outcome
        settled decisively (price ~1.0). Used to settle leaders' open positions
        when reconstructing their realized P&L.
        """
        m = self._fetch_market_raw(condition_id)
        if not m:
            return (False, None)
        return (bool(m.get("closed", False)), self._winner_from_raw(m))

    def get_market_with_resolution(self, condition_id: str) -> tuple[Market | None, str | None]:
        """One lookup -> (Market, winning_token_id). Used by the backtester."""
        m = self._fetch_market_raw(condition_id)
        if not m:
            return (None, None)
        return (self._parse_market(m), self._winner_from_raw(m))

    @staticmethod
    def _winner_from_raw(m: dict) -> str | None:
        if not bool(m.get("closed", False)):
            return None
        prices = [_to_float_or_none(p) or 0.0 for p in _load_json_list(m.get("outcomePrices"))]
        tokens = _load_json_list(m.get("clobTokenIds"))
        if prices and tokens and len(prices) == len(tokens):
            idx = max(range(len(prices)), key=lambda i: prices[i])
            if prices[idx] >= 0.99:
                return str(tokens[idx])
        return None

    # -- parsing ---------------------------------------------------------
    @classmethod
    def _parse_market(cls, m: dict) -> Market:
        outcomes = _load_json_list(m.get("outcomes"))
        token_ids = _load_json_list(m.get("clobTokenIds"))
        tokens = {
            str(o): str(t)
            for o, t in zip(outcomes, token_ids)
            if o is not None and t is not None
        }
        liquidity = m.get("liquidityNum")
        if liquidity is None:
            liquidity = m.get("liquidity")
        prices = [_to_float_or_none(p) for p in _load_json_list(m.get("outcomePrices"))]
        outcome_prices = {
            str(t): p
            for t, p in zip(token_ids, prices)
            if t is not None and p is not None
        }
        return Market(
            market_id=str(m.get("conditionId", "")),
            question=str(m.get("question", "")),
            category=cls._extract_category(m),
            end_date=_parse_dt(m.get("endDateIso") or m.get("endDate")),
            liquidity_usd=_to_float_or_none(liquidity),
            closed=bool(m.get("closed", False)),
            tokens=tokens,
            outcome_prices=outcome_prices,
        )

    @staticmethod
    def _extract_category(m: dict) -> str | None:
        """Best-effort coarse category from the market's first event tag."""
        events = m.get("events")
        if isinstance(events, list) and events:
            tags = events[0].get("tags")
            if isinstance(tags, list) and tags:
                first = tags[0]
                if isinstance(first, dict):
                    return first.get("label") or first.get("slug")
                return str(first)
        return None


def _load_json_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


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
