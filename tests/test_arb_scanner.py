"""Offline tests for ArbScanner + ArbitrageStrategy, using fakes."""

from __future__ import annotations

from datetime import datetime, timezone

from pmbot.arb.matcher import ConfirmedPair
from pmbot.arb.scanner import ArbScanner
from pmbot.models import KalshiMarket, Market, Quote, Side, Venue
from pmbot.portfolio.ledger import Ledger
from pmbot.strategy.arbitrage import ArbitrageStrategy

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

PAIR = ConfirmedPair(pm_market_id="0xmkt", kalshi_ticker="KX-T", pm_outcome="Yes")


def pm_market(closed=False):
    return Market(
        market_id="0xmkt", question="Will X happen?", closed=closed,
        tokens={"Yes": "tok-yes", "No": "tok-no"}, end_date=NOW,
    )


def k_market(*, yes_ask=None, no_ask=None, yes_size=500.0, no_size=500.0, status="active"):
    return KalshiMarket(
        ticker="KX-T", event_ticker="KX", title="Will X happen?", status=status,
        close_time=NOW, yes_ask=yes_ask, no_ask=no_ask,
        yes_ask_size=yes_size, no_ask_size=no_size,
    )


class FakeGamma:
    def __init__(self, market):
        self._m = market

    def get_market(self, condition_id):
        return self._m


class FakeKalshi:
    def __init__(self, market):
        self._m = market

    def get_market(self, ticker):
        return self._m


class FakePrices:
    """token_id -> Quote"""

    def __init__(self, quotes):
        self._q = quotes

    def get_quote(self, token_id, *, force=False):
        return self._q[token_id]


def scanner(pm, k, quotes, **kw):
    kw.setdefault("min_edge", 0.01)
    kw.setdefault("slippage_buffer", 0.005)
    kw.setdefault("max_per_trade_usd", 100.0)
    return ArbScanner(FakeGamma(pm), FakePrices(quotes), FakeKalshi(k), **kw)


class TestScanner:
    def test_detects_pm_yes_plus_kalshi_no(self):
        # PM YES ask 0.40 + Kalshi NO ask 0.55 = 0.95 -> ~3.2% net edge.
        quotes = {
            "tok-yes": Quote("tok-yes", bid=0.39, ask=0.40, ask_size=1000),
            "tok-no": Quote("tok-no", bid=0.59, ask=0.62, ask_size=1000),  # no arb this way
        }
        opps = scanner(pm_market(), k_market(yes_ask=0.62, no_ask=0.55), quotes).scan([PAIR])
        assert len(opps) == 1
        o = opps[0]
        assert o.pm_outcome == "Yes" and o.kalshi_side == "no"
        assert o.sized.profit_usd > 0
        assert o.net_edge_per_pair >= 0.01

    def test_detects_reverse_direction(self):
        # PM NO ask 0.35 + Kalshi YES ask 0.60 = 0.95.
        quotes = {
            "tok-yes": Quote("tok-yes", bid=0.62, ask=0.66, ask_size=1000),
            "tok-no": Quote("tok-no", bid=0.33, ask=0.35, ask_size=1000),
        }
        opps = scanner(pm_market(), k_market(yes_ask=0.60, no_ask=0.42), quotes).scan([PAIR])
        assert len(opps) == 1
        o = opps[0]
        assert o.pm_outcome == "No" and o.kalshi_side == "yes"

    def test_aligned_false_flips_kalshi_sides(self):
        # PM Yes == Kalshi NO, so the hedge for PM Yes is Kalshi YES.
        pair = ConfirmedPair(pm_market_id="0xmkt", kalshi_ticker="KX-T",
                             pm_outcome="Yes", aligned=False)
        quotes = {
            "tok-yes": Quote("tok-yes", bid=0.39, ask=0.40, ask_size=1000),
            "tok-no": Quote("tok-no", bid=0.59, ask=0.62, ask_size=1000),
        }
        opps = scanner(pm_market(), k_market(yes_ask=0.55, no_ask=0.62), quotes).scan([pair])
        assert len(opps) == 1
        assert opps[0].kalshi_side == "yes"

    def test_no_opportunity_when_sum_exceeds_one(self):
        quotes = {
            "tok-yes": Quote("tok-yes", bid=0.49, ask=0.52, ask_size=1000),
            "tok-no": Quote("tok-no", bid=0.47, ask=0.50, ask_size=1000),
        }
        opps = scanner(pm_market(), k_market(yes_ask=0.52, no_ask=0.50), quotes).scan([PAIR])
        assert opps == []

    def test_edge_below_threshold_filtered(self):
        # Gross gap 2c; fee ~1.75c + buffer 0.5c -> net < min_edge.
        quotes = {
            "tok-yes": Quote("tok-yes", bid=0.47, ask=0.48, ask_size=1000),
            "tok-no": Quote("tok-no", bid=0.49, ask=0.52, ask_size=1000),
        }
        opps = scanner(pm_market(), k_market(yes_ask=0.52, no_ask=0.50), quotes).scan([PAIR])
        assert opps == []

    def test_depth_caps_contracts(self):
        quotes = {
            "tok-yes": Quote("tok-yes", bid=0.39, ask=0.40, ask_size=7),
            "tok-no": Quote("tok-no", bid=0.59, ask=0.62, ask_size=1000),
        }
        opps = scanner(pm_market(), k_market(yes_ask=0.62, no_ask=0.55), quotes).scan([PAIR])
        assert len(opps) == 1
        assert opps[0].sized.contracts == 7

    def test_closed_markets_skipped(self):
        quotes = {"tok-yes": Quote("tok-yes", ask=0.40, ask_size=100),
                  "tok-no": Quote("tok-no", ask=0.35, ask_size=100)}
        assert scanner(pm_market(closed=True), k_market(no_ask=0.55), quotes).scan([PAIR]) == []
        assert scanner(pm_market(), k_market(no_ask=0.55, status="closed"), quotes).scan([PAIR]) == []


class TestArbitrageStrategy:
    def _strategy(self, ledger, opps_scanner):
        return ArbitrageStrategy(
            gamma=None, price_cache=None, kalshi=None, ledger=ledger,
            pairs=[PAIR], scanner=opps_scanner,
        )

    def test_emits_two_legs_sharing_group(self):
        quotes = {
            "tok-yes": Quote("tok-yes", bid=0.39, ask=0.40, ask_size=1000),
            "tok-no": Quote("tok-no", bid=0.59, ask=0.62, ask_size=1000),
        }
        sc = scanner(pm_market(), k_market(yes_ask=0.62, no_ask=0.55), quotes)
        led = Ledger(":memory:")
        sigs = list(self._strategy(led, sc).generate())
        led.close()
        assert len(sigs) == 2
        pm_leg = next(s for s in sigs if s.venue == Venue.POLYMARKET.value)
        k_leg = next(s for s in sigs if s.venue == Venue.KALSHI.value)
        assert pm_leg.leg_group and pm_leg.leg_group == k_leg.leg_group
        assert pm_leg.side is Side.BUY and k_leg.side is Side.BUY
        assert pm_leg.source_uid == k_leg.source_uid
        # Same contract count on both legs (hedged): size/price ratio matches.
        n_pm = pm_leg.size_usd / pm_leg.target_price
        n_k = k_leg.size_usd / k_leg.target_price
        assert abs(n_pm - n_k) < 1.0

    def test_dedupes_open_entries_via_ledger(self):
        from datetime import datetime, timezone as tz

        from pmbot.models import Fill, Signal

        quotes = {
            "tok-yes": Quote("tok-yes", bid=0.39, ask=0.40, ask_size=1000),
            "tok-no": Quote("tok-no", bid=0.59, ask=0.62, ask_size=1000),
        }
        sc = scanner(pm_market(), k_market(yes_ask=0.62, no_ask=0.55), quotes)
        led = Ledger(":memory:")
        strat = self._strategy(led, sc)
        first = list(strat.generate())
        assert len(first) == 2
        # Record one leg -> uid marked as copied -> no more signals for the pair.
        sig = first[0]
        led.record_fill(Fill(signal=sig, fill_price=sig.target_price,
                             size_usd=sig.size_usd, shares=1.0,
                             timestamp=datetime.now(tz.utc), mode="paper"))
        assert list(strat.generate()) == []
        led.close()
