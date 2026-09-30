"""Týdenní syntéza silnějším modelem, validace doložení (evidence_item_ids) a záložní výstup."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from decarbotracker.config import Settings, prompt_path
from decarbotracker.llm import (
    ClaudeClient,
    LLMError,
    LLMMaxTokens,
    LLMRefusal,
    LLMValidationError,
)
from decarbotracker.models import (
    REGION_ORDER,
    ByRegion,
    Event,
    Forecast,
    PublicAttitudes,
    Recommendation,
    ReportDraft,
    ReportItemRef,
    ScoredItem,
    Survey,
    Swot,
    SwotPoint,
    TopItem,
    WeeklyReport,
)
from decarbotracker.weeks import week_bounds

log = logging.getLogger(__name__)

NO_CZ_NOTICE = ("Tento týden k České republice v monitorovaných zdrojích nevyšlo nic podstatného. "
                "Přehled proto vychází jen ze zahraničních zdrojů – to neznamená, že se v ČR nic neděje.")
QUADRANTS = ("strengths", "weaknesses", "opportunities", "threats")


class HardValidationError(Exception):
    pass


# --------------------------------------------------------------------------- vstup pro model


def format_items_for_synthesis(selected: list[ScoredItem], chars: int) -> str:
    blocks = []
    for s in selected:
        it, sc = s.item, s.score
        date = it.published_at.date().isoformat() if it.published_at else f"{it.first_seen.date().isoformat()} (datum spatření)"
        flags = []
        if sc.is_opinion:
            flags.append("KOMENTÁŘ/STANOVISKO")
        if sc.is_original_research:
            flags.append("originální výzkum/data")
        if sc.is_public_attitudes_or_communication:
            flags.append("postoje/komunikace")
        authors = f" | autoři: {', '.join(it.authors[:4])}" if it.authors else ""
        blocks.append(
            f"### [ID: {it.id}]\n"
            f"Titulek: {it.title}\n"
            f"Zdroj: {it.source_name} | typ: {it.source_type} | region zdroje: {it.region} | fokus: {sc.geo_focus} | datum: {date}{authors}\n"
            f"Témata: {', '.join(sc.topics)} | relevance: {sc.relevance}/10{(' | ' + ', '.join(flags)) if flags else ''}\n"
            f"Výtah: {it.summary_raw[:chars] or '(bez výtahu – jen titulek)'}\n"
        )
    return "\n".join(blocks)


def build_user_prompt(week: str, selected: list[ScoredItem], chars: int, cz_present: bool,
                      error_feedback: str | None = None) -> str:
    start, end = week_bounds(week)
    head = (
        f"Týden: {week} ({start.date().isoformat()} – {end.date().isoformat()})\n"
        f"Počet vybraných položek: {len(selected)}\n"
    )
    if not cz_present:
        head += "UPOZORNĚNÍ: mezi položkami není žádná s fokusem na ČR. Řekni to výslovně.\n"
    prompt = head + "\n## Položky\n\n" + format_items_for_synthesis(selected, chars)
    if error_feedback:
        prompt += (
            "\n\n## Oprava předchozího pokusu\nPředchozí výstup neprošel kontrolou:\n"
            f"{error_feedback}\nOprav to. Používej jen ID uvedená výše."
        )
    return prompt


# --------------------------------------------------------------------------- validace


def _valid_ids(ids: list[str], valid: set[str]) -> list[str]:
    return list(dict.fromkeys(i.strip() for i in ids if i and i.strip() in valid))


def validate_draft(draft: ReportDraft, valid: set[str], geo_of: dict[str, str]) -> tuple[ReportDraft, list[str]]:
    """Vyřadí body bez platného doložení, seřadí CZ první; vyhodí HardValidationError, když výsledek nedává smysl."""
    logs: list[str] = []

    def keep_points(points: list, label: str) -> list:
        kept = []
        for p in points:
            before = list(p.evidence_item_ids)
            p.evidence_item_ids = _valid_ids(before, valid)
            if len(p.evidence_item_ids) < len(before):
                logs.append(f"{label}: odstraněna neplatná ID {sorted(set(before) - set(p.evidence_item_ids))}")
            if not p.evidence_item_ids:
                logs.append(f"{label}: vyřazen bod bez platného doložení: {getattr(p, 'text_cs', '')[:80]}")
                continue
            kept.append(p)
        return kept

    for q in QUADRANTS:
        pts = keep_points(getattr(draft.swot, q), f"SWOT/{q}")
        pts.sort(key=lambda p: REGION_ORDER.get(p.geo, 9))
        if len(pts) > 6:
            logs.append(f"SWOT/{q}: zkráceno z {len(pts)} na 6 bodů")
            pts = pts[:6]
        setattr(draft.swot, q, pts)

    tops, seen = [], set()
    for t in draft.top_items:
        tid = t.item_id.strip()
        if tid not in valid:
            logs.append(f"top_items: vyřazena neexistující položka {tid}")
            continue
        if tid in seen:
            continue
        seen.add(tid)
        t.item_id = tid
        tops.append(t)
    tops.sort(key=lambda t: REGION_ORDER.get(geo_of.get(t.item_id, "GLOBAL"), 9) != 0)
    draft.top_items = tops[:12]

    pa = draft.public_attitudes_cs
    pa.surveys = keep_points(pa.surveys, "průzkumy")
    pa.communication_recommendations = keep_points(pa.communication_recommendations, "doporučení")
    draft.forecasts_cs = keep_points(draft.forecasts_cs, "prognózy")
    draft.events_cs = keep_points(draft.events_cs, "události")[:6]
    draft.watchlist_cs = keep_points(draft.watchlist_cs, "watchlist")[:5]

    problems = [f"kvadrant SWOT '{q}' je po kontrole prázdný" for q in QUADRANTS if not getattr(draft.swot, q)]
    if len(draft.top_items) < 3:
        problems.append(f"top_items má jen {len(draft.top_items)} platných položek (očekávám 8–12)")
    if not draft.headline_cs.strip() or not draft.executive_summary_cs.strip():
        problems.append("chybí headline_cs nebo executive_summary_cs")
    for q in QUADRANTS:
        n = len(getattr(draft.swot, q))
        if 0 < n < 3:
            logs.append(f"SWOT/{q}: jen {n} bodů (doporučeno 3–6)")
    if problems:
        raise HardValidationError("; ".join(problems))
    return draft, logs


# --------------------------------------------------------------------------- mock (dry-run)


def mock_draft(selected: list[ScoredItem]) -> ReportDraft:
    """Deterministický ukázkový výstup bez volání LLM (pro --dry-run a testy)."""
    ids = [s.item.id for s in selected]
    geo = {s.item.id: s.score.geo_focus for s in selected}

    def pt(i: int, prefix: str) -> SwotPoint:
        s = selected[i % len(selected)]
        return SwotPoint(text_cs=f"[Ukázka – dry-run] {prefix}: {s.item.title}", evidence_item_ids=[s.item.id],
                         geo=geo[s.item.id])

    swot = Swot(
        strengths=[pt(i, "Opora") for i in range(0, 3)],
        weaknesses=[pt(i, "Bariéra") for i in range(3, 6)],
        opportunities=[pt(i, "Příležitost") for i in range(6, 9)],
        threats=[pt(i, "Hrozba") for i in range(9, 12)],
    )
    tops = [
        TopItem(item_id=s.item.id, short_cs=f"[Ukázka] {s.score.one_line_cs[:120]}",
                why_it_matters_cs=f"[Ukázka – dry-run] **{s.item.source_name}**: {s.score.one_line_cs}",
                key_finding_cs="Ukázkový výstup bez volání AI – konkrétní zjištění ověřte u zdroje.",
                category=s.score.topics[0])
        for s in selected[:10]
    ]
    att = [s for s in selected if s.score.is_public_attitudes_or_communication]
    surveys = [
        Survey(institution=s.item.source_name, fieldwork="neuvedeno", country=s.score.geo_focus, sample_n="neuvedeno",
               method="neuvedeno", finding_cs=f"[Ukázka – dry-run] {s.item.title}", evidence_item_ids=[s.item.id])
        for s in att[:3]
    ]
    recs = [Recommendation(text_cs=f"[Ukázka – dry-run] Doporučení odvozené z: {s.item.title}",
                           evidence_item_ids=[s.item.id]) for s in att[:2]]
    fc = [s for s in selected if "forecast" in s.score.topics][:3]
    forecasts = [Forecast(author=s.item.source_name, horizon="neuvedeno", key_figure="neuvedeno",
                          text_cs=f"[Ukázka – dry-run] {s.item.title}", evidence_item_ids=[s.item.id]) for s in fc]
    cz = [s for s in selected if s.score.geo_focus == "CZ"]
    return ReportDraft(
        headline_cs="[Ukázka – dry-run] Týdenní přehled vygenerovaný bez volání AI.",
        executive_summary_cs=(
            f"Toto je **deterministický ukázkový výstup** režimu --dry-run nad {len(ids)} vybranými položkami. "
            "Slouží k otestování celé pipeline a webu bez API klíče. Texty nejsou analytické shrnutí."
        ),
        swot=swot,
        top_items=tops,
        public_attitudes_cs=PublicAttitudes(
            summary_cs="[Ukázka – dry-run] Přehled položek označených jako postoje veřejnosti a komunikace.",
            surveys=surveys, communication_recommendations=recs),
        forecasts_cs=forecasts,
        by_region=ByRegion(cz=f"[Ukázka] {len(cz)} položek s fokusem na ČR.", eu="[Ukázka] EU.", us="[Ukázka] USA.",
                           **{"global": ""}),
        events_cs=[Event(when="termín neuveden", text_cs=f"[Ukázka – dry-run] {selected[0].item.title}",
                         evidence_item_ids=[selected[0].item.id])],
        watchlist_cs=[],
        data_gaps_cs=["Režim dry-run: bez skutečné syntézy AI."],
    )


# --------------------------------------------------------------------------- hlavní funkce


def item_refs(selected: list[ScoredItem]) -> list[ReportItemRef]:
    return [
        ReportItemRef(
            id=s.item.id, title=s.item.title, title_cs=s.score.title_cs, url=s.item.url,
            source_name=s.item.source_name, source_type=s.item.source_type, region=s.item.region,
            geo_focus=s.score.geo_focus, published_at=s.item.published_at,
            date_is_first_seen=s.item.published_at is None, topics=list(s.score.topics),
            one_line_cs=s.score.one_line_cs, final_score=s.final_score, is_opinion=s.score.is_opinion,
            is_public_attitudes_or_communication=s.score.is_public_attitudes_or_communication,
        )
        for s in selected
    ]


def _fit_budget(client: ClaudeClient, settings: Settings, model: str, system: str, week: str,
                selected: list[ScoredItem], cz_present: bool) -> tuple[list[ScoredItem], int, list[str]]:
    """Zkrátí vstup, pokud by odhad nákladů (count_tokens) překročil zbývající rozpočet."""
    llm = settings.llm
    notes: list[str] = []
    chars = llm.synthesis_summary_chars
    items = list(selected)
    for _ in range(12):
        user = build_user_prompt(week, items, chars, cz_present)
        tokens = client.count_tokens(model=model, system=system, user=user, schema=ReportDraft)
        est = client.tracker.cost(model, tokens, llm.expected_synthesis_output_tokens)
        remaining = llm.max_cost_usd - client.tracker.spent
        log.info("Odhad syntézy: %d vstupních tokenů, ≈ $%.3f (zbývá $%.3f)", tokens, est, remaining)
        if est <= remaining:
            return items, chars, notes
        if chars > 450:
            chars = 450
            notes.append("Výtahy zkráceny kvůli limitu nákladů.")
        elif len(items) > 15:
            # vyřaď nejslabší ne-CZ položku
            non_cz = [s for s in items if s.score.geo_focus != "CZ"]
            victim = min(non_cz or items, key=lambda s: s.final_score)
            items.remove(victim)
        else:
            notes.append("Vstup nešlo zkrátit pod limit nákladů – syntéza přeskočena.")
            raise LLMError("Odhad nákladů syntézy překračuje max_cost_usd i po zkrácení vstupu")
    if len(items) < len(selected):
        notes.append(f"Kvůli limitu nákladů použito {len(items)} z {len(selected)} položek.")
    return items, chars, notes


def fallback_report(week: str, scored: list[ScoredItem], considered: int, settings: Settings,
                    reason: str, status: str = "fallback") -> WeeklyReport:
    start, end = week_bounds(week)
    top = sorted(scored, key=lambda s: (REGION_ORDER.get(s.score.geo_focus, 9) != 0, -s.final_score))[:15]
    cz = any(s.score.geo_focus == "CZ" for s in scored)
    notices = [f"Syntézu AI se tento týden nepodařilo vytvořit ({reason}). Zobrazujeme jen nejlépe hodnocené "
               "položky bez SWOT analýzy."]
    if not cz:
        notices.append(NO_CZ_NOTICE)
    return WeeklyReport(
        week=week, period_from=start, period_to=end, generated_at=datetime.now(UTC), model="—",
        items_considered=considered, items_selected=len(top), status=status, notices_cs=notices,  # type: ignore[arg-type]
        cz_content_present=cz,
        headline_cs="Přehled bez AI syntézy – nejlépe hodnocené položky týdne",
        top_items=[TopItem(item_id=s.item.id, short_cs=s.score.one_line_cs, why_it_matters_cs=s.score.one_line_cs or s.item.title,
                           key_finding_cs="Bez AI syntézy – ověřte u zdroje.", category=s.score.topics[0])
                   for s in top],
        items=item_refs(top),
        data_gaps_cs=[reason],
    )


def synthesize(week: str, selected: list[ScoredItem], considered: int, settings: Settings,
               client: ClaudeClient | None) -> WeeklyReport:
    """Vytvoří WeeklyReport. Nikdy nevyhazuje výjimku – při selhání vrátí záložní report."""
    start, end = week_bounds(week)
    if not selected:
        rep = fallback_report(week, selected, considered, settings, "tento týden nebyly nalezeny žádné relevantní položky")
        rep.notices_cs = ["V daném týdnu nebyly nalezeny žádné relevantní položky."]
        return rep
    cz_present = any(s.score.geo_focus == "CZ" for s in selected)
    valid = {s.item.id for s in selected}
    geo_of = {s.item.id: s.score.geo_focus for s in selected}
    llm = settings.llm
    notices: list[str] = []
    validation_log: list[str] = []

    if client is None:
        try:
            draft, vlog = validate_draft(mock_draft(selected), valid, geo_of)
        except HardValidationError as exc:
            return fallback_report(week, selected, considered, settings, f"dry-run: {exc}", status="dry_run")
        status, model = "dry_run", "mock (dry-run)"
        validation_log.extend(vlog)
        notices.append("Režim dry-run: ukázkový výstup bez volání AI, nejde o analytický text.")
    else:
        system = prompt_path("synthesis.md").read_text(encoding="utf-8")
        model = llm.model_synthesis
        try:
            selected, chars, budget_notes = _fit_budget(client, settings, model, system, week, selected, cz_present)
        except LLMError as exc:
            return fallback_report(week, selected, considered, settings, str(exc))
        notices.extend(budget_notes)
        valid = {s.item.id for s in selected}
        max_tokens = llm.synthesis_max_tokens
        feedback: str | None = None
        draft = None
        validation_retry_used = refusal_fallback_used = max_tokens_retry_used = False
        status = "ok"
        for _attempt in range(5):
            user = build_user_prompt(week, selected, chars, cz_present, feedback)
            try:
                raw, _msg = client.structured(
                    model=model, system=system, user=user, schema=ReportDraft, max_tokens=max_tokens,
                    effort=llm.synthesis_effort, cache_system=False,
                    server_fallback=llm.server_side_fallback,
                )
                draft, vlog = validate_draft(raw, valid, geo_of)
                validation_log.extend(vlog)
                break
            except LLMMaxTokens as exc:
                if max_tokens_retry_used:
                    return _fail(week, selected, considered, settings, client, f"{exc} i po navýšení limitu")
                max_tokens_retry_used = True
                max_tokens = llm.synthesis_max_tokens_retry
                log.warning("%s – opakuji s max_tokens=%d", exc, max_tokens)
            except LLMRefusal as exc:
                if refusal_fallback_used or model == llm.model_synthesis_fallback:
                    return _fail(week, selected, considered, settings, client, str(exc))
                refusal_fallback_used = True
                log.warning("%s – zkouším záložní model %s", exc, llm.model_synthesis_fallback)
                model = llm.model_synthesis_fallback
            except (LLMValidationError, HardValidationError) as exc:
                if validation_retry_used:
                    return _fail(week, selected, considered, settings, client, f"validace selhala i po opravě: {exc}")
                validation_retry_used = True
                feedback = str(exc)[:1500]
                validation_log.append(f"1. pokus neprošel validací: {feedback[:300]}")
                log.warning("Výstup neprošel validací (%s) – jeden opakovaný pokus s chybovou hláškou", exc)
            except LLMError as exc:
                return _fail(week, selected, considered, settings, client, str(exc))
        if draft is None:
            return _fail(week, selected, considered, settings, client, "syntéza se nezdařila")
        for line in validation_log:
            log.info("Validace: %s", line)

    if not cz_present:
        notices.insert(0, NO_CZ_NOTICE)
        if "nevyšlo" not in draft.by_region.cz and "nic" not in draft.by_region.cz:
            draft.by_region.cz = NO_CZ_NOTICE + (" " + draft.by_region.cz if draft.by_region.cz else "")
    report = WeeklyReport(
        week=week, period_from=start, period_to=end, generated_at=datetime.now(UTC), model=model,
        items_considered=considered, items_selected=len(selected), status=status,  # type: ignore[arg-type]
        notices_cs=notices, cz_content_present=cz_present,
        headline_cs=draft.headline_cs, executive_summary_cs=draft.executive_summary_cs, swot=draft.swot,
        top_items=draft.top_items, public_attitudes_cs=draft.public_attitudes_cs, forecasts_cs=draft.forecasts_cs,
        by_region=draft.by_region, events_cs=draft.events_cs, watchlist_cs=draft.watchlist_cs, data_gaps_cs=draft.data_gaps_cs,
        items=item_refs(selected), validation_log=validation_log,
    )
    if client is not None:
        report.usage = client.tracker.info
    return report


def _fail(week: str, selected: list[ScoredItem], considered: int, settings: Settings, client: ClaudeClient,
          reason: str) -> WeeklyReport:
    log.error("Syntéza selhala: %s – publikuji záložní přehled", reason)
    rep = fallback_report(week, selected, considered, settings, reason)
    rep.usage = client.tracker.info
    return rep
