"""Cross-venue market matching: Polymarket <-> Kalshi.

Two layers, deliberately separated:

1. `suggest_pairs` — fuzzy matching (title similarity + close-time proximity)
   that only ever *proposes* candidates for a human to review.
2. `load_confirmed_pairs` — the YAML file of pairs a human has vetted. The
   scanner/strategy trade ONLY confirmed pairs.

Why the human gate: two markets can share a title and still resolve
differently (different data source, deadline, or edge-case rules). A false
match turns a "risk-free" arb into an unhedged double position — the single
biggest way cross-platform arb loses money.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import yaml

from pmbot.models import KalshiMarket, Market

log = logging.getLogger(__name__)

# Words that carry no matching signal in market titles.
_STOPWORDS = frozenset(
    "will the be a an of to in on at by for is are it this that vs or and "
    "before after during than more less over under".split()
)
# Date words add noise, not signal: equivalence across venues is enforced by
# the close-time window instead, so "jul 4" vs "2026-07-04" shouldn't hurt.
_DATE_WORDS = frozenset(
    "jan january feb february mar march apr april may jun june jul july aug "
    "august sep september oct october nov november dec december today "
    "tomorrow tonight".split()
)
# Light synonym folding for cross-venue phrasing ("X Winner?" vs "Will X win?").
_SYNONYMS = {
    "winner": "win", "wins": "win", "winning": "win", "beat": "win",
    "beats": "win", "defeat": "win", "defeats": "win",
    "advances": "advance", "advancing": "advance", "qualifies": "advance",
    "qualify": "advance",
    "above": "higher", "below": "lower",
}

# Tokens that change WHAT a market resolves on. If one title has one of
# these and the other doesn't, the markets are probably different questions
# ("Will France advance?" vs "France to win in regulation?") even when every
# other word lines up — a mismatch here cost real money in backtests.
_CRITICAL_TOKENS = frozenset(
    "advance tie draw spread series mvp finals semifinal quarterfinal "
    "extra overtime penalties shutout sweep exactly range between".split()
)

_DEFAULT_PAIRS_PATH = Path(__file__).resolve().parent.parent / "config" / "arb_pairs.yaml"


def _is_date_token(t: str) -> bool:
    if t in _DATE_WORDS:
        return True
    if t.isdigit():
        n = int(t)
        return n <= 31 or 1900 <= n <= 2100    # day-of-month or year
    return False


def normalize_title(text: str) -> str:
    """Lowercase, strip markdown/punct/non-ascii/date noise, fold synonyms."""
    text = text.lower()
    text = re.sub(r"\*\*|__", " ", text)           # markdown emphasis
    text = re.sub(r"reg[.]? time:?", " ", text)    # Kalshi subtitle boilerplate
    text = text.encode("ascii", "ignore").decode() # mojibake / degree signs
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = [
        _SYNONYMS.get(t, t)
        for t in text.split()
        if t not in _STOPWORDS and not _is_date_token(t)
    ]
    return " ".join(tokens)


def title_similarity(a: str, b: str) -> float:
    """Blend of sequence similarity and token-set overlap, 0..1.

    A containment bonus rewards one title's tokens being a subset of the
    other's — common when one venue's title is terse ("France Winner?")
    and the other's is a full sentence.
    """
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    if not (ta and tb):
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    containment = len(ta & tb) / min(len(ta), len(tb))
    score = 0.45 * seq + 0.35 * jaccard + 0.20 * containment
    # Asymmetric outcome-critical wording -> almost certainly a different
    # question; halve the score so it can't clear sane thresholds.
    if (ta ^ tb) & _CRITICAL_TOKENS:
        score *= 0.5
    return min(score, 1.0)


@dataclass(frozen=True)
class ConfirmedPair:
    """A human-vetted equivalence between one PM outcome and one Kalshi side.

    Semantics: holding `pm_outcome` on the Polymarket market is equivalent to
    holding YES on the Kalshi market when `aligned` is True (NO when False).
    """

    pm_market_id: str          # Polymarket condition id
    kalshi_ticker: str
    pm_outcome: str = "Yes"
    aligned: bool = True
    note: str = ""

    @property
    def uid(self) -> str:
        return f"{self.pm_market_id}:{self.pm_outcome}:{self.kalshi_ticker}"


@dataclass(frozen=True)
class MatchCandidate:
    """A fuzzy-matched pair awaiting human confirmation."""

    pm_market: Market
    kalshi_market: KalshiMarket
    similarity: float
    close_diff_hours: float | None

    def as_yaml_stub(self) -> str:
        """Ready-to-paste stub for arb_pairs.yaml (still needs human review)."""
        return (
            f"  - pm_market_id: \"{self.pm_market.market_id}\"\n"
            f"    kalshi_ticker: \"{self.kalshi_market.ticker}\"\n"
            f"    pm_outcome: \"Yes\"\n"
            f"    aligned: true\n"
            f"    note: \"sim={self.similarity:.2f} | PM: {self.pm_market.question[:60]}"
            f" | K: {self.kalshi_market.title[:60]}\"\n"
        )


@dataclass
class ArbPairsConfig:
    pairs: list[ConfirmedPair] = field(default_factory=list)
    # (pm_market_id, kalshi_ticker) combos never to suggest again.
    rejected: set[tuple[str, str]] = field(default_factory=set)


def load_pairs_config(path: str | Path | None = None) -> ArbPairsConfig:
    p = Path(path) if path else _DEFAULT_PAIRS_PATH
    if not p.exists():
        return ArbPairsConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    pairs = []
    for row in raw.get("pairs") or []:
        try:
            pairs.append(
                ConfirmedPair(
                    pm_market_id=str(row["pm_market_id"]),
                    kalshi_ticker=str(row["kalshi_ticker"]),
                    pm_outcome=str(row.get("pm_outcome", "Yes")),
                    aligned=bool(row.get("aligned", True)),
                    note=str(row.get("note", "")),
                )
            )
        except KeyError as e:
            log.warning("arb_pairs.yaml: skipping pair missing %s: %r", e, row)
    rejected = {
        (str(r["pm_market_id"]), str(r["kalshi_ticker"]))
        for r in (raw.get("rejected") or [])
        if isinstance(r, dict) and "pm_market_id" in r and "kalshi_ticker" in r
    }
    return ArbPairsConfig(pairs=pairs, rejected=rejected)


def _subtitle_side_ok(pm_question: str, kalshi_subtitle: str) -> bool:
    """Kalshi sub-markets encode WHICH outcome in the subtitle ("Reg Time:
    Canada"). If that side shares no token with the PM question, the match is
    the wrong side of a multi-outcome event ("Will Morocco win?" <-> the
    Canada market) — reject it outright.

    Empty/boilerplate-only subtitles pass (nothing to contradict); numeric
    strike subtitles ("$62,700 or above") pass on shared numbers too.
    """
    sub = set(normalize_title(kalshi_subtitle).split())
    if not sub:
        return True
    q = set(normalize_title(pm_question).split())
    return bool(sub & q)


def suggest_pairs(
    pm_markets: list[Market],
    kalshi_markets: list[KalshiMarket],
    *,
    min_similarity: float = 0.60,
    max_close_diff_hours: float = 36.0,
    exclude: set[tuple[str, str]] | None = None,
    top_k_per_market: int = 2,
    include_closed: bool = False,
) -> list[MatchCandidate]:
    """Fuzzy-match binary markets across venues. Suggestions only.

    For each PM market, keep the top-k Kalshi candidates above the similarity
    floor whose close times agree within the window (when both are known).
    `include_closed=True` also matches resolved markets (backtest pairing).
    """
    exclude = exclude or set()
    out: list[MatchCandidate] = []
    # Binary PM markets only: exactly a Yes/No token pair.
    pm_binary = [
        m for m in pm_markets
        if set(m.tokens) >= {"Yes", "No"} and (include_closed or not m.closed)
    ]
    kalshi_open = [
        k for k in kalshi_markets if k.title and (include_closed or k.is_open)
    ]

    for pm in pm_binary:
        scored: list[MatchCandidate] = []
        for k in kalshi_open:
            if (pm.market_id, k.ticker) in exclude:
                continue
            diff_h: float | None = None
            if pm.end_date and k.close_time:
                diff_h = abs((pm.end_date - k.close_time).total_seconds()) / 3600.0
                if diff_h > max_close_diff_hours:
                    continue
            if not _subtitle_side_ok(pm.question, k.subtitle):
                continue
            sim = title_similarity(pm.question, f"{k.title} {k.subtitle}")
            if sim < min_similarity:
                continue
            scored.append(MatchCandidate(pm, k, round(sim, 4), diff_h))
        # Tiebreak equal similarity by closest close time.
        scored.sort(key=lambda c: (-c.similarity,
                                   c.close_diff_hours if c.close_diff_hours is not None else 1e9))
        out.extend(scored[:top_k_per_market])

    out.sort(key=lambda c: -c.similarity)
    return out
