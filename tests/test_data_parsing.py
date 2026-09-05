"""Offline tests for data-layer parsing, using real API response shapes."""

from __future__ import annotations

from datetime import timezone

from pmbot.data.gamma import GammaClient, _parse_dt, _load_json_list
from pmbot.data.polymarket_data import PolymarketDataClient
from pmbot.models import Side

# A real /trades item shape (trimmed).
TRADE = {
    "proxyWallet": "0x6981081df56906bceb0cb7f49e6fae6e332f4178",
    "side": "SELL",
    "asset": "76638532101043658630369484092110685720624002077285242368448475410253808399541",
    "conditionId": "0xbaf7780f9059e34b84301fd411f8dc573b4d56adfe6e0cda33daf304b1438da4",
    "size": 52.47,
    "price": 0.994,
    "timestamp": 1782479734,
    "outcome": "No",
    "outcomeIndex": 1,
    "eventSlug": "world-cup-winner",
    "transactionHash": "0xb1192c7ff19e1e4d44de392c17ea02f7b3f40552c9b893023ae0f2afccc5f26a",
}

# A real /markets item shape (trimmed).
MARKET = {
    "conditionId": "0xbaf7780f9059e34b84301fd411f8dc573b4d56adfe6e0cda33daf304b1438da4",
    "question": "Will Ecuador win the 2026 FIFA World Cup?",
    "slug": "will-ecuador-win-the-2026-fifa-world-cup",
    "endDate": "2026-07-20T00:00:00Z",
    "endDateIso": "2026-07-20T00:00:00Z",
    "liquidityNum": 4278326.19904,
    "closed": False,
    "outcomes": '["Yes", "No"]',
    "clobTokenIds": '["39971087496427056640429359043364261029374524049464674733142166279730655826181", "76638532101043658630369484092110685720624002077285242368448475410253808399541"]',
    "events": [{"slug": "world-cup-winner", "tags": [{"label": "Sports", "slug": "sports"}]}],
}


def test_parse_trade_maps_fields_and_computes_usd():
    t = PolymarketDataClient._parse_trade(TRADE)
    assert t.leader == TRADE["proxyWallet"]
    assert t.side is Side.SELL
    assert t.token_id == TRADE["asset"]
    assert t.market_id == TRADE["conditionId"]
    assert t.shares == 52.47
    assert t.price == 0.994
    assert round(t.usd_size, 2) == round(52.47 * 0.994, 2)
    assert t.timestamp.tzinfo == timezone.utc
    assert t.event_slug == "world-cup-winner"
    assert t.uid == TRADE["transactionHash"]


def test_parse_trade_handles_bad_side_and_missing_fields():
    t = PolymarketDataClient._parse_trade({"side": "weird", "size": None, "price": None})
    assert t.side is Side.BUY          # falls back safely
    assert t.shares == 0.0 and t.price == 0.0
    assert t.usd_size == 0.0
    # uid is derived when no tx hash present
    assert ":" in t.uid


def test_parse_market_decodes_json_strings_and_tokens():
    m = GammaClient._parse_market(MARKET)
    assert m.market_id == MARKET["conditionId"]
    assert m.liquidity_usd == 4278326.19904
    assert m.tokens["Yes"].startswith("39971087")
    assert m.tokens["No"].startswith("76638532")
    assert m.end_date is not None and m.end_date.year == 2026
    assert m.category == "Sports"
    assert m.closed is False


def test_parse_market_keeps_the_slugs_a_public_url_needs():
    """The condition id can't be turned into a link; the slug pair can."""
    m = GammaClient._parse_market(MARKET)
    assert m.slug == "will-ecuador-win-the-2026-fifa-world-cup"
    assert m.event_slug == "world-cup-winner"   # events[0].slug, not the tag slug


def test_parse_market_slugs_are_none_when_gamma_omits_them():
    bare = {"conditionId": "0xabc", "question": "q"}
    m = GammaClient._parse_market(bare)
    assert m.slug is None and m.event_slug is None
    # An empty string is 'missing', not a slug that would build a broken URL.
    assert GammaClient._parse_market({**bare, "slug": "", "events": [{"slug": ""}]}).slug is None
    assert GammaClient._parse_market({**bare, "events": [{"tags": []}]}).event_slug is None


def test_load_json_list_variants():
    assert _load_json_list('["a","b"]') == ["a", "b"]
    assert _load_json_list(["a"]) == ["a"]
    assert _load_json_list(None) == []
    assert _load_json_list("not json") == []


def test_parse_dt_handles_z_suffix_and_none():
    dt = _parse_dt("2026-07-20T00:00:00Z")
    assert dt is not None and dt.tzinfo is not None
    assert _parse_dt(None) is None
    assert _parse_dt("garbage") is None


# --- retry classification -------------------------------------------------
def test_4xx_is_non_retryable_5xx_and_429_are_retryable():
    import httpx
    import pytest

    from pmbot.data.errors import NonRetryableAPIError, raise_for_status_smart

    req = httpx.Request("GET", "http://api.test/x")
    with pytest.raises(NonRetryableAPIError):
        raise_for_status_smart(httpx.Response(404, request=req))
    with pytest.raises(httpx.HTTPStatusError):
        raise_for_status_smart(httpx.Response(500, request=req))
    with pytest.raises(httpx.HTTPStatusError):
        raise_for_status_smart(httpx.Response(429, request=req))
    raise_for_status_smart(httpx.Response(200, request=req))  # no raise


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _RecordingClient:
    """Captures the params of each request instead of making one."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        return _Response(self.payload)


def test_trades_are_cdn_cached_unless_the_bypass_is_on():
    """`/trades` is served with cache-control: max-age=300, and that cache is
    measurably the whole of our copy lag (KS D=0.060 against U(0,300), n=148).
    The bypass adds a parameter the API ignores and the CDN keys on. It is off
    by default because bypassing someone else's cache is an operator's call."""
    http = _RecordingClient([TRADE])
    off = PolymarketDataClient(client=http, bypass_cache=False)
    off.get_raw_trades(user="0xlead", limit=25)
    assert "_" not in http.calls[-1][1]

    on = PolymarketDataClient(client=http, bypass_cache=True)
    on.get_raw_trades(user="0xlead", limit=25)
    first = http.calls[-1][1]
    assert first["_"] and first["user"] == "0xlead" and first["limit"] == 25

    # Unique per request: a fixed value would just seed one more cache entry.
    on.get_raw_trades(user="0xlead", limit=25)
    assert http.calls[-1][1]["_"] != first["_"]

    # Only /trades. The other endpoints are polled rarely enough that the
    # cache costs us nothing, so there is no reason to push load to origin.
    on.get_positions("0xlead")
    assert "_" not in http.calls[-1][1]
