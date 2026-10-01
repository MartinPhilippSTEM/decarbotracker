import re

import pytest
from conftest import make_item, make_scored

from decarbotracker.render import build_site
from decarbotracker.storage import week_path, write_json
from decarbotracker.synthesis import synthesize


@pytest.fixture
def with_report(settings):
    sel = [make_scored(make_item(i, region="CZ" if i < 4 else "EU"), relevance=7, attitudes=i % 3 == 0)
           for i in range(14)]
    rep = synthesize("2026-W39", sel, 40, settings, None)
    write_json(week_path("2026-W39"), rep)
    return rep


@pytest.mark.parametrize("base", ["/", "/decarbotracker/"])
def test_render_respects_base_url(settings, with_report, tmp_path, base):
    out = build_site(settings, out_dir=tmp_path / "site", base_url=base)
    for rel in ["index.html", "archiv/index.html", "metodika/index.html", "dotazy/index.html",
                "tydny/2026-w39/index.html", "feed.xml", "404.html", "static/style.css", "static/logo.svg",
                "static/favicon.svg"]:
        assert (out / rel).exists(), rel
    html = (out / "index.html").read_text(encoding="utf-8")
    internal = re.findall(r'(?:href|src)="(/[^"]*)"', html)
    assert internal, "žádné interní odkazy"
    assert all(u.startswith(base) for u in internal), [u for u in internal if not u.startswith(base)]
    if base != "/":
        assert 'href="/static/' not in html
    assert f'{base}static/style.css' in html


def test_every_evidence_link_has_anchor(settings, with_report, tmp_path):
    out = build_site(settings, out_dir=tmp_path / "site")
    html = (out / "index.html").read_text(encoding="utf-8")
    targets = set(re.findall(r'href="#i-([0-9a-f]+)"', html))
    anchors = set(re.findall(r'id="i-([0-9a-f]+)"', html))
    assert targets and targets <= anchors


def test_page_size_under_200kb(settings, with_report, tmp_path):
    out = build_site(settings, out_dir=tmp_path / "site")
    css = (out / "static" / "style.css").stat().st_size
    for page in ["index.html", "metodika/index.html"]:
        assert (out / page).stat().st_size + css < 200_000


def test_render_without_data(settings, tmp_path):
    out = build_site(settings, out_dir=tmp_path / "site")
    assert "Zatím tu není žádný týdenní přehled" in (out / "index.html").read_text(encoding="utf-8")


def test_feed_is_valid_xml(settings, with_report, tmp_path):
    import xml.etree.ElementTree as ET

    settings.site.site_url = "https://example.github.io/decarbotracker/"
    out = build_site(settings, out_dir=tmp_path / "site", base_url="/decarbotracker/")
    root = ET.parse(out / "feed.xml").getroot()
    link = root.find("channel/item/link").text
    assert link == "https://example.github.io/decarbotracker/tydny/2026-w39/"


def test_rich_filter_escapes_html_and_renders_bold():
    from decarbotracker.render import plain, rich

    out = str(rich("Podpora <script>x</script> **roste o 5 %** a **klesá**"))
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<strong>roste o 5 %</strong>" in out and "<strong>klesá</strong>" in out
    assert str(rich("lichý ** znak")) == "lichý  znak"
    assert plain("**a** b") == "a b"


def test_glance_tables_rendered(settings, with_report, tmp_path):
    out = build_site(settings, out_dir=tmp_path / "site")
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "Pět nejdůležitějších analýz a článků" in html
    assert "Události a termíny" in html
    assert html.index("Na první pohled") < html.index('id="swot"')
    assert "<strong>deterministický ukázkový výstup</strong>" in html
    assert "**" not in html


def test_opportunities_table_and_back_to_top(settings, with_report, tmp_path):
    from decarbotracker.models import Opportunity, WeeklyReport
    from decarbotracker.storage import read_json

    rep = WeeklyReport.model_validate(read_json(week_path("2026-W39")))
    rep.opportunities_cs = [Opportunity(item_id="x1", title_cs="Výzva SIGMA", funder="TA ČR", amount_min="1 mil. Kč",
                                        amount_max="10 mil. Kč", open_from="1. 10. 2026", deadline="15. 12. 2026",
                                        summary_cs="Podpora **společenskovědního** výzkumu.", title="SIGMA",
                                        url="https://tacr.gov.cz/x", source_name="TA ČR")]
    write_json(week_path("2026-W39"), rep)
    html = (build_site(settings, out_dir=tmp_path / "site") / "index.html").read_text(encoding="utf-8")
    assert "Příležitosti: výzvy a financování" in html
    assert "max. 10 mil. Kč" in html and "15. 12. 2026" in html
    assert html.index("Události a termíny") < html.index("Příležitosti: výzvy") < html.index('id="swot"')
    assert 'href="#nahoru"' in html and 'id="nahoru"' in html
