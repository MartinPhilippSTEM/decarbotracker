"""Orientační odhad nálady diskurzu: vážený průměr vyznění zpráv (hodnocení AI, -2 … +2).

Jde o přibližný ukazatel z omezeného vzorku, ne o měření. Vyznění se posuzuje z perspektivy
dekarbonizace ČR a podpory veřejnosti (stejně jako SWOT), ne podle tónu jazyka.
"""

from __future__ import annotations

from decarbotracker.models import REGION_ORDER, RegionSentiment, ScoredItem, SentimentSummary

MIN_ITEMS = 8  # pod tímto počtem LLM-hodnocených zpráv se odhad nezobrazuje


def label_for(index: int) -> str:
    if index <= -40:
        return "spíše nepříznivá"
    if index <= -10:
        return "mírně nepříznivá"
    if index < 10:
        return "přibližně vyrovnaná"
    if index < 40:
        return "mírně příznivá"
    return "spíše příznivá"


def _stats(rows: list[ScoredItem]) -> tuple[int, float, float, float]:
    weights = [max(s.final_score, 0.1) for s in rows]
    total = sum(weights)
    mean = sum(w * s.score.sentiment for w, s in zip(weights, rows, strict=True)) / total
    neg = sum(w for w, s in zip(weights, rows, strict=True) if s.score.sentiment < 0) / total
    pos = sum(w for w, s in zip(weights, rows, strict=True) if s.score.sentiment > 0) / total
    return round(mean / 2 * 100), round(neg, 3), round(1 - neg - pos, 3), round(pos, 3)


def summarize(scored: list[ScoredItem], report_item_ids: set[str], min_relevance: int = 3) -> SentimentSummary | None:
    """Odhad z relevantních zpráv ohodnocených AI; váha = finální skóre (důležitější zprávy váží víc)."""
    rows = [s for s in scored if s.scored_by == "llm" and s.score.relevance >= min_relevance]
    if len(rows) < MIN_ITEMS:
        return None
    index, neg, neu, pos = _stats(rows)
    regions = []
    for region in sorted({s.score.geo_focus for s in rows}, key=lambda r: REGION_ORDER.get(r, 9)):
        sub = [s for s in rows if s.score.geo_focus == region]
        r_index, r_neg, r_neu, r_pos = _stats(sub)
        regions.append(RegionSentiment(region=region, n=len(sub), negative=r_neg, neutral=r_neu, positive=r_pos,
                                       index=r_index))
    in_report = [s for s in rows if s.item.id in report_item_ids]
    pos_pick = max((s for s in in_report if s.score.sentiment > 0),
                   key=lambda s: (s.score.sentiment, s.final_score), default=None)
    neg_pick = min((s for s in in_report if s.score.sentiment < 0),
                   key=lambda s: (s.score.sentiment, -s.final_score), default=None)
    return SentimentSummary(
        index=index, label_cs=label_for(index), n=len(rows), negative=neg, neutral=neu, positive=pos,
        by_region=regions, most_positive_id=pos_pick.item.id if pos_pick else None,
        most_negative_id=neg_pick.item.id if neg_pick else None,
    )
