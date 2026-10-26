from conftest import make_item, make_scored

from decarbotracker import source_review as sr
from decarbotracker.config import load_sources
from decarbotracker.scoring import select_for_synthesis


def _state(weeks):
    return {"weeks": weeks, "paused": {}, "activated": {}, "flagged": {}}


def test_reserves_not_active_until_activated():
    ids = {s.id for s in sr.active_sources(_state({}))}
    assert "denik-n" not in ids and "seznam-zpravy" in ids
    st = _state({})
    st["activated"]["denik-n"] = {"since": "2026-W43", "replaces": "seznam-zpravy"}
    st["paused"]["seznam-zpravy"] = {"since": "2026-W43", "until": "2026-W47", "replaced_by": "denik-n"}
    ids = {s.id for s in sr.active_sources(st)}
    assert "denik-n" in ids and "seznam-zpravy" not in ids


def test_underperformer_two_weeks_rotated_with_same_region_reserve():
    st = _state({
        "2026-W42": {"seznam-zpravy": [30, 0], "ekolist": [10, 2], "cvvm-ekologie": [3, 0]},
        "2026-W43": {"seznam-zpravy": [25, 0], "ekolist": [12, 0], "cvvm-ekologie": [2, 0]},
    })
    changes = sr.rotate(st, "2026-W43")
    assert "seznam-zpravy" in st["paused"]  # 2 týdny publikoval, nic neprošlo
    assert "ekolist" not in st["paused"]  # v W42 něco prošlo
    assert "cvvm-ekologie" not in st["paused"]  # chráněný (váha 1,5)
    repl = st["paused"]["seznam-zpravy"]["replaced_by"]
    regions = {s.id: s.region for s in load_sources()}
    assert regions[repl] == "CZ" and repl in st["activated"]
    assert st["paused"]["seznam-zpravy"]["until"] == "2026-W47"
    assert changes


def test_not_enough_history_no_rotation():
    st = _state({"2026-W43": {"seznam-zpravy": [25, 0]}})
    assert sr.rotate(st, "2026-W43") == [] and not st["paused"]


def test_paused_source_returns_after_pause():
    st = _state({})
    st["paused"]["e15"] = {"since": "2026-W40", "until": "2026-W44", "replaced_by": "ekonom"}
    sr.rotate(st, "2026-W44")
    assert "e15" not in st["paused"]


def test_record_week_counts_window_and_selected(settings):
    from decarbotracker.synthesis import synthesize

    a = make_item(1, source_id="ekolist")
    b = make_item(2, source_id="ekolist")
    c = make_item(3, source_id="e15")
    sel = [make_scored(a)] + [make_scored(make_item(i, source_id="x")) for i in range(10, 22)]
    rep = synthesize("2026-W43", sel, 20, settings, None)
    st = _state({})
    sr.record_week(st, "2026-W43", [a, b, c], rep, {"ekolist", "e15", "idle"})
    assert st["weeks"]["2026-W43"]["ekolist"] == [2, 1]
    assert st["weeks"]["2026-W43"]["e15"] == [1, 0]
    assert st["weeks"]["2026-W43"]["idle"] == [0, 0]


def test_selection_caps_per_source_and_media(settings):
    sel = settings.selection
    sel.max_items_synthesis, sel.max_per_source, sel.max_media_share = 10, 3, 0.4
    media = [make_scored(make_item(i, source_id="hn", source_type="media"), relevance=9, final=100 - i)
             for i in range(8)]
    media2 = [make_scored(make_item(20 + i, source_id="e15", source_type="media"), relevance=9, final=90 - i)
              for i in range(8)]
    tanks = [make_scored(make_item(40 + i, source_id=f"tt{i}", source_type="think_tank"), relevance=5, final=10 - i)
             for i in range(10)]
    chosen = select_for_synthesis(media + media2 + tanks, sel)
    assert len(chosen) == 10
    assert sum(1 for s in chosen if s.item.source_id == "hn") <= 3
    assert sum(1 for s in chosen if s.item.source_type == "media") <= 4


def test_selection_fills_up_when_caps_leave_gaps(settings):
    sel = settings.selection
    sel.max_items_synthesis, sel.max_per_source = 10, 3
    only = [make_scored(make_item(i, source_id="one", source_type="think_tank"), relevance=7) for i in range(12)]
    assert len(select_for_synthesis(only, sel)) == 10  # pojistky nesmí zmenšit výběr, když nejsou alternativy
