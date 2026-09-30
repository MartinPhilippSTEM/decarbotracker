"""Dotaz na téma kdykoliv během týdne: aktuální zjištění k vybranému tématu."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from decarbotracker.config import Settings, prompt_path
from decarbotracker.dedup import deduplicate
from decarbotracker.fetch.academic import fetch_academic
from decarbotracker.filter import default_matcher, normalize_text, prefilter
from decarbotracker.llm import (
    ClaudeClient,
    LLMError,
    LLMMaxTokens,
    LLMRefusal,
    LLMValidationError,
    UsageTracker,
)
from decarbotracker.models import REGION_ORDER, BriefDraft, ByRegion, Finding, Item, TopicBrief
from decarbotracker.normalize import strip_diacritics
from decarbotracker.scoring import final_score, heuristic_score
from decarbotracker.sources import fetch_all
from decarbotracker.storage import briefs_dir, read_json, write_json
from decarbotracker.synthesis import format_items_for_synthesis, item_refs
from decarbotracker.weeks import week_of

log = logging.getLogger(__name__)

STOPWORDS = {
    "a", "i", "v", "ve", "na", "o", "k", "ke", "s", "se", "z", "ze", "do", "pro", "po", "od", "u", "za", "jak", "co",
    "je", "jsou", "the", "of", "and", "in", "on", "for", "to", "about", "what", "how", "aktualni", "novinky", "zjisteni",
    "tema", "tematu",
}


def slugify(text: str, max_len: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", strip_diacritics(text).lower()).strip("-")
    return s[:max_len].rstrip("-") or "dotaz"


def query_patterns(topic: str) -> list[re.Pattern[str]]:
    """Z dotazu udělá kmeny (kvůli české flexi) – 'tepelná čerpadla' → \\btepeln\\w*, \\bcerpad\\w*."""
    pats = []
    for word in normalize_text(topic).replace("-", " ").split():
        word = re.sub(r"[^a-z0-9]", "", word)
        if len(word) < 2 or word in STOPWORDS:
            continue
        if word.isdigit() or len(word) <= 4:
            pats.append(re.compile(rf"\b{re.escape(word)}\b"))
        else:
            stem = word[: max(4, len(word) - 2)]
            pats.append(re.compile(rf"\b{re.escape(stem)}\w*"))
    return pats


def topic_hits(item: Item, pats: list[re.Pattern[str]]) -> int:
    text = normalize_text(f"{item.title} {item.title} {item.summary_raw}")
    return sum(1 for p in pats if p.search(text))


def load_recent_stored(days: int, now: datetime) -> list[Item]:
    from decarbotracker.storage import items_path

    items: list[Item] = []
    weeks = {week_of(now - timedelta(days=d)) for d in range(0, days + 7, 7)}
    for w in sorted(weeks):
        data = read_json(items_path(w), default=None) or {}
        items.extend(Item.model_validate(d) for d in data.get("items", []))
    return items


def _mock_brief(topic: str, items: list[Item]) -> BriefDraft:
    findings = [
        Finding(text_cs=f"[Ukázka – dry-run] {i.title}", evidence_item_ids=[i.id],
                geo=i.region) for i in items[:6]
    ]
    return BriefDraft(
        headline_cs=f"[Ukázka – dry-run] Přehled k tématu „{topic}“ bez volání AI.",
        summary_cs=f"Deterministický ukázkový výstup nad {len(items)} položkami. Nejde o analytický text.",
        key_findings=findings, public_attitudes_cs="[Ukázka] —", forecasts_cs="[Ukázka] —",
        by_region=ByRegion(cz="[Ukázka]", eu="[Ukázka]", us="[Ukázka]", **{"global": ""}),
        data_gaps_cs=["Režim dry-run."],
    )


def _validate(draft: BriefDraft, valid: set[str]) -> tuple[BriefDraft, list[str]]:
    logs = []
    kept = []
    for f in draft.key_findings:
        ids = [i for i in dict.fromkeys(f.evidence_item_ids) if i in valid]
        if len(ids) < len(f.evidence_item_ids):
            logs.append(f"zjištění: odstraněna neplatná ID {sorted(set(f.evidence_item_ids) - set(ids))}")
        if not ids:
            logs.append(f"vyřazeno zjištění bez doložení: {f.text_cs[:80]}")
            continue
        f.evidence_item_ids = ids
        kept.append(f)
    kept.sort(key=lambda f: REGION_ORDER.get(f.geo, 9))
    draft.key_findings = kept
    if not kept:
        raise LLMValidationError("žádné zjištění nemá platné evidence_item_ids", "")
    return draft, logs


def run_ask(topic: str, settings: Settings, *, days: int | None = None, dry_run: bool = False,
            now: datetime | None = None, items: list[Item] | None = None) -> TopicBrief:
    """Stáhne aktuální položky, vybere relevantní k tématu a nechá model sepsat stručný přehled."""
    now = now or datetime.now(UTC)
    days = days or settings.ask.default_days
    since = now - timedelta(days=days)
    slug = f"{now.date().isoformat()}-{slugify(topic)}"

    if items is None:
        results = fetch_all(settings, now=now)
        items = [i for r in results for i in r.items]
        try:
            acad, _ = fetch_academic(settings, now=now, extra_query=topic)
            items += acad
        except Exception:  # noqa: BLE001
            log.exception("Akademická API selhala")
        items += load_recent_stored(days, now)
    items = [i for i in deduplicate(items) if i.effective_date >= since]
    items = prefilter(items)
    pats = query_patterns(topic)
    matched = [(topic_hits(i, pats), i) for i in items]
    matcher = default_matcher()
    need = max(1, min(2, len(pats)))  # u víceslovných dotazů chci shodu aspoň ve dvou kmenech
    relevant = [(h, i) for h, i in matched if h >= need] or [(h, i) for h, i in matched if h >= 1]
    relevant.sort(key=lambda t: (-t[0], REGION_ORDER.get(t[1].region, 9),
                                 -final_score(t[1], heuristic_score(t[1], matcher), settings.selection), t[1].id))
    chosen = [i for _, i in relevant[: settings.ask.max_items]]
    log.info("Dotaz „%s“: %d položek za %d dní, %d relevantních, použito %d", topic, len(items), days,
             len(relevant), len(chosen))

    brief = TopicBrief(slug=slug, topic=topic, days=days, period_from=since, period_to=now,
                       generated_at=datetime.now(UTC), model="—")
    if not chosen:
        brief.status = "empty"
        brief.headline_cs = f"K tématu „{topic}“ jsme za posledních {days} dní v monitorovaných zdrojích nic nenašli."
        brief.notices_cs = ["Zkuste obecnější formulaci, jiné klíčové slovo nebo delší období (--days)."]
        write_json(briefs_dir() / f"{slug}.json", brief)
        return brief

    scored = []
    from decarbotracker.models import ScoredItem

    for i in chosen:
        s = heuristic_score(i, matcher)
        scored.append(ScoredItem(item=i, score=s, final_score=final_score(i, s, settings.selection), scored_by="heuristic"))
    brief.items = item_refs(scored)
    valid = {i.id for i in chosen}

    client: ClaudeClient | None = None
    if not dry_run:
        try:
            client = ClaudeClient(settings, UsageTracker(settings.llm))
        except LLMError as exc:
            brief.status = "fallback"
            brief.notices_cs = [f"{exc}. Zobrazujeme jen nalezené položky."]
    if dry_run:
        draft, vlog = _validate(_mock_brief(topic, chosen), valid)
        brief.status, brief.model = "dry_run", "mock (dry-run)"
        brief.notices_cs.append("Režim dry-run: ukázkový výstup bez volání AI.")
        brief.validation_log = vlog
    elif client is not None:
        system = prompt_path("ask.md").read_text(encoding="utf-8")
        user = (f"Téma dotazu: {topic}\nObdobí: {since.date().isoformat()} – {now.date().isoformat()}\n\n## Položky\n\n"
                + format_items_for_synthesis(scored, settings.llm.synthesis_summary_chars))
        model, max_tokens, feedback = settings.llm.model_synthesis, settings.llm.synthesis_max_tokens, ""
        draft = None
        for _ in range(3):
            try:
                raw, _m = client.structured(model=model, system=system, user=user + feedback, schema=BriefDraft,
                                            max_tokens=max_tokens, effort=settings.llm.synthesis_effort,
                                            cache_system=False, server_fallback=settings.llm.server_side_fallback)
                draft, brief.validation_log = _validate(raw, valid)
                break
            except LLMMaxTokens:
                max_tokens = settings.llm.synthesis_max_tokens_retry
            except LLMRefusal:
                model = settings.llm.model_synthesis_fallback
            except LLMValidationError as exc:
                feedback = f"\n\n## Oprava\nPředchozí výstup neprošel kontrolou: {exc}. Používej jen ID ze vstupu."
            except LLMError as exc:
                brief.notices_cs.append(f"Syntéza selhala: {exc}")
                break
        brief.usage = client.tracker.info
        if draft is None:
            brief.status = "fallback"
            brief.notices_cs.append("AI shrnutí se nepodařilo vytvořit – zobrazujeme jen nalezené položky.")
        else:
            brief.model = model
    else:
        draft = None

    if draft is not None:
        brief.headline_cs = draft.headline_cs
        brief.summary_cs = draft.summary_cs
        brief.key_findings = draft.key_findings
        brief.public_attitudes_cs = draft.public_attitudes_cs
        brief.forecasts_cs = draft.forecasts_cs
        brief.by_region = draft.by_region
        brief.data_gaps_cs = draft.data_gaps_cs
    elif not brief.headline_cs:
        brief.headline_cs = f"Nalezené položky k tématu „{topic}“ (bez AI shrnutí)"
    if not any(i.region == "CZ" for i in chosen):
        brief.notices_cs.insert(0, "K tomuto tématu jsme v daném období nenašli žádný český zdroj ani obsah o ČR.")
    write_json(briefs_dir() / f"{slug}.json", brief)
    log.info("Uložen dotaz %s (stav: %s, cena ≈ $%.4f)", slug, brief.status, brief.usage.cost_usd)
    return brief
