"""Scrapery proti HTML uloženému z reálných stránek (30. 9. 2026)."""

import pytest
from conftest import FIXTURES, NOW

from decarbotracker.config import load_sources
from decarbotracker.fetch.feeds import ParseError
from decarbotracker.fetch.scrape import parse_listing

SCRAPE_SOURCES = [s for s in load_sources() if s.type == "scrape" and s.enabled]
UNDATED = {"hnuti-duha"}  # výpis bez dat – použije se datum prvního spatření


def test_every_enabled_scraper_has_fixture():
    missing = [s.id for s in SCRAPE_SOURCES if not (FIXTURES / "html" / f"{s.id}.html").exists()]
    assert not missing, f"Chybí fixture pro scrapery: {missing}"


@pytest.mark.parametrize("source", SCRAPE_SOURCES, ids=lambda s: s.id)
def test_scraper_on_saved_page(source):
    html = (FIXTURES / "html" / f"{source.id}.html").read_bytes()
    items = parse_listing(html, source, now=NOW)
    assert len(items) >= 5, f"{source.id}: jen {len(items)} položek"
    for it in items:
        assert len(it.title) >= 8
        assert it.url.startswith("http"), it.url  # relativní URL převedeny na absolutní
        assert " " not in it.url
    urls = [i.url for i in items]
    assert len(urls) == len(set(urls))
    dated = [i for i in items if i.published_at]
    if source.id in UNDATED:
        assert all(i.date_is_first_seen for i in items)
    else:
        assert len(dated) >= len(items) * 0.8, f"{source.id}: málo položek s datem"
        assert all(i.published_at.year >= 2015 for i in dated)


def test_relative_urls_are_absolutized():
    src = next(s for s in SCRAPE_SOURCES if s.id == "ipsos-cz")
    items = parse_listing((FIXTURES / "html" / "ipsos-cz.html").read_bytes(), src, now=NOW)
    assert all(i.url.startswith("https://www.ipsos.com/cs-cz/") for i in items)


def test_changed_page_raises_parse_error():
    src = SCRAPE_SOURCES[0]
    with pytest.raises(ParseError):
        parse_listing("<html><body><p>Nic tu není</p></body></html>", src)
