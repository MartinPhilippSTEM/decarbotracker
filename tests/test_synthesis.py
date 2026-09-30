import pytest
from conftest import make_item, make_scored

from decarbotracker.llm import LLMMaxTokens, LLMRefusal, LLMValidationError, UsageTracker
from decarbotracker.models import (
    ByRegion,
    PublicAttitudes,
    Recommendation,
    ReportDraft,
    Swot,
    SwotPoint,
    TopItem,
    WatchItem,
    WeeklyReport,
)
from decarbotracker.synthesis import HardValidationError, mock_draft, synthesize, validate_draft

WEEK = "2026-W39"


def _selected(n=12):
    out = []
    for i in range(n):
        region = "CZ" if i < 3 else "EU"
        out.append(make_scored(make_item(i, region=region), relevance=7, attitudes=i % 4 == 0))
    return out


def _draft(ids, bad="neexistuje123"):
    def pts(prefix):
        return [SwotPoint(text_cs=f"{prefix} {k}", evidence_item_ids=[ids[k]], geo="EU") for k in range(3)]

    sw = Swot(strengths=pts("S"), weaknesses=pts("W"), opportunities=pts("O"), threats=pts("T"))
    sw.strengths.append(SwotPoint(text_cs="Vymyšlený bod", evidence_item_ids=[bad], geo="CZ"))
    sw.threats[0].evidence_item_ids.append(bad)
    return ReportDraft(
        headline_cs="Hlavní poselství.", executive_summary_cs="Shrnutí.",
        swot=sw,
        top_items=[TopItem(item_id=i, why_it_matters_cs="Proto.", key_finding_cs="Zjištění.", category="study")
                   for i in ids[:9]] + [TopItem(item_id=bad, why_it_matters_cs="x", key_finding_cs="x", category="study")],
        public_attitudes_cs=PublicAttitudes(summary_cs="Nic nového.", surveys=[], communication_recommendations=[
            Recommendation(text_cs="Mluvte o úsporách.", evidence_item_ids=[ids[0]]),
            Recommendation(text_cs="Bez doložení.", evidence_item_ids=[bad]),
        ]),
        forecasts_cs=[], by_region=ByRegion(cz="CZ text", eu="EU", us="US", **{"global": ""}),
        watchlist_cs=[WatchItem(when="6. 10. 2026", text_cs="Zveřejnění dat", evidence_item_ids=[ids[1]]),
                      WatchItem(when="", text_cs="Vymyšlená akce", evidence_item_ids=[bad])],
        data_gaps_cs=[],
    )


def test_validate_removes_invalid_evidence_ids():
    sel = _selected()
    ids = [s.item.id for s in sel]
    geo = {s.item.id: s.score.geo_focus for s in sel}
    draft, logs = validate_draft(_draft(ids), set(ids), geo)
    assert all(t != "Vymyšlený bod" for t in (p.text_cs for p in draft.swot.strengths))
    assert draft.swot.threats[0].evidence_item_ids == [ids[0]]
    assert all(t.item_id in ids for t in draft.top_items)
    assert len(draft.public_attitudes_cs.communication_recommendations) == 1
    assert [e.text_cs for e in draft.watchlist_cs] == ["Zveřejnění dat"]
    assert any("neplatná ID" in line or "bez platného doložení" in line for line in logs)
    # CZ položky v top_items první
    assert geo[draft.top_items[0].item_id] == "CZ"


def test_validate_hard_error_when_quadrant_empty():
    sel = _selected()
    ids = [s.item.id for s in sel]
    d = _draft(ids)
    for p in d.swot.opportunities:
        p.evidence_item_ids = ["nic"]
    with pytest.raises(HardValidationError):
        validate_draft(d, set(ids), {})


def test_mock_draft_passes_validation():
    sel = _selected()
    ids = {s.item.id for s in sel}
    draft, _ = validate_draft(mock_draft(sel), ids, {s.item.id: s.score.geo_focus for s in sel})
    assert draft.swot.strengths and draft.top_items


class FakeClient:
    """Napodobí ClaudeClient bez sítě: vrací/vyhazuje připravené odpovědi."""

    def __init__(self, settings, responses):
        self.tracker = UsageTracker(settings.llm)
        self.responses = list(responses)
        self.calls = []

    def count_tokens(self, **kw):
        return 20000

    def structured(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, None


def test_synthesize_ok_with_dry_run(settings):
    rep = synthesize(WEEK, _selected(), 50, settings, None)
    assert rep.status == "dry_run" and rep.swot is not None
    WeeklyReport.model_validate(rep.model_dump(mode="json", by_alias=True))


def test_synthesize_retries_on_validation_then_succeeds(settings):
    sel = _selected()
    ids = [s.item.id for s in sel]
    client = FakeClient(settings, [LLMValidationError("špatné", "{}"), _draft(ids)])
    rep = synthesize(WEEK, sel, 50, settings, client)
    assert rep.status == "ok"
    assert len(client.calls) == 2
    assert "Oprava předchozího pokusu" in client.calls[1]["user"]


def test_synthesize_max_tokens_raises_limit(settings):
    sel = _selected()
    ids = [s.item.id for s in sel]
    client = FakeClient(settings, [LLMMaxTokens("cut"), _draft(ids)])
    rep = synthesize(WEEK, sel, 50, settings, client)
    assert rep.status == "ok"
    assert client.calls[1]["max_tokens"] == settings.llm.synthesis_max_tokens_retry


def test_synthesize_refusal_uses_fallback_model_then_fallback_report(settings):
    sel = _selected()
    client = FakeClient(settings, [LLMRefusal("no"), LLMRefusal("no again")])
    rep = synthesize(WEEK, sel, 50, settings, client)
    assert client.calls[1]["model"] == settings.llm.model_synthesis_fallback
    assert rep.status == "fallback"
    assert rep.swot is None and rep.top_items
    assert "Syntézu AI se tento týden nepodařilo vytvořit" in rep.notices_cs[0]


def test_synthesize_double_validation_failure_falls_back(settings):
    client = FakeClient(settings, [LLMValidationError("a", ""), LLMValidationError("b", "")])
    rep = synthesize(WEEK, _selected(), 50, settings, client)
    assert rep.status == "fallback"


def test_no_cz_content_is_stated_explicitly(settings):
    sel = [make_scored(make_item(i, region="EU"), relevance=7) for i in range(12)]
    rep = synthesize(WEEK, sel, 20, settings, None)
    assert rep.cz_content_present is False
    assert any("České republice" in n for n in rep.notices_cs)


def test_cost_limit_shrinks_input(settings):
    settings.llm.max_cost_usd = 0.05
    sel = _selected(30)
    ids = [s.item.id for s in sel]

    class Expensive(FakeClient):
        def count_tokens(self, **kw):
            return 2000 + kw["user"].count("[ID:") * 900

    client = Expensive(settings, [_draft(ids)])
    rep = synthesize(WEEK, sel, 50, settings, client)
    assert rep.items_selected < 30
    assert any("limitu nákladů" in n for n in rep.notices_cs)
