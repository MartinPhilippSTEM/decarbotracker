"""Pozvánky/akce nesmí skončit mezi nejdůležitějšími položkami (případ EUROPEUM, týden 2026-W43)."""

from conftest import make_item, make_scored

from decarbotracker.models import ItemScore
from decarbotracker.scoring import _sanitize, final_score, looks_like_event
from decarbotracker.synthesis import move_events_out_of_top, synthesize

EUROPEUM_TITLE = "Veřejná debata | Spravedlivá transformace v Karlovarském kraji"


def test_title_patterns():
    assert looks_like_event(make_item(1, title=EUROPEUM_TITLE))
    assert looks_like_event(make_item(2, title="Webinář: ETS2 a domácnosti"))
    assert looks_like_event(make_item(3, title="Konference Energetika 2026 – pozvánka"))
    assert not looks_like_event(make_item(4, title="Summit EU se dohodl na klimatickém cíli pro rok 2040"))
    assert not looks_like_event(make_item(5, title="Průzkum: Češi podporují úspory energie"))


def _score(item, **kw):
    base = dict(item_id=item.id, relevance=8, topics=["public_attitudes", "just_transition"], geo_focus="CZ",
                is_public_attitudes_or_communication=True, is_original_research=True, relevant_to_cz_eu=True,
                is_opinion=False, is_event=False, title_cs="", one_line_cs="", sentiment=0)
    base.update(kw)
    return ItemScore(**base)


def test_event_detected_by_title_even_if_ai_missed_it_and_loses_bonuses(settings):
    item = make_item(1, title=EUROPEUM_TITLE, region="CZ", source_type="think_tank")
    missed = _sanitize(_score(item, is_event=False), item)  # AI akci nepoznala
    assert missed.is_event and not missed.is_public_attitudes_or_communication and not missed.is_original_research
    normal = _score(make_item(2, region="CZ", source_type="think_tank"))
    assert final_score(item, missed, settings.selection) < final_score(item, normal, settings.selection) * 0.5


def test_event_moved_from_top_items_to_events(settings):
    sel = [make_scored(make_item(i, region="CZ"), relevance=7) for i in range(12)]
    ev = make_scored(make_item(99, title=EUROPEUM_TITLE, region="CZ", source_id="europeum"), relevance=7, is_event=True)
    sel.insert(3, ev)
    rep_draft_logs: list[str] = []
    from decarbotracker.synthesis import mock_draft

    draft = mock_draft(sel)  # mock dává prvních 10 položek do top_items, včetně akce
    assert any(t.item_id == ev.item.id for t in draft.top_items)
    draft = move_events_out_of_top(draft, sel, rep_draft_logs)
    assert all(t.item_id != ev.item.id for t in draft.top_items)
    assert any(ev.item.id in w.evidence_item_ids and w.when == "termín ve zdroji" for w in draft.watchlist_cs)
    assert rep_draft_logs


def test_full_synthesis_keeps_event_out_of_top_and_shows_it_in_events(settings, tmp_path):
    from decarbotracker.render import build_site
    from decarbotracker.storage import week_path, write_json

    sel = [make_scored(make_item(i, region="CZ"), relevance=7) for i in range(12)]
    sel.insert(0, make_scored(make_item(99, title=EUROPEUM_TITLE, region="CZ"), relevance=7, is_event=True))
    rep = synthesize("2026-W43", sel, 40, settings, None)
    assert all(not next(i for i in rep.items if i.id == t.item_id).is_event for t in rep.top_items)
    write_json(week_path("2026-W43"), rep)
    html = (build_site(settings, out_dir=tmp_path / "site") / "index.html").read_text(encoding="utf-8")
    events_part = html[html.index("Události a termíny"):html.index('id="swot"')]
    top5_part = html[html.index("Pět nejdůležitějších"):html.index("Události a termíny")]
    assert "Karlovarském kraji" in events_part and "Karlovarském kraji" not in top5_part
