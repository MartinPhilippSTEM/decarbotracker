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


def _weeks(rows_by_source):
    weeks = ["2026-W40", "2026-W41", "2026-W42", "2026-W43"]
    return {w: {sid: rows[i] for sid, rows in rows_by_source.items()} for i, w in enumerate(weeks)}


def test_off_topic_source_rotated_after_four_weeks():
    """Od 23. 11. 2026: pozastavuje se jen zdroj, ze kterého 4 týdny nic neprojde předfiltrem –
    to, že ho AI nevybrala, nestačí."""
    st = _state(_weeks({
        "respekt": [[30, 0, 0]] * 4,                                   # píše mimo téma
        "oenergetice": [[10, 0, 4], [12, 0, 3], [9, 0, 5], [11, 0, 2]],  # k tématu, jen nevybráno
        "seznam-zpravy": [[30, 0, 0]] * 4,                              # chráněný
    }))
    changes = sr.rotate(st, "2026-W43")
    assert "respekt" in st["paused"]
    assert "oenergetice" not in st["paused"]
    assert "seznam-zpravy" not in st["paused"]
    repl = st["paused"]["respekt"]["replaced_by"]
    regions = {s.id: s.region for s in load_sources()}
    assert regions[repl] == "CZ" and repl in st["activated"]
    assert changes


def test_old_two_value_rows_never_rotate():
    st = _state(_weeks({"respekt": [[30, 0]] * 4}))
    assert sr.rotate(st, "2026-W43") == [] and not st["paused"]

def test_not_enough_history_no_rotation():
    st = _state({"2026-W43": {"respekt": [25, 0, 0]}})
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
    sr.record_week(st, "2026-W43", [a, b, c], rep, {"ekolist", "e15", "idle"}, candidates=[a, c])
    assert st["weeks"]["2026-W43"]["ekolist"] == [2, 1, 1]
    assert st["weeks"]["2026-W43"]["e15"] == [1, 0, 1]
    assert st["weeks"]["2026-W43"]["idle"] == [0, 0, 0]


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


def test_already_paused_source_not_rotated_again():
    st = _state(_weeks({"respekt": [[40, 0, 0]] * 4, "sme": [[60, 0, 0]] * 4}))
    st["paused"]["respekt"] = {"since": "2026-W43", "until": "2026-W47", "replaced_by": "iness"}
    st["activated"]["iness"] = {"since": "2026-W43", "replaces": "respekt"}
    sr.rotate(st, "2026-W43")
    assert st["paused"]["respekt"]["replaced_by"] == "iness"  # nezměněno
    assert sum(1 for v in st["activated"].values() if v["replaces"] == "respekt") == 1

def test_journal_share_cap(settings):
    sel = settings.selection
    sel.max_items_synthesis, sel.max_per_source, sel.max_journal_share, sel.min_cz = 10, 3, 0.4, 5
    journals = [make_scored(make_item(i, source_id=f"journal-{i}", source_type="journal"), relevance=9, final=100 - i)
                for i in range(15)]
    others = [make_scored(make_item(50 + i, source_id=f"tt{i}", source_type="think_tank"), relevance=5, final=10 - i)
              for i in range(10)]
    chosen = select_for_synthesis(journals + others, sel)
    assert sum(1 for s in chosen if s.item.source_type == "journal") == 4
