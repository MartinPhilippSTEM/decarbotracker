from datetime import UTC, datetime

from conftest import make_item, make_scored

from decarbotracker import media
from decarbotracker.llm import UsageTracker
from decarbotracker.models import MediaClass, MediaClassBatch, MediaDraft, MediaTheme

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)  # středa W44
WEEK = "2026-W44"


def _art(n, title, outlet="Novinky.cz", day=27):
    it = make_item(n, title=title, region="CZ", source_type="media", source_id=outlet.lower(),
                   published=datetime(2026, 10, day, 9, 0, tzinfo=UTC), summary="Perex.")
    it.source_name = outlet
    return it


def test_add_to_store_dedups_and_keeps_first_seen():
    a = _art(1, "Ceny elektřiny porostou")
    media.add_to_store([a], NOW)
    a2 = a.model_copy(update={"first_seen": NOW.replace(hour=20)})
    added = media.add_to_store([a2, _art(2, "Fotbal: Sparta vyhrála")], NOW)
    store, meta = media.load_store(WEEK)
    assert added[WEEK] == 1 and len(store) == 2
    assert store[a.id].first_seen == a.first_seen
    assert meta["collections"] == 2


def test_add_to_store_ignores_old_weeks():
    old = _art(3, "Stará zpráva o uhlí", day=1)  # 1. 10. = W40
    assert media.add_to_store([old], NOW) == {}


class FakeClient:
    def __init__(self, settings, classes, draft=None):
        self.tracker = UsageTracker(settings.llm)
        self.classes, self.draft, self.calls = classes, draft, []

    def structured(self, **kw):
        self.calls.append(kw["schema"].__name__)
        if kw["schema"] is MediaClassBatch:
            return MediaClassBatch(items=self.classes), None
        return self.draft, None


def _setup_store():
    arts = [_art(1, "Ceny elektřiny porostou, domácnosti zaplatí víc", "Novinky.cz"),
            _art(2, "Vláda schválila podporu fotovoltaiky", "iDNES.cz"),
            _art(3, "Komentář: Zelené šílenství z Bruselu", "Parlamentní listy"),
            _art(4, "Fotbal: Sparta vyhrála derby", "Novinky.cz"),          # nepropadne klíčovými slovy
            _art(5, "Firemní klima a motivace zaměstnanců", "iDNES.cz")]    # propadne, ale AI ho vyřadí
    media.add_to_store(arts, NOW)
    return arts


def test_analyze_counts_by_code_and_validates_draft(settings):
    arts = _setup_store()
    classes = [
        MediaClass(item_id=arts[0].id, relevant=True, topic="ceny_energii", frame="naklady_a_zatez", genre="zprava", tone=-1),
        MediaClass(item_id=arts[1].id, relevant=True, topic="obnovitelne_zdroje", frame="ekonomicka_prilezitost", genre="zprava", tone=1),
        MediaClass(item_id=arts[2].id, relevant=True, topic="klimaticka_politika", frame="skepse_a_odmitani", genre="komentar", tone=-2),
        MediaClass(item_id=arts[4].id, relevant=False, topic="jine", frame="neutralni_informovani", genre="jine", tone=0),
    ]
    draft = MediaDraft(headline_cs="H", summary_cs="S", framing_cs="F", outlets_cs="O",
                       themes=[MediaTheme(title_cs="Ceny", text_cs="t", evidence_item_ids=[arts[0].id, "vymysleno"]),
                               MediaTheme(title_cs="Bez dokladu", text_cs="t", evidence_item_ids=["vymysleno"])],
                       notable=[])
    settings.media.min_relevant = 2
    client = FakeClient(settings, classes, draft)
    rep = media.analyze(WEEK, settings, client)
    assert rep.n_collected == 5 and rep.n_candidates == 4 and rep.n_relevant == 3
    assert rep.topics == {"ceny_energii": 1, "obnovitelne_zdroje": 1, "klimaticka_politika": 1}
    assert rep.genres["komentar"] == 1
    assert rep.tone_index == round((-1 + 1 - 2) / 3 * 50)
    outlets = {o.outlet: (o.relevant, o.collected) for o in rep.outlets}
    assert outlets["Novinky.cz"] == (1, 2) and outlets["iDNES.cz"] == (1, 2)
    assert [t.title_cs for t in rep.draft.themes] == ["Ceny"]
    assert rep.draft.themes[0].evidence_item_ids == [arts[0].id]
    assert client.calls == ["MediaClassBatch", "MediaDraft"]


def test_analyze_missing_classification_is_noted(settings):
    arts = _setup_store()
    classes = [MediaClass(item_id=arts[0].id, relevant=True, topic="ceny_energii", frame="naklady_a_zatez",
                          genre="zprava", tone=0)]
    rep = media.analyze(WEEK, settings, FakeClient(settings, classes))
    assert rep.n_relevant == 1 and rep.draft is None  # pod min_relevant se komentář nepíše
    assert any("bez klasifikace" in n for n in rep.notes)


def test_analyze_without_store_returns_none(settings):
    assert media.analyze("2026-W30", settings, None) is None


def test_media_section_rendered(settings, tmp_path):
    from decarbotracker.render import build_site
    from decarbotracker.storage import week_path, write_json
    from decarbotracker.synthesis import synthesize

    arts = _setup_store()
    rep = synthesize(WEEK, [make_scored(make_item(i, region="CZ"), relevance=7) for i in range(12)], 20, settings, None)
    rep.media = media.analyze(WEEK, settings, None)  # dry-run: čísla bez komentáře
    write_json(week_path(WEEK), rep)
    html = (build_site(settings, out_dir=tmp_path / "site") / "index.html").read_text(encoding="utf-8")
    assert "Co psala česká média" in html and "sběr každou hodinu" in html
    assert "Kolik prostoru téma dostalo" in html
    assert arts[0].source_name in html
