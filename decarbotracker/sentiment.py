"""Orientační odhad nálady diskurzu: vážený průměr vyznění zpráv (hodnocení AI, -2 … +2).

Jde o přibližný ukazatel z omezeného vzorku, ne o měření. Vyznění se posuzuje z perspektivy
dekarbonizace ČR a podpory veřejnosti (stejně jako SWOT), ne podle tónu jazyka.
"""

from __future__ import annotations

from pydantic import BaseModel

from decarbotracker.config import Settings
from decarbotracker.llm import ClaudeClient
from decarbotracker.models import REGION_ORDER, RegionSentiment, ScoredItem, SentimentSummary, WeeklyReport

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


Row = tuple[str, float, int, str]  # (id položky, váha, vyznění −2…+2, region)


def _stats(rows: list[Row]) -> tuple[int, float, float, float]:
    total = sum(max(w, 0.1) for _, w, _, _ in rows)
    mean = sum(max(w, 0.1) * v for _, w, v, _ in rows) / total
    neg = sum(max(w, 0.1) for _, w, v, _ in rows if v < 0) / total
    pos = sum(max(w, 0.1) for _, w, v, _ in rows if v > 0) / total
    return round(mean / 2 * 100), round(neg, 3), round(1 - neg - pos, 3), round(pos, 3)


def _summary(rows: list[Row], pickable: set[str]) -> SentimentSummary | None:
    if len(rows) < MIN_ITEMS:
        return None
    index, neg, neu, pos = _stats(rows)
    regions = []
    for region in sorted({r for *_, r in rows}, key=lambda r: REGION_ORDER.get(r, 9)):
        sub = [row for row in rows if row[3] == region]
        r_index, r_neg, r_neu, r_pos = _stats(sub)
        regions.append(RegionSentiment(region=region, n=len(sub), negative=r_neg, neutral=r_neu, positive=r_pos,
                                       index=r_index))
    cand = [row for row in rows if row[0] in pickable]
    pos_pick = max((row for row in cand if row[2] > 0), key=lambda row: (row[2], row[1]), default=None)
    neg_pick = min((row for row in cand if row[2] < 0), key=lambda row: (row[2], -row[1]), default=None)
    return SentimentSummary(
        index=index, label_cs=label_for(index), n=len(rows), negative=neg, neutral=neu, positive=pos,
        by_region=regions, most_positive_id=pos_pick[0] if pos_pick else None,
        most_negative_id=neg_pick[0] if neg_pick else None,
    )


def summarize(scored: list[ScoredItem], report_item_ids: set[str], min_relevance: int = 3) -> SentimentSummary | None:
    """Odhad z relevantních zpráv ohodnocených AI; váha = finální skóre (důležitější zprávy váží víc)."""
    rows = [(s.item.id, s.final_score, s.score.sentiment, s.score.geo_focus)
            for s in scored if s.scored_by == "llm" and s.score.relevance >= min_relevance]
    return _summary(rows, report_item_ids)


def summarize_report_items(report: WeeklyReport) -> SentimentSummary | None:
    """Odhad jen z položek uložených v přehledu (pro dodatečné doplnění starších týdnů)."""
    rows = [(i.id, i.final_score, i.sentiment, i.geo_focus) for i in report.items]
    return _summary(rows, {i.id for i in report.items})


class _Rating(BaseModel):
    item_id: str
    sentiment: int


class _RatingBatch(BaseModel):
    ratings: list[_Rating]


BACKFILL_SYSTEM = (
    "U každé položky odhadni její vyznění pro dekarbonizaci ČR a podporu veřejnosti – zda jde spíše o dobrou, "
    "nebo špatnou zprávu pro transformaci, ne o tón jazyka. Škála: -2 výrazně nepříznivé, -1 spíše nepříznivé, "
    "0 neutrální/smíšené/čistě informativní, 1 spíše příznivé, 2 výrazně příznivé. Při nejistotě volte 0. "
    "Vrať hodnocení pro každou položku, item_id přesně podle vstupu."
)


def backfill(report: WeeklyReport, client: ClaudeClient, settings: Settings) -> WeeklyReport:
    """Doplní vyznění k položkám už hotového přehledu (bez nového stahování) a spočítá odhad."""
    user = "\n".join(f"[ID: {i.id}] {i.title} | {i.source_name} | {i.geo_focus}\n  {i.one_line_cs}" for i in report.items)
    parsed, _ = client.structured(model=settings.llm.model_scoring, system=BACKFILL_SYSTEM, user=user,
                                  schema=_RatingBatch, max_tokens=4000, effort=settings.llm.scoring_effort)
    by_id = {r.item_id: max(-2, min(2, r.sentiment)) for r in parsed.ratings}
    for it in report.items:
        it.sentiment = by_id.get(it.id, 0)
    report.sentiment = summarize_report_items(report)
    return report
