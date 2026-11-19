"""Pozvánky/akce nesmí skončit mezi zjištěními (případ EUROPEUM, W43 a W46) a každá zpráva je
v hlavní části jen jednou (případ „662 projektů“ 7× na stránce, W46)."""

from conftest import make_item, make_scored

from decarbotracker.models import (
    ItemScore,
    KeyFinding,
    NewsItem,
    PublicAttitudes,
    Recommendation,
    ReportDraft,
    Swot,
    SwotPoint,
    WatchItem,
)
from decarbotracker.scoring import _sanitize, final_score, looks_like_event
from decarbotracker.synthesis import (
    enforce_single_place,
    main_section_ids,
    mock_draft,
    move_events_out,
    postprocess,
    synthesize,
)

EUROPEUM_TITLE = "Veřejná debata | Spravedlivá transformace v Karlovarském kraji"
TEXT = "Výtah s konkrétním obsahem článku."


def test_title_patterns():
    assert looks_like_event(make_item(1, title=EUROPEUM_TITLE))
    assert looks_like_event(make_item(2, title="Webinář: ETS2 a domácnosti"))
    assert looks_like_event(make_item(3, title="Konference Energetika 2026 – pozvánka"))
    assert not looks_like_event(make_item(4, title="Summit EU se dohodl na klimatickém cíli pro rok 2040"))
    assert not looks_like_event(make_item(5, title="Průzkum: Češi podporují úspory energie"))


def _score(item, **kw):
    base = dict(item_id=item.id, relevance=8, topics=["public_attitudes", "just_transition"], geo_focus="CZ",
                is_public_attitudes_or_communication=True, is_original_research=True, relevant_to_cz_eu=True,
                cz_impact=3, eu_impact=1, is_opinion=False, is_event=False, title_cs="", one_line_cs="", sentiment=0)
    base.update(kw)
    return ItemScore(**base)


def test_event_detected_by_title_even_if_ai_missed_it_and_loses_bonuses(settings):
    item = make_item(1, title=EUROPEUM_TITLE, region="CZ", source_type="think_tank")
    missed = _sanitize(_score(item, is_event=False), item)  # AI akci nepoznala
    assert missed.is_event and not missed.is_public_attitudes_or_communication and not missed.is_original_research
    normal = _score(make_item(2, region="CZ", source_type="think_tank"))
    assert final_score(item, missed, settings.selection) < final_score(item, normal, settings.selection) - 5


def _sel_with_event():
    sel = [make_scored(make_item(i, region="CZ" if i < 6 else "EU", summary=TEXT, source_id=f"s{i}"), relevance=7)
           for i in range(12)]
    ev = make_scored(make_item(99, title=EUROPEUM_TITLE, region="CZ", source_id="europeum", summary=TEXT),
                     relevance=7, is_event=True)
    return sel, ev


def test_event_removed_from_findings_news_swot_and_attitudes(settings):
    sel, ev = _sel_with_event()
    sel.insert(0, ev)
    draft = mock_draft(sel)
    draft.key_findings.append(KeyFinding(item_id=ev.item.id, title_cs="x", text_cs="x"))
    draft.swot.strengths.append(SwotPoint(text_cs="42 mld. Kč", evidence_item_ids=[ev.item.id], geo="CZ"))
    draft.public_attitudes_cs.communication_recommendations.append(
        Recommendation(text_cs="x", evidence_item_ids=[ev.item.id]))
    logs: list[str] = []
    draft = move_events_out(draft, sel, logs)
    ids_everywhere = ([f.item_id for f in draft.key_findings] + [n.item_id for n in draft.cz_items + draft.world_items]
                      + [i for q in ("strengths", "weaknesses", "opportunities", "threats")
                         for p in getattr(draft.swot, q) for i in p.evidence_item_ids]
                      + [i for r in draft.public_attitudes_cs.communication_recommendations for i in r.evidence_item_ids])
    assert ev.item.id not in ids_everywhere
    assert any(ev.item.id in w.evidence_item_ids for w in draft.watchlist_cs)
    assert logs


def _news(i):
    return NewsItem(item_id=i, title_cs="t", text_cs="x", key_figure="")


def test_one_item_one_place_in_main_part():
    ids = [f"id{i}" for i in range(6)]
    draft = ReportDraft(
        headline_cs="h",
        key_findings=[KeyFinding(item_id=ids[0], title_cs="t", text_cs="x")],  # zjištění smí odkazovat na zprávu
        cz_items=[_news(ids[0]), _news(ids[1])],
        world_items=[_news(ids[1]), _news(ids[2])],  # ids[1] už je v Česku
        public_attitudes_cs=PublicAttitudes(summary_cs="s", surveys=[], communication_recommendations=[
            Recommendation(text_cs="opakuje", evidence_item_ids=[ids[2]]),
            Recommendation(text_cs="nové", evidence_item_ids=[ids[3]])]),
        watchlist_cs=[WatchItem(when="", text_cs="opakuje 662 projektů", evidence_item_ids=[ids[0]]),
                      WatchItem(when="1. 1. 2027", text_cs="termín k již uvedené zprávě", evidence_item_ids=[ids[1]]),
                      WatchItem(when="", text_cs="nový trend", evidence_item_ids=[ids[4]])],
        swot=Swot(strengths=[SwotPoint(text_cs="s", evidence_item_ids=[ids[0]], geo="CZ")], weaknesses=[],
                  opportunities=[], threats=[]),
        data_gaps_cs=[],
    )
    logs: list[str] = []
    d = enforce_single_place(draft, logs)
    used = main_section_ids(d)
    assert len(used) == len(set(used))
    assert [n.item_id for n in d.world_items] == [ids[2]]
    assert [r.text_cs for r in d.public_attitudes_cs.communication_recommendations] == ["nové"]
    assert [w.text_cs for w in d.watchlist_cs] == ["termín k již uvedené zprávě", "nový trend"]
    assert d.swot.strengths  # SWOT (sbalená část) smí položky z hlavní části použít
    assert logs


def test_findings_never_from_title_only_items(settings):
    sel = [make_scored(make_item(i, region="CZ" if i < 4 else "EU", summary=TEXT if i % 2 else "",
                                 source_id=f"s{i}"), relevance=7) for i in range(10)]
    draft = mock_draft(sel)
    draft.key_findings = [KeyFinding(item_id=sel[0].item.id, title_cs="t", text_cs="x")]  # bez výtahu
    d = postprocess(draft, sel, [])
    no_summary = {s.item.id for s in sel if not s.item.summary_raw}
    assert d.key_findings and all(f.item_id not in no_summary for f in d.key_findings)


def test_full_synthesis_renders_event_only_in_watchlist(settings, tmp_path):
    from decarbotracker.render import build_site
    from decarbotracker.storage import week_path, write_json

    sel, ev = _sel_with_event()
    sel.insert(0, ev)
    rep = synthesize("2026-W48", sel, 40, settings, None)
    shown = [n.item_id for n in rep.cz_items + rep.world_items] + [f.item_id for f in rep.key_findings]
    assert ev.item.id not in shown
    write_json(week_path("2026-W48"), rep)
    html = (build_site(settings, out_dir=tmp_path / "site") / "index.html").read_text(encoding="utf-8")
    main = html[:html.index('id="analytici"')]
    watch = main[main.index('id="sledovat"'):]
    before_watch = main[:main.index('id="sledovat"')]
    assert "Karlovarském kraji" in watch and "Karlovarském kraji" not in before_watch
