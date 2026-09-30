from datetime import UTC, datetime

from conftest import make_item

from decarbotracker.dedup import SUPPRESSED, SeenState, deduplicate, select_window


def test_dedup_by_url_variants():
    a = make_item(1, url="https://example.org/clanek/?utm_source=rss")
    b = make_item(2, url="http://example.org/clanek", title="Úplně jiný titulek o něčem jiném")
    out = deduplicate([a, b])
    assert len(out) == 1


def test_dedup_by_doi():
    a = make_item(1, url="https://doi.org/10.1/abc", doi="10.1/abc", source_type="journal")
    b = make_item(2, url="https://www.sciencedirect.com/x", doi="10.1/abc", title="Jiný titulek téhož článku")
    assert len(deduplicate([a, b])) == 1


def test_fuzzy_title_prefers_primary_source():
    media = make_item(1, title="Ember: Solar overtakes coal in EU electricity for the first time",
                      source_type="media", url="https://news.example/ember-solar")
    primary = make_item(2, title="Solar overtakes coal in EU electricity for the first time",
                        source_type="think_tank", url="https://ember.example/solar")
    other = make_item(3, title="Heat pumps sales fall in Germany amid subsidy confusion", url="https://x.example/hp")
    out = deduplicate([media, primary, other], threshold=90)
    assert len(out) == 2
    assert primary in out and media not in out


def test_different_titles_not_merged():
    a = make_item(1, title="Průzkum: Češi podporují fotovoltaiku na střechách")
    b = make_item(2, title="Průzkum: Češi odmítají větrné elektrárny v okolí obce")
    assert len(deduplicate([a, b])) == 2


def test_window_and_seen():
    week = "2026-W39"  # 21.–27. 9. 2026
    inside = make_item(1, published=datetime(2026, 9, 23, tzinfo=UTC))
    before = make_item(2, published=datetime(2026, 9, 20, 23, 59, tzinfo=UTC))
    after = make_item(3, published=datetime(2026, 9, 28, 0, 1, tzinfo=UTC))
    old_seen = make_item(4, published=datetime(2026, 9, 24, tzinfo=UTC))
    seen = SeenState({old_seen.id: "2026-W38"})
    got, suppressed = select_window([inside, before, after, old_seen], week, seen)
    assert got == [inside]
    assert suppressed == []


def test_rerun_same_week_is_idempotent():
    week = "2026-W39"
    item = make_item(1, published=datetime(2026, 9, 23, tzinfo=UTC))
    seen = SeenState({item.id: week})
    got, _ = select_window([item], week, seen)
    assert got == [item]


def test_undated_first_run_cap_and_suppression():
    week = "2026-W39"
    undated = [make_item(i, published=None, source_id="duha") for i in range(6)]
    for it in undated:
        it.first_seen = datetime(2026, 9, 28, 6, tzinfo=UTC)  # pondělní běh
    seen = SeenState()
    got, suppressed = select_window(undated, week, seen, undated_first_run_cap=3)
    assert len(got) == 3 and len(suppressed) == 3
    # po uložení: potlačené se už nevrátí, ani když je zdroj inicializovaný
    seen.mark(got, week)
    for s in suppressed:
        seen.items[s.id] = SUPPRESSED
    seen.sources.add("duha")
    got2, _ = select_window(undated, "2026-W40", seen)
    assert got2 == []


def test_prune_keeps_suppressed():
    seen = SeenState({"a": "2025-W01", "b": "2026-W38", "c": SUPPRESSED})
    seen.prune("2026-W39", keep_weeks=30)
    assert set(seen.items) == {"b", "c"}
