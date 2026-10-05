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
    for rel in ["index.html", "archiv/index.html", "metodika/index.html",
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


def test_evidence_links_open_source_directly(settings, with_report, tmp_path):
    out = build_site(settings, out_dir=tmp_path / "site")
    html = (out / "index.html").read_text(encoding="utf-8")
    evidence = re.findall(r'<span class="evidence">(.*?)</span>', html)
    assert evidence
    hrefs = {h for block in evidence for h in re.findall(r'href="([^"]+)"', block)}
    item_urls = {i.url for i in with_report.items}
    assert hrefs and hrefs <= item_urls  # odkazy vedou přímo na zdroj, ne na kotvu dole
    assert 'href="#i-' not in html


def test_opportunities_grouped_by_programme():
    from decarbotracker.models import Opportunity
    from decarbotracker.render import group_opportunities, program_of

    def opp(title, source):
        return Opportunity(item_id=title, title_cs=title, funder="x", amount_min="1", amount_max="2", open_from="a",
                           deadline="b", summary_cs="s", title=title, url="https://x", source_name=source)

    opps = [opp("A (HORIZON-CL5-2027-07-D3-11)", "EU Funding & Tenders Portal"),
            opp("B (HORIZON-CL2-2027-01-X)", "EU Funding & Tenders Portal"),
            opp("C (LIFE-2027-CET)", "EU Funding & Tenders Portal"),
            opp("Program SIGMA", "Technologická agentura ČR (TA ČR)")]
    assert program_of(opps[0]) == "Horizon Europe" and program_of(opps[2]) == "LIFE"
    groups = group_opportunities(opps)
    assert [g for g, _ in groups][-1] == "Horizon Europe"
    assert dict(groups)["Horizon Europe"] == opps[:2]
    assert "Technologická agentura ČR (TA ČR)" in dict(groups)


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
    assert "1 mil. Kč – 10 mil. Kč" in html and "15. 12. 2026" in html
    assert html.index("Události a termíny") < html.index("Příležitosti: výzvy") < html.index('id="swot"')
    assert 'href="#nahoru"' in html and 'id="nahoru"' in html
