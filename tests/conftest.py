"""Společné fixtures. Testy nikdy nevolají síť ani Claude API."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from decarbotracker.models import Item, ItemScore, ScoredItem, Source

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path, monkeypatch):
    """Každý test pracuje v dočasném kořeni (data/, site/), ne v repozitáři. Bez API klíčů."""
    monkeypatch.setenv("DECARBO_ROOT", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.delenv("DECARBO_BASE_URL", raising=False)
    from decarbotracker import config

    config.load_settings.cache_clear()
    yield tmp_path
    config.load_settings.cache_clear()


@pytest.fixture
def settings():
    from decarbotracker.config import load_settings

    s = load_settings()
    s.anthropic_api_key = None
    return s


def read_fixture(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()


def load_json_fixture(*parts: str):
    return json.loads(read_fixture(*parts).decode("utf-8"))


def make_source(**kw) -> Source:
    base = dict(id="test", name="Test", type="rss", url="https://example.org/feed", region="EU",
                source_type="think_tank", language="en", topic_filter=True, weight=1.0, enabled=True)
    base.update(kw)
    return Source(**base)


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def make_item(n: int, *, title: str | None = None, region: str = "EU", source_type: str = "think_tank",
              published: datetime | None = datetime(2026, 9, 23, 10, 0, tzinfo=UTC), url: str | None = None,
              summary: str = "", doi: str | None = None, source_id: str = "src", topic_filter: bool = True) -> Item:
    from decarbotracker.normalize import make_id

    url = url or f"https://example.org/a/{n}"
    return Item(
        id=make_id(url), title=title or f"Článek číslo {n} o klimatu a emisích", url=url, source_id=source_id,
        source_name=source_id.upper(), region=region, source_type=source_type, language="cs" if region == "CZ" else "en",
        published_at=published, first_seen=NOW, summary_raw=summary, doi=doi, topic_filter=topic_filter,
        date_is_first_seen=published is None,
    )


def make_scored(item: Item, *, relevance: int = 7, geo: str | None = None, attitudes: bool = False,
                topics: list[str] | None = None, final: float | None = None) -> ScoredItem:
    s = ItemScore(item_id=item.id, relevance=relevance, topics=topics or ["decarbonization_policy"],
                  geo_focus=geo or item.region, is_public_attitudes_or_communication=attitudes,
                  is_original_research=False, relevant_to_cz_eu=True, is_opinion=False, title_cs="",
                  one_line_cs="Věta.")
    return ScoredItem(item=item, score=s, final_score=final if final is not None else float(relevance))
