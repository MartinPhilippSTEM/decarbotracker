from conftest import NOW, load_json_fixture

from decarbotracker.config import JournalSettings
from decarbotracker.fetch.academic import parse_crossref_work, parse_openalex_work, reconstruct_abstract


def test_reconstruct_abstract():
    inv = {"Public": [0], "support": [1, 5], "for": [2], "carbon": [3], "taxes.": [4]}
    assert reconstruct_abstract(inv) == "Public support for carbon taxes. support"
    assert reconstruct_abstract(None) == ""
    assert reconstruct_abstract({}) == ""
    assert reconstruct_abstract({"slovo": None}) == ""


def test_parse_openalex_work_with_known_journal():
    work = {
        "id": "https://openalex.org/W1",
        "doi": "https://doi.org/10.1016/j.gloenvcha.2026.1",
        "title": "Public attitudes to ETS2 in Central Europe",
        "publication_date": "2026-09-22",
        "primary_location": {"landing_page_url": "https://www.sciencedirect.com/x",
                             "source": {"display_name": "Global Environmental Change", "issn": ["0959-3780"]}},
        "authorships": [{"author": {"display_name": "Jana Nováková"}}],
        "abstract_inverted_index": {"We": [0], "survey": [1], "Czechia.": [2]},
        "language": "en",
    }
    journals = {"0959-3780": JournalSettings(name="Global Environmental Change", issn=["0959-3780"])}
    it = parse_openalex_work(work, journals, now=NOW)
    assert it.source_type == "journal"
    assert it.source_name == "Global Environmental Change"
    assert it.doi == "10.1016/j.gloenvcha.2026.1"
    assert it.summary_raw == "We survey Czechia."
    assert it.authors == ["Jana Nováková"]
    assert it.topic_filter is False


def test_parse_openalex_missing_abstract_and_venue():
    it = parse_openalex_work({"id": "https://openalex.org/W2", "title": "Energy poverty in Poland",
                              "doi": None, "publication_date": "2026-09-21",
                              "abstract_inverted_index": None, "primary_location": None}, {}, now=NOW)
    assert it is not None and it.summary_raw == "" and it.url == "https://openalex.org/W2"


def test_parse_crossref_real_fixture():
    data = load_json_fixture("feeds", "crossref_erss.json")
    journal = JournalSettings(name="Energy Research & Social Science", issn=["2214-6296"])
    items = [parse_crossref_work(w, journal, now=NOW) for w in data["message"]["items"]]
    items = [i for i in items if i]
    assert len(items) >= 1
    for it in items:
        assert it.doi and it.doi.startswith("10.")
        assert it.published_at is not None  # z pole "created"
        assert it.source_name == "Energy Research & Social Science"
        assert "<jats" not in it.summary_raw
