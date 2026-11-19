from conftest import make_item, make_scored

from decarbotracker.models import ItemScore
from decarbotracker.scoring import (
    final_score,
    heuristic_score,
    score_items,
    select_for_synthesis,
    topic_clusters,
)


def _score(**kw) -> ItemScore:
    base = dict(item_id="x", relevance=8, topics=["study"], geo_focus="CZ",
                is_public_attitudes_or_communication=False, is_original_research=False, relevant_to_cz_eu=False,
                cz_impact=0, eu_impact=0, is_opinion=False, title_cs="", one_line_cs="", sentiment=0, is_event=False)
    base.update(kw)
    return ItemScore(**base)


def test_final_score_is_additive(settings):
    sel = settings.selection
    item = make_item(1, source_type="polling")
    item.source_weight = 1.0
    base = sel.geo_points["CZ"] + sel.source_type_points["polling"]
    assert final_score(item, _score(relevance=10), sel) == round(10 + base, 4)
    s2 = _score(relevance=10, cz_impact=3, eu_impact=1, is_public_attitudes_or_communication=True,
                is_original_research=True)
    expected = (10 + base + 3 * sel.cz_impact_points + sel.eu_impact_points + sel.attitudes_points
                + sel.research_points)
    assert abs(final_score(item, s2, sel) - expected) < 1e-3


def test_czech_policy_news_beats_foreign_journal_study(settings):
    """Případ z hodnocení: globální psychologická studie nesmí přeskočit českou politickou zprávu."""
    sel = settings.selection
    study = make_item(1, source_type="journal", region="EU")
    study.source_weight = 1.2
    s_study = _score(relevance=7, geo_focus="GLOBAL", cz_impact=1, eu_impact=1,
                     is_public_attitudes_or_communication=True, is_original_research=True)
    news = make_item(2, source_type="media", region="CZ")
    news.source_weight = 0.8
    s_news = _score(relevance=7, geo_focus="CZ", cz_impact=3, eu_impact=1)
    assert final_score(news, s_news, sel) > final_score(study, s_study, sel)


def test_event_gets_no_bonuses(settings):
    sel = settings.selection
    item = make_item(1)
    plain = final_score(item, _score(relevance=6), sel)
    event = final_score(item, _score(relevance=6, is_event=True, is_public_attitudes_or_communication=True), sel)
    assert event == round(plain + sel.event_points, 4)


def test_quotas_cz_and_attitudes(settings):
    sel = settings.selection
    sel.max_items_synthesis = 20
    strong = [make_scored(make_item(i, region="US", source_id=f"us{i}"), relevance=9, final=50 - i) for i in range(30)]
    cz = [make_scored(make_item(100 + i, region="CZ", source_id=f"cz{i}"), relevance=5, final=5 - i * 0.1)
          for i in range(14)]
    att = [make_scored(make_item(200 + i, region="EU", source_id=f"a{i}"), relevance=5, attitudes=True,
                       final=4 - i * 0.1) for i in range(6)]
    chosen = select_for_synthesis(strong + cz + att, sel)
    assert sum(1 for s in chosen if s.score.geo_focus == "CZ") >= sel.min_cz
    assert sum(1 for s in chosen if s.score.is_public_attitudes_or_communication) >= 5
    first_non_cz = next(i for i, s in enumerate(chosen) if s.score.geo_focus != "CZ")
    assert all(s.score.geo_focus != "CZ" for s in chosen[first_non_cz:])


def test_quota_not_enforced_when_missing(settings):
    items = [make_scored(make_item(i, region="EU", source_id=f"s{i}"), relevance=6, final=10 - i) for i in range(8)]
    chosen = select_for_synthesis(items, settings.selection)
    assert len(chosen) == 8


def test_low_relevance_excluded(settings):
    items = [make_scored(make_item(1), relevance=1), make_scored(make_item(2), relevance=6)]
    assert [s.item.id for s in select_for_synthesis(items, settings.selection)] == [items[1].item.id]


def test_cap_counts_organization_not_channel(settings):
    """Grantham má dva kanály (zprávy a publikace) – strop 3 položky platí pro organizaci."""
    sel = settings.selection
    items = [make_scored(make_item(i, source_id="grantham-news" if i % 2 else "grantham-publications"),
                         relevance=8, final=20 - i) for i in range(8)]
    items += [make_scored(make_item(50 + i, source_id=f"x{i}"), relevance=6, final=5 - i) for i in range(sel.max_items_synthesis)]
    org = {"grantham-news": "grantham", "grantham-publications": "grantham"}
    chosen = select_for_synthesis(items, sel, org_of=org)
    assert sum(1 for s in chosen if s.item.source_id.startswith("grantham")) == sel.max_per_source


def test_same_doi_selected_once_and_topic_cap(settings):
    sel = settings.selection
    sel.max_per_topic = 2
    a = make_scored(make_item(1, title="Coal power plants live longer next to data centres", doi="10.1/x",
                              source_id="ncc"), relevance=8, final=12)
    b = make_scored(make_item(2, title="Early evidence links data centre demand to coal", doi="10.1/x",
                              source_id="ncc2"), relevance=8, final=11)
    anx = [make_scored(make_item(10 + i, title=f"Climate anxiety among young adults in study {i}", source_id=f"j{i}"),
                       relevance=8, final=10 - i * 0.1) for i in range(4)]
    chosen = {s.item.id for s in select_for_synthesis([a, b, *anx], sel)}
    assert a.item.id in chosen and b.item.id not in chosen
    assert sum(1 for s in anx if s.item.id in chosen) == 2


def test_topic_clusters_group_similar_titles():
    a = make_scored(make_item(1, title="Climate anxiety among young adults: survey of 10 countries"))
    b = make_scored(make_item(2, title="Climate anxiety among young adults – new survey of 10 countries"))
    c = make_scored(make_item(3, title="EU agrees 2040 emissions target after long night of talks"))
    cl = topic_clusters([a, b, c], 70)
    assert cl[a.item.id] == cl[b.item.id] != cl[c.item.id]


def test_ties_are_broken_by_cz_impact_not_hash(settings):
    x = make_scored(make_item(1, source_id="a"), relevance=7, final=7.0, cz_impact=0)
    y = make_scored(make_item(2, source_id="b"), relevance=7, final=7.0, cz_impact=3)
    chosen = select_for_synthesis([x, y], settings.selection)
    assert [s.item.id for s in chosen] == [y.item.id, x.item.id]


def test_heuristic_is_deterministic_and_detects_czech():
    it = make_item(1, title="Průzkum: Češi a postoje k emisním povolenkám", region="EU")
    a, b = heuristic_score(it), heuristic_score(it)
    assert a == b
    assert a.geo_focus == "CZ" and a.cz_impact >= 2
    assert a.is_public_attitudes_or_communication
    assert 0 <= a.relevance <= 10


def test_score_items_without_client_uses_heuristic(settings):
    items = [make_item(i) for i in range(5)]
    scored = score_items(items, settings, None)
    assert len(scored) == 5
    assert all(s.scored_by == "heuristic" for s in scored)
    assert scored == sorted(scored, key=lambda s: -s.final_score)
