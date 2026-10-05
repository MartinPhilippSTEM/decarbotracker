from conftest import make_item, make_scored

from decarbotracker.models import ItemScore
from decarbotracker.scoring import final_score, heuristic_score, score_items, select_for_synthesis


def _score(**kw) -> ItemScore:
    base = dict(item_id="x", relevance=8, topics=["study"], geo_focus="CZ",
                is_public_attitudes_or_communication=False, is_original_research=False, relevant_to_cz_eu=False,
                is_opinion=False, title_cs="", one_line_cs="", sentiment=0)
    base.update(kw)
    return ItemScore(**base)


def test_final_score_formula(settings):
    sel = settings.selection
    item = make_item(1, source_type="polling")
    item.source_weight = 1.0
    s = _score(relevance=10, geo_focus="CZ")
    assert final_score(item, s, sel) == round(10 * 1.0 * sel.source_type_weights["polling"], 4)
    s2 = _score(relevance=10, geo_focus="CZ", is_public_attitudes_or_communication=True, is_original_research=True)
    expected = 10 * sel.source_type_weights["polling"] * 1.30 * 1.15
    assert abs(final_score(item, s2, sel) - expected) < 1e-3


def test_geo_weights_and_global_bonus(settings):
    sel = settings.selection
    item = make_item(1, source_type="government")
    us = final_score(item, _score(geo_focus="US"), sel)
    eu = final_score(item, _score(geo_focus="EU"), sel)
    cz = final_score(item, _score(geo_focus="CZ"), sel)
    glob = final_score(item, _score(geo_focus="GLOBAL"), sel)
    glob_rel = final_score(item, _score(geo_focus="GLOBAL", relevant_to_cz_eu=True), sel)
    assert cz > eu > glob > us
    assert glob_rel > glob


def test_quotas_cz_and_attitudes(settings):
    sel = settings.selection
    sel.max_items_synthesis = 10
    strong = [make_scored(make_item(i, region="US"), relevance=9, final=50 - i) for i in range(20)]
    cz = [make_scored(make_item(100 + i, region="CZ"), relevance=5, final=5 - i * 0.1) for i in range(6)]
    att = [make_scored(make_item(200 + i, region="EU"), relevance=5, attitudes=True, final=4 - i * 0.1) for i in range(6)]
    chosen = select_for_synthesis(strong + cz + att, sel)
    assert sum(1 for s in chosen if s.score.geo_focus == "CZ") >= 5
    assert sum(1 for s in chosen if s.score.is_public_attitudes_or_communication) >= 5
    # CZ položky jsou na začátku
    first_non_cz = next(i for i, s in enumerate(chosen) if s.score.geo_focus != "CZ")
    assert all(s.score.geo_focus != "CZ" for s in chosen[first_non_cz:])


def test_quota_not_enforced_when_missing(settings):
    sel = settings.selection
    items = [make_scored(make_item(i, region="EU"), relevance=6, final=10 - i) for i in range(8)]
    chosen = select_for_synthesis(items, sel)
    assert len(chosen) == 8
    assert all(s.score.geo_focus == "EU" for s in chosen)


def test_low_relevance_excluded(settings):
    items = [make_scored(make_item(1), relevance=1), make_scored(make_item(2), relevance=6)]
    assert [s.item.id for s in select_for_synthesis(items, settings.selection)] == [items[1].item.id]


def test_heuristic_is_deterministic_and_detects_czech():
    it = make_item(1, title="Průzkum: Češi a postoje k emisním povolenkám", region="EU")
    a, b = heuristic_score(it), heuristic_score(it)
    assert a == b
    assert a.geo_focus == "CZ"
    assert a.is_public_attitudes_or_communication
    assert 0 <= a.relevance <= 10


def test_score_items_without_client_uses_heuristic(settings):
    items = [make_item(i) for i in range(5)]
    scored = score_items(items, settings, None)
    assert len(scored) == 5
    assert all(s.scored_by == "heuristic" for s in scored)
    assert scored == sorted(scored, key=lambda s: -s.final_score)
