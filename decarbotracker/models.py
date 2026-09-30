"""Datové modely: zdroje, položky, skóre a výstupy LLM."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Region = Literal["CZ", "EU", "US", "GLOBAL"]
SourceType = Literal[
    "think_tank", "research", "polling", "government", "media", "journal", "ngo", "industry"
]
Topic = Literal[
    "decarbonization_policy",
    "energy_markets",
    "public_attitudes",
    "communication",
    "forecast",
    "study",
    "just_transition",
    "technology",
]
TOPICS: tuple[str, ...] = Topic.__args__  # type: ignore[attr-defined]
REGION_ORDER = {"CZ": 0, "EU": 1, "US": 2, "GLOBAL": 3}


# --------------------------------------------------------------------------- zdroje


class ScrapeConfig(BaseModel):
    item: str
    title: str
    link: str = ""  # prázdné = odkaz je samotný titulek / první <a> v položce
    date: str = ""  # volitelné
    date_attr: str = ""  # např. "datetime"; jinak se bere text
    summary: str = ""


class Source(BaseModel):
    id: str
    name: str
    type: Literal["rss", "wp_json", "scrape"]
    url: str
    region: Region
    source_type: SourceType
    language: str = "en"
    topic_filter: bool = True
    weight: float = Field(default=1.0, ge=0.5, le=1.5)
    enabled: bool = True
    notes: str = ""
    homepage: str = ""
    scrape: ScrapeConfig | None = None


# --------------------------------------------------------------------------- položky


class Item(BaseModel):
    id: str
    title: str
    url: str
    source_id: str
    source_name: str
    region: Region
    source_type: SourceType
    language: str = "en"
    published_at: datetime | None = None
    first_seen: datetime
    summary_raw: str = ""
    authors: list[str] = Field(default_factory=list)
    doi: str | None = None
    topic_filter: bool = True
    source_weight: float = 1.0
    date_is_first_seen: bool = False

    @property
    def effective_date(self) -> datetime:
        return self.published_at or self.first_seen


class ItemScore(BaseModel):
    """Výstup LLM skórování pro jednu položku (strukturovaný výstup)."""

    item_id: str
    relevance: int = Field(description="0–10, důležitost pro sledování dekarbonizace ČR a postojů veřejnosti")
    topics: list[Topic]
    geo_focus: Region
    is_public_attitudes_or_communication: bool
    is_original_research: bool = Field(description="originální výzkum / nová data / průzkum, ne komentář")
    relevant_to_cz_eu: bool
    is_opinion: bool = Field(description="op-ed, stanovisko NGO/průmyslu, komentář")
    title_cs: str = Field(description="český překlad názvu, pokud je název anglický; jinak prázdný řetězec")
    one_line_cs: str


class ScoreBatch(BaseModel):
    scores: list[ItemScore]


class ScoredItem(BaseModel):
    item: Item
    score: ItemScore
    final_score: float
    scored_by: Literal["llm", "heuristic"] = "llm"


# --------------------------------------------------------------------------- týdenní report (LLM draft)


class SwotPoint(BaseModel):
    text_cs: str
    evidence_item_ids: list[str]
    geo: Region


class Swot(BaseModel):
    strengths: list[SwotPoint]
    weaknesses: list[SwotPoint]
    opportunities: list[SwotPoint]
    threats: list[SwotPoint]


class TopItem(BaseModel):
    item_id: str
    short_cs: str = Field(default="", description="jedna krátká věta (max. ~20 slov) do přehledové tabulky")
    why_it_matters_cs: str
    key_finding_cs: str
    category: Topic


class Event(BaseModel):
    when: str = Field(description="datum nebo termín doslova ze vstupu, např. '6. 10. 2026'; jinak 'termín neuveden'")
    text_cs: str = Field(description="co se stane (konference, zveřejnění dat, hlasování, publikace…)")
    evidence_item_ids: list[str]


class Survey(BaseModel):
    institution: str = Field(description="kdo průzkum dělal; 'neuvedeno' pokud chybí")
    fieldwork: str = Field(description="termín sběru; 'neuvedeno' pokud chybí")
    country: str
    sample_n: str = Field(description="velikost vzorku N; 'neuvedeno' pokud chybí")
    method: str = Field(description="metoda sběru; 'neuvedeno' pokud chybí")
    finding_cs: str
    evidence_item_ids: list[str]


class Recommendation(BaseModel):
    text_cs: str
    evidence_item_ids: list[str]


class PublicAttitudes(BaseModel):
    summary_cs: str
    surveys: list[Survey]
    communication_recommendations: list[Recommendation]


class Forecast(BaseModel):
    author: str
    horizon: str
    key_figure: str = Field(description="klíčové číslo doslova ze vstupu s jednotkou; jinak 'neuvedeno'")
    text_cs: str
    evidence_item_ids: list[str]


class ByRegion(BaseModel):
    cz: str
    eu: str
    us: str
    global_: str = Field(alias="global", description="globální kontext; může být prázdný řetězec")

    model_config = {"populate_by_name": True}


class WatchItem(BaseModel):
    text_cs: str
    evidence_item_ids: list[str]


class ReportDraft(BaseModel):
    """Část týdenního reportu generovaná modelem."""

    headline_cs: str
    executive_summary_cs: str
    swot: Swot
    top_items: list[TopItem]
    public_attitudes_cs: PublicAttitudes
    forecasts_cs: list[Forecast]
    by_region: ByRegion
    events_cs: list[Event]
    watchlist_cs: list[WatchItem]
    data_gaps_cs: list[str]


class ReportItemRef(BaseModel):
    """Metadata položky uložená s reportem (pro render bez přístupu k surovým datům)."""

    id: str
    title: str
    title_cs: str = ""
    url: str
    source_name: str
    source_type: str
    region: Region
    geo_focus: Region
    published_at: datetime | None
    date_is_first_seen: bool = False
    topics: list[str] = Field(default_factory=list)
    one_line_cs: str = ""
    final_score: float = 0.0
    is_opinion: bool = False
    is_public_attitudes_or_communication: bool = False


class UsageInfo(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, dict[str, float]] = Field(default_factory=dict)


class WeeklyReport(BaseModel):
    week: str
    period_from: datetime
    period_to: datetime
    generated_at: datetime
    model: str
    items_considered: int
    items_selected: int
    status: Literal["ok", "fallback", "dry_run"] = "ok"
    notices_cs: list[str] = Field(default_factory=list)
    cz_content_present: bool = True
    headline_cs: str = ""
    executive_summary_cs: str = ""
    swot: Swot | None = None
    top_items: list[TopItem] = Field(default_factory=list)
    public_attitudes_cs: PublicAttitudes | None = None
    forecasts_cs: list[Forecast] = Field(default_factory=list)
    by_region: ByRegion | None = None
    events_cs: list[Event] = Field(default_factory=list)
    watchlist_cs: list[WatchItem] = Field(default_factory=list)
    data_gaps_cs: list[str] = Field(default_factory=list)
    items: list[ReportItemRef] = Field(default_factory=list)
    usage: UsageInfo = Field(default_factory=UsageInfo)
    validation_log: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- dotaz na téma


class Finding(BaseModel):
    text_cs: str
    evidence_item_ids: list[str]
    geo: Region


class BriefDraft(BaseModel):
    headline_cs: str
    summary_cs: str
    key_findings: list[Finding]
    public_attitudes_cs: str
    forecasts_cs: str
    by_region: ByRegion
    data_gaps_cs: list[str]


class TopicBrief(BaseModel):
    slug: str
    topic: str
    days: int
    period_from: datetime
    period_to: datetime
    generated_at: datetime
    model: str
    status: Literal["ok", "fallback", "dry_run", "empty"] = "ok"
    notices_cs: list[str] = Field(default_factory=list)
    headline_cs: str = ""
    summary_cs: str = ""
    key_findings: list[Finding] = Field(default_factory=list)
    public_attitudes_cs: str = ""
    forecasts_cs: str = ""
    by_region: ByRegion | None = None
    data_gaps_cs: list[str] = Field(default_factory=list)
    items: list[ReportItemRef] = Field(default_factory=list)
    usage: UsageInfo = Field(default_factory=UsageInfo)
    validation_log: list[str] = Field(default_factory=list)
