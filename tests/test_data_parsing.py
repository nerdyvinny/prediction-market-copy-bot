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


# --- Data API v2 (v1 retired 2026-11-02 and answers 410 on every route) ----
# A real /v2/trades row (trimmed).
TRADE_V2 = {
    "proxy_wallet": "0xc7681a3b05052fdb3ec737bf6a6cfe175e1efdbf",
    "side": "BUY",
    "token_id": "3028730761916982946156889971869341610476256386836911451297450809391475011255",
    "condition_id": "0x6016a045cd1d6f5201f00b0cd09a1c8bbf9991c020bf850f6425e6525b6ccf8e",
    "size": 94.8208,
    "price": 0.9753132646,
    "timestamp": 1794248613,
    "slug": "eth-updown-5m-1794248400",
    "event_slug": "",
    "outcome": "Down",
    "transaction_hash": "0x6e7a47d6f05f04ada619c4109745fceffd2e4b77ee80fad2ff9b4229dc593468",
}

# A real /v2/positions row (trimmed).
POSITION_V2 = {
    "proxy_wallet": "0xc69bd5567b40ef4d11922eaa57e1f9be1c642076",
    "token_id": "15907269680083135091620869613557617263613890890387514554477997501123964677519",
    "condition_id": "0x98c266979031b3ee6c87ea5b8b671be0e242e13f9be4c123ff2c21ef46ff62c3",
    "current_size": 138740.278,
    "avg_price": 0.3362,
    "current_price": 0.625,
    "current_value": 86712.6737,
    "unrealized_pnl": 40056.3059,
    "percent_pnl": 85.8538,
    "redeemable": False,
    "title": "Will Gen.G win Worlds 2026?",
    "slug": "will-genptg-win-worlds-2026",
    "event_slug": "worlds-2026-winner",
    "outcome": "Yes",
    "end_date": "2026-11-29",
}


class _PagedClient:
    """Serves a fixed list of v2 trade rows through cursor pages."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[dict] = []

    def get(self, url, params=None):
        params = dict(params or {})
        self.calls.append(params)
        start = int(params.get("cursor", 0))
        end = start + int(params["limit"])
        page = self.rows[start:end]
        more = end < len(self.rows)
        return _Response({
            "data": page,
            "pagination": {"has_more": more, "next_cursor": str(end) if more else None},
        })


def test_v2_trade_row_parses_like_a_v1_row():
    t = PolymarketDataClient._parse_trade(TRADE_V2)
    assert t.leader == TRADE_V2["proxy_wallet"]
    assert t.token_id == TRADE_V2["token_id"]          # was `asset` in v1
    assert t.market_id == TRADE_V2["condition_id"]
    assert t.side is Side.BUY and t.shares == 94.8208
    assert t.uid == TRADE_V2["transaction_hash"]
    assert t.event_slug is None                          # v2 sends "" for none


def test_raw_trades_unwrap_the_envelope_and_carry_v1_keys():
    http = _PagedClient([TRADE_V2])
    raw = PolymarketDataClient(client=http, bypass_cache=False).get_raw_trades(user="0xlead")
    assert len(raw) == 1
    row = raw[0]
    assert row["proxyWallet"] == TRADE_V2["proxy_wallet"]
    assert row["asset"] == TRADE_V2["token_id"]
    assert row["conditionId"] == TRADE_V2["condition_id"]
    assert row["token_id"] == TRADE_V2["token_id"]       # v2 keys kept too
    assert "offset" not in http.calls[-1]                # v2 answers 400 to it
    assert "cursor" not in http.calls[-1]                # and to an empty cursor


def test_market_filter_is_sent_as_condition():
    http = _PagedClient([])
    PolymarketDataClient(client=http, bypass_cache=False).get_raw_trades(market="0xcid")
    assert http.calls[-1]["condition"] == "0xcid" and "market" not in http.calls[-1]


def test_sequential_offset_paging_costs_one_request_per_page():
    rows = [dict(TRADE_V2, transaction_hash=f"0x{i}") for i in range(10)]
    http = _PagedClient(rows)
    client = PolymarketDataClient(client=http, bypass_cache=False)
    got = []
    for offset in (0, 4, 8):
        got += client.get_raw_trades(user="0xlead", limit=4, offset=offset)
    assert [r["transactionHash"] for r in got] == [f"0x{i}" for i in range(10)]
    assert len(http.calls) == 3
    assert all("offset" not in c for c in http.calls)


def test_cold_offset_walks_forward_and_past_the_end_is_empty():
    rows = [dict(TRADE_V2, transaction_hash=f"0x{i}") for i in range(10)]
    http = _PagedClient(rows)
    client = PolymarketDataClient(client=http, bypass_cache=False)
    page = client.get_raw_trades(user="0xlead", limit=3, offset=6)
    assert [r["transactionHash"] for r in page] == ["0x6", "0x7", "0x8"]
    assert client.get_raw_trades(user="0xother", limit=3, offset=50) == []


def test_positions_carry_the_v1_names_the_dashboard_reads():
    http = _RecordingClient({"data": [POSITION_V2], "pagination": {"has_more": False}})
    p = PolymarketDataClient(client=http).get_positions("0xlead")[0]
    assert p["asset"] == POSITION_V2["token_id"]
    assert p["size"] == POSITION_V2["current_size"]
    assert p["avgPrice"] == 0.3362 and p["curPrice"] == 0.625
    assert p["currentValue"] == POSITION_V2["current_value"]
    assert p["cashPnl"] == POSITION_V2["unrealized_pnl"]
    assert p["percentPnl"] == POSITION_V2["percent_pnl"]
    assert p["endDate"] == "2026-11-29" and p["eventSlug"] == "worlds-2026-winner"
    assert http.calls[-1][0].endswith("/v2/positions")


def test_value_and_holders_read_the_v2_shapes():
    http = _RecordingClient({"data": {"proxy_wallet": "0xa", "value": 93864.8414}})
    assert PolymarketDataClient(client=http).get_value("0xa") == 93864.8414

    http = _RecordingClient({"data": [{"token_id": "t1", "holders": [
        {"proxy_wallet": "0xh1", "amount": 5.0}]}]})
    client = PolymarketDataClient(client=http)
    groups = client.get_holders("0xcid")
    assert groups[0]["token"] == "t1"
    assert groups[0]["holders"][0]["proxyWallet"] == "0xh1"
    assert http.calls[-1][1]["condition"] == "0xcid"


class _GammaClient:
    """Answers /markets from {condition id: (row, closed)}, like Gamma does:
    the default query returns only non-closed markets."""

    def __init__(self, markets):
        self.markets = markets
        self.calls: list[dict] = []

    def get(self, url, params=None):
        params = dict(params or {})
        self.calls.append(params)
        want_closed = params.get("closed") == "true"
        rows = [row for cid, (row, closed) in self.markets.items()
                if cid in params["condition_ids"] and closed == want_closed]
        return _Response(rows)


def test_markets_by_ids_batches_and_falls_back_to_closed():
    open_m = dict(MARKET, conditionId="0xopen")
    closed_m = dict(MARKET, conditionId="0xshut", closed=True)
    http = _GammaClient({"0xopen": (open_m, False), "0xshut": (closed_m, True)})
    found = GammaClient(client=http).get_markets_by_ids(
        ["0xopen", "0xshut", "0xnope", "0xopen"], chunk=50)
    assert set(found) == {"0xopen", "0xshut"}
    assert len(http.calls) == 2                       # one open pass + one closed pass
    assert http.calls[1]["condition_ids"] == ["0xshut", "0xnope"]   # only the misses
