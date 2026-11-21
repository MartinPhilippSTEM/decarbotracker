from datetime import UTC, datetime, timedelta

from conftest import NOW, load_json_fixture, make_item, make_source

from decarbotracker.dedup import SeenState
from decarbotracker.fetch.funding import budget_info, parse_eu_results
from decarbotracker.llm import LLMError, UsageTracker
from decarbotracker.models import OpportunityBatch, OpportunityDraft
from decarbotracker.opportunities import build_pool, find_opportunities, parse_deadline

EU_SOURCE = make_source(id="eu-funding", name="EU Funding & Tenders Portal", type="eu_funding", region="EU",
                        source_type="funding", topic_filter=False, funding=True)


def test_parse_eu_funding_real_fixture():
    items = parse_eu_results(load_json_fixture("feeds", "eu_funding_ssh.json"), EU_SOURCE, now=NOW,
                             require_topic=False)
    assert len(items) == 4
    it = items[0]
    assert it.url.startswith("https://ec.europa.eu/info/funding-tenders/")
    assert "Uzávěrka:" in it.summary_raw and "Otevření:" in it.summary_raw
    assert "HORIZON-" in it.title


def test_budget_info_extracts_min_max():
    data = load_json_fixture("feeds", "eu_funding_ssh.json")
    md = data["results"][0]["metadata"]
    b = budget_info(md, md["identifier"][0])
    assert b.get("min", "").endswith("EUR") and b.get("max", "").endswith("EUR")


def test_pool_contains_funding_sources_and_call_titles_only():
    call = make_item(1, title="TA ČR vyhlašuje veřejnou soutěž programu SIGMA k energetické transformaci", region="CZ", source_id="tacr")
    news = make_item(2, title="Ceny elektřiny rostou", region="CZ", source_id="oenergetice")
    titled = make_item(3, title="Nová výzva Modernizačního fondu", region="CZ", source_id="mzp")
    old = make_item(4, title="Výzva z loňska", region="CZ", source_id="tacr", published=NOW - timedelta(days=90))
    eu = make_item(5, title="Societal readiness of energy transition (HORIZON-X)", source_id="eu-funding",
                   published=NOW - timedelta(days=200))
    eu.source_type = "funding"
    pool = build_pool([call, news, titled, old, eu], {"tacr", "eu-funding"}, SeenState(), NOW, 30, 50)
    ids = [i.id for i in pool]
    assert call.id in ids and titled.id in ids and eu.id in ids
    assert news.id not in ids and old.id not in ids
    assert ids.index(call.id) < ids.index(eu.id)  # české první


def test_pool_puts_climate_calls_first():
    social = make_item(1, title="Výzva OPZ+ na sociální podnikání", region="CZ", source_id="opz-plus")
    climate = make_item(2, title="Výzva na komunitní energetiku v uhelných regionech", region="CZ", source_id="opst")
    eu = make_item(3, title="Citizens in the energy transition (HORIZON-X)", source_id="eu-funding")
    eu.source_type = "funding"
    ids = [i.id for i in build_pool([social, climate, eu], {"opz-plus", "opst", "eu-funding"}, SeenState(),
                                    NOW, 30, 50)]
    assert ids == [climate.id, eu.id, social.id]


def test_pool_skips_opportunities_shown_in_other_week():
    it = make_item(1, title="Výzva X", source_id="tacr")
    seen = SeenState(opportunities={it.id: "2026-W38"})
    assert build_pool([it], {"tacr"}, seen, NOW, 30, 50, week="2026-W39") == []
    seen_same = SeenState(opportunities={it.id: "2026-W39"})
    assert len(build_pool([it], {"tacr"}, seen_same, NOW, 30, 50, week="2026-W39")) == 1


class FakeClient:
    def __init__(self, settings, result):
        self.tracker = UsageTracker(settings.llm)
        self.result = result

    def structured(self, **kw):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result, None


def _draft(item_id, title="Výzva"):
    return OpportunityDraft(item_id=item_id, title_cs=title, funder="TA ČR", amount_min="1 mil. Kč",
                            amount_max="5 mil. Kč", open_from="1. 10. 2026", deadline="15. 12. 2026",
                            summary_cs="Podpora společenskovědního výzkumu.",
                            stem_role_cs="STEM jako partner: reprezentativní šetření v ČR.")


def test_find_opportunities_validates_ids_and_copies_links(settings):
    it = make_item(1, title="Veřejná soutěž TA ČR", source_id="tacr", region="CZ")
    client = FakeClient(settings, OpportunityBatch(opportunities=[_draft(it.id), _draft("neexistuje")]))
    opps, logs = find_opportunities([it], settings, client, SeenState(), {"tacr"}, NOW, "2026-W40")
    assert len(opps) == 1 and opps[0].url == it.url and opps[0].source_name == it.source_name
    assert any("neexistující" in line for line in logs)


def test_find_opportunities_error_does_not_crash(settings):
    it = make_item(1, title="Výzva", source_id="tacr")
    opps, logs = find_opportunities([it], settings, FakeClient(settings, LLMError("boom")), SeenState(),
                                    {"tacr"}, NOW, "2026-W40")
    assert opps == [] and logs


def test_find_opportunities_dry_run(settings):
    it = make_item(1, title="Grantová výzva", source_id="tacr", published=datetime(2026, 9, 25, tzinfo=UTC))
    opps, _ = find_opportunities([it], settings, None, SeenState(), {"tacr"}, NOW, "2026-W40")
    assert len(opps) == 1 and opps[0].deadline == "neuvedeno"


def test_parse_deadline_formats():
    from datetime import date

    assert parse_deadline("15. 12. 2026") == date(2026, 12, 15)
    assert parse_deadline("2027-03-04T17:00:00") == date(2027, 3, 4)
    assert parse_deadline("31. března 2027") == date(2027, 3, 31)
    assert parse_deadline("březen 2027") == date(2027, 3, 31)
    assert parse_deadline("neuvedeno") is None


def _eu(n, deadline, prog="HORIZON"):
    it = make_item(n, title=f"Výzva {n} ({prog}-CL5-2027-01-D{n})", source_id="eu-funding",
                   summary=f"Výzva EU | Uzávěrka: {deadline} | Štítek SSH (společenské a humanitní vědy)")
    it.source_type = "funding"
    return it


def test_pool_drops_calls_outside_six_month_window():
    """Uzávěrka později než 6 měsíců od konce týdne (nebo už prošlá) → výzva se nezobrazí."""
    soon, far, past = _eu(1, "15. 1. 2027"), _eu(2, "23. 9. 2027"), _eu(3, "1. 9. 2026")
    pool = build_pool([soon, far, past], {"eu-funding"}, SeenState(), NOW, 30, 50, "2026-W40", NOW)
    assert [i.id for i in pool] == [soon.id]


def test_shown_call_returns_as_reminder_only_near_deadline():
    near, later = _eu(1, "20. 10. 2026"), _eu(2, "15. 2. 2027")
    seen = SeenState(opportunities={near.id: "2026-W38", later.id: "2026-W38"})
    pool = build_pool([near, later], {"eu-funding"}, seen, NOW, 30, 50, "2026-W40", NOW)
    assert [i.id for i in pool] == [near.id]


def test_finalize_caps_programme_and_window(settings):
    calls = [_eu(i, "15. 1. 2027") for i in range(1, 6)] + [_eu(9, "15. 1. 2027", prog="LIFE")]
    late = _eu(20, "15. 1. 2027")
    drafts = [_draft(c.id) for c in calls] + [_draft(late.id)]
    drafts[-1].deadline = "30. 11. 2027"  # AI uvedla uzávěrku mimo okno
    for d in drafts[:-1]:
        d.deadline = "15. 1. 2027"
    client = FakeClient(settings, OpportunityBatch(opportunities=drafts))
    opps, logs = find_opportunities(calls + [late], settings, client, SeenState(), {"eu-funding"}, NOW, "2026-W40")
    progs = [o.title.split("(")[1].split("-")[0] for o in opps]
    assert progs.count("HORIZON") == 3 and "LIFE" in progs
    assert all(o.item_id != late.id for o in opps)
    assert all(o.stem_role_cs for o in opps)
    assert any("mimo okno" in line for line in logs) and any("z programu" in line for line in logs)
