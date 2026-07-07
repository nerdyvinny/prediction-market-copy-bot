"""Matcher: normalization, similarity, suggestion filtering, YAML config."""

from datetime import datetime, timedelta, timezone

from pmbot.arb.matcher import (
    ConfirmedPair,
    load_pairs_config,
    normalize_title,
    suggest_pairs,
    title_similarity,
)
from pmbot.models import KalshiMarket, Market

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


def pm_market(question: str, *, market_id: str = "0xaaa", end: datetime | None = NOW) -> Market:
    return Market(
        market_id=market_id,
        question=question,
        end_date=end,
        closed=False,
        tokens={"Yes": "tok-yes", "No": "tok-no"},
    )


def k_market(title: str, *, ticker: str = "KX-TEST", close: datetime | None = NOW) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker, event_ticker="KX", title=title, status="active", close_time=close
    )


class TestNormalization:
    def test_strips_markdown_punctuation_stopwords(self):
        assert normalize_title("Will the **Lakers** win?") == "lakers win"

    def test_drops_non_ascii(self):
        # Kalshi titles carry degree signs / mojibake.
        assert "98" in normalize_title("high temp in NYC be <98° on Jul 4?")

    def test_similar_wordings_score_high(self):
        sim = title_similarity(
            "Will Donald Trump win the 2028 election?",
            "Will Trump win the 2028 US election?",
        )
        assert sim > 0.7

    def test_unrelated_markets_score_low(self):
        sim = title_similarity(
            "Will the Lakers win the NBA championship?",
            "Will it rain in Seattle tomorrow?",
        )
        assert sim < 0.4

    def test_critical_token_asymmetry_penalized(self):
        """'advance' vs 'win in regulation' are different questions — a real
        backtest mismatch that lost money before this penalty existed."""
        win = title_similarity(
            "Will France win on 2026-07-04?", "Paraguay vs France Winner? France"
        )
        advance = title_similarity(
            "Will France advance on 2026-07-04?", "Paraguay vs France Winner? France"
        )
        assert advance < win * 0.75
        assert advance < 0.5

    def test_date_tokens_ignored(self):
        # Close-time window handles dates; titles shouldn't fight over format.
        a = title_similarity("Will Bitcoin close above $200k on July 4?",
                             "Bitcoin above $200k on Jul 4?")
        b = title_similarity("Will Bitcoin close above $200k on 2026-07-04?",
                             "Bitcoin above $200k on Jul 4?")
        assert abs(a - b) < 0.05


class TestSuggestPairs:
    def test_finds_obvious_match(self):
        pms = [pm_market("Will Bitcoin close above $200k on July 4?")]
        ks = [k_market("Bitcoin above $200k on Jul 4?")]
        out = suggest_pairs(pms, ks, min_similarity=0.5)
        assert len(out) == 1
        assert out[0].pm_market.market_id == "0xaaa"

    def test_respects_close_time_window(self):
        pms = [pm_market("Will Bitcoin close above $200k on July 4?")]
        ks = [k_market("Bitcoin above $200k on Jul 4?", close=NOW + timedelta(days=10))]
        assert suggest_pairs(pms, ks, min_similarity=0.5, max_close_diff_hours=36) == []

    def test_excludes_known_combos(self):
        pms = [pm_market("Will Bitcoin close above $200k on July 4?")]
        ks = [k_market("Bitcoin above $200k on Jul 4?")]
        out = suggest_pairs(pms, ks, min_similarity=0.5, exclude={("0xaaa", "KX-TEST")})
        assert out == []

    def test_skips_non_binary_and_closed(self):
        weird = Market(market_id="0xbbb", question="Which team wins?", closed=False,
                       tokens={"A": "1", "B": "2", "C": "3"})
        closed = Market(market_id="0xccc", question="Bitcoin above $200k on Jul 4?",
                        closed=True, tokens={"Yes": "y", "No": "n"}, end_date=NOW)
        ks = [k_market("Bitcoin above $200k on Jul 4?")]
        assert suggest_pairs([weird, closed], ks, min_similarity=0.3) == []

    def test_no_close_time_still_matches(self):
        # Missing dates shouldn't block a strong title match (human vets anyway).
        pms = [pm_market("Will Bitcoin close above $200k on July 4?", end=None)]
        ks = [k_market("Bitcoin above $200k on Jul 4?", close=None)]
        assert len(suggest_pairs(pms, ks, min_similarity=0.5)) == 1

    def test_subtitle_naming_other_team_rejected(self):
        """Kalshi sub-market subtitles name the outcome side. 'Will Morocco
        win?' must not match the Canada side of 'Canada vs Morocco Winner?'
        (a real backtest mismatch)."""
        pms = [pm_market("Will Morocco win on 2026-07-04?")]
        wrong = KalshiMarket(ticker="KX-CAN", event_ticker="KX", status="active",
                             title="Canada vs Morocco Winner?",
                             subtitle="Reg Time: Canada", close_time=NOW)
        right = KalshiMarket(ticker="KX-MAR", event_ticker="KX", status="active",
                             title="Canada vs Morocco Winner?",
                             subtitle="Reg Time: Morocco", close_time=NOW)
        out = suggest_pairs(pms, [wrong, right], min_similarity=0.4)
        assert [c.kalshi_market.ticker for c in out] == ["KX-MAR"]

    def test_boilerplate_only_subtitle_passes(self):
        # Subtitle that normalizes to nothing can't contradict the question.
        pms = [pm_market("Will Bitcoin close above $200k on July 4?")]
        ks = [k_market("Bitcoin above $200k on Jul 4?")]  # subtitle ""
        assert len(suggest_pairs(pms, ks, min_similarity=0.5)) == 1


class TestPairsConfig:
    def test_roundtrip(self, tmp_path):
        p = tmp_path / "arb_pairs.yaml"
        p.write_text(
            """
pairs:
  - pm_market_id: "0xabc"
    kalshi_ticker: "KX-A"
    pm_outcome: "Yes"
    aligned: false
    note: "checked"
rejected:
  - pm_market_id: "0xdef"
    kalshi_ticker: "KX-B"
""",
            encoding="utf-8",
        )
        cfg = load_pairs_config(p)
        assert cfg.pairs == [
            ConfirmedPair(pm_market_id="0xabc", kalshi_ticker="KX-A",
                          pm_outcome="Yes", aligned=False, note="checked")
        ]
        assert cfg.rejected == {("0xdef", "KX-B")}

    def test_missing_file_gives_empty_config(self, tmp_path):
        cfg = load_pairs_config(tmp_path / "nope.yaml")
        assert cfg.pairs == [] and cfg.rejected == set()

    def test_malformed_pair_skipped(self, tmp_path):
        p = tmp_path / "arb_pairs.yaml"
        p.write_text("pairs:\n  - kalshi_ticker: KX-ONLY\n", encoding="utf-8")
        assert load_pairs_config(p).pairs == []
