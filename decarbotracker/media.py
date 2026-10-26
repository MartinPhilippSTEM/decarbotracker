"""Sekce „Co psala česká média“: hodinový sběr článků, klasifikace každého kandidáta a komentář.

Postup (pečlivě, bez vzorkování):
1. collect(): každou hodinu se stáhnou feedy médií (media_monitor: true) a nové články se přidají
   do úložiště týdne (data/media/<týden>.json, v CI samostatná větev media-collect). Velké portály
   mají v RSS jen poslední 2–4 hodiny, proto hodinový sběr.
2. analyze(): ze všech zachycených článků týdne široký filtr klíčových slov → KAŽDÝ kandidát projde
   klasifikací (relevance, téma, rámec, žánr, vyznění) → čísla spočítá kód → model napíše komentář
   nad čísly, každé tvrzení doloží články (ID se kontrolují).
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from decarbotracker.config import Settings, data_dir, load_sources, prompt_path
from decarbotracker.filter import item_text, media_matcher
from decarbotracker.llm import ClaudeClient, LLMError, LLMMaxTokens
from decarbotracker.models import (
    Item,
    MediaArticleRef,
    MediaClass,
    MediaClassBatch,
    MediaDraft,
    MediaOutletStat,
    MediaReport,
    Source,
)
from decarbotracker.storage import read_json, write_json
from decarbotracker.weeks import week_bounds, week_of

log = logging.getLogger(__name__)

TOPIC_LABELS = {
    "ceny_energii": "ceny energií", "obnovitelne_zdroje": "obnovitelné zdroje", "jadro": "jádro", "uhli": "uhlí",
    "teplo_a_budovy": "teplo a budovy", "ets2_a_povolenky": "ETS2 a povolenky", "klimaticka_politika": "klimatická politika",
    "pocasi_a_adaptace": "počasí a adaptace", "doprava": "doprava", "energeticka_bezpecnost": "energetická bezpečnost",
    "veda_o_klimatu": "věda o klimatu", "jine": "jiné",
}
FRAME_LABELS = {
    "naklady_a_zatez": "náklady a zátěž", "ekonomicka_prilezitost": "ekonomická příležitost",
    "energeticka_bezpecnost": "energetická bezpečnost", "zdravi_a_zivotni_prostredi": "zdraví a životní prostředí",
    "spravedlnost_a_socialni_dopady": "spravedlnost a sociální dopady", "technologie_a_inovace": "technologie a inovace",
    "politicky_konflikt": "politický konflikt", "skepse_a_odmitani": "skepse a odmítání",
    "odpovednost_a_moralka": "odpovědnost a morálka", "neutralni_informovani": "neutrální informování",
}
GENRE_LABELS = {"zprava": "zpráva", "komentar": "komentář", "rozhovor": "rozhovor", "analyza": "analýza", "jine": "jiné"}


# --------------------------------------------------------------------------- úložiště


def media_dir() -> Path:
    env = os.environ.get("DECARBO_MEDIA_DIR")
    return Path(env) if env else data_dir() / "media"


def store_path(week: str) -> Path:
    return media_dir() / f"{week}.json"


def media_sources() -> list[Source]:
    return [s for s in load_sources() if s.enabled and s.media_monitor]


def load_store(week: str) -> tuple[dict[str, Item], dict]:
    data = read_json(store_path(week), default=None) or {}
    items = {d["id"]: Item.model_validate(d) for d in data.get("items", [])}
    return items, data.get("meta", {})


def save_store(week: str, items: dict[str, Item], meta: dict) -> None:
    ordered = sorted(items.values(), key=lambda i: (i.effective_date, i.id))
    write_json(store_path(week), {"week": week, "meta": meta, "items": [i.model_dump(mode="json") for i in ordered]})


def add_to_store(items: list[Item], now: datetime) -> dict[str, int]:
    """Přidá články do úložiště jejich týdne (jen aktuální a minulý týden). Vrací počty nových podle týdne."""
    current = week_of(now)
    start, _ = week_bounds(current)
    keep_weeks = {current, week_of(start - timedelta(days=1))}  # aktuální + minulý týden (pozdní zachycení)
    by_week: dict[str, list[Item]] = {}
    for it in items:
        w = week_of(it.effective_date)
        if w in keep_weeks:
            by_week.setdefault(w, []).append(it)
    added: dict[str, int] = {}
    for w, new in by_week.items():
        store, meta = load_store(w)
        n0 = len(store)
        for it in new:
            store.setdefault(it.id, it)  # první zachycení vyhrává (zachová first_seen)
        meta.setdefault("first_collection", now.isoformat())
        meta["last_collection"] = now.isoformat()
        meta["collections"] = int(meta.get("collections", 0)) + 1
        save_store(w, store, meta)
        added[w] = len(store) - n0
    return added


def collect(settings: Settings, now: datetime | None = None) -> dict[str, int]:
    """Stáhne všechny sledované zdroje médií a uloží nové články (bez AI, zdarma)."""
    from decarbotracker.sources import fetch_all

    now = now or datetime.now(UTC)
    results = fetch_all(settings, media_sources(), now=now)
    items = [i for r in results for i in r.items]
    added = add_to_store(items, now)
    log.info("Média: staženo %d článků z %d zdrojů, nových: %s", len(items), len(results), added)
    return added


# --------------------------------------------------------------------------- analýza


def _heuristic_class(it: Item) -> MediaClass:
    return MediaClass(item_id=it.id, relevant=True, topic="jine", frame="neutralni_informovani", genre="zprava", tone=0)


def _format(it: Item) -> str:
    return f"[ID: {it.id}] {it.source_name} | {it.title}\n  {it.summary_raw[:350]}\n"


def classify(items: list[Item], settings: Settings, client: ClaudeClient | None) -> tuple[dict[str, MediaClass], list[str]]:
    """Klasifikuje KAŽDÝ článek (dávky; při uříznutí se dávka dělí). Nezklasifikované se vynechají a zalogují."""
    notes: list[str] = []
    if client is None:
        return {it.id: _heuristic_class(it) for it in items}, ["dry-run: klasifikace bez AI"]
    system = prompt_path("media_classify.md").read_text(encoding="utf-8")
    out: dict[str, MediaClass] = {}
    size = settings.media.batch_size
    queue = [items[i:i + size] for i in range(0, len(items), size)]
    while queue:
        batch = queue.pop(0)
        try:
            parsed, _ = client.structured(model=settings.llm.model_scoring, system=system,
                                          user="Klasifikuj tyto články:\n\n" + "\n".join(_format(i) for i in batch),
                                          schema=MediaClassBatch, max_tokens=8000, effort=settings.llm.scoring_effort,
                                          cache_system=True)
        except LLMMaxTokens:
            if len(batch) > 1:
                half = len(batch) // 2
                queue[:0] = [batch[:half], batch[half:]]
            else:
                notes.append(f"článek {batch[0].id} se nepodařilo klasifikovat")
            continue
        except LLMError as exc:
            notes.append(f"dávku {len(batch)} článků se nepodařilo klasifikovat ({exc})")
            continue
        valid = {i.id for i in batch}
        for c in parsed.items:
            if c.item_id in valid:
                c.tone = max(-2, min(2, int(c.tone)))
                out[c.item_id] = c
        missing = valid - set(out)
        if missing:
            notes.append(f"{len(missing)} článků bez klasifikace (model je vynechal)")
    return out, notes


def _stats_text(rep: MediaReport) -> str:
    def fmt(d: dict[str, int], labels: dict[str, str]) -> str:
        return ", ".join(f"{labels.get(k, k)} {v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))

    outlets = ", ".join(f"{o.outlet} {o.relevant}/{o.collected}" for o in rep.outlets if o.relevant)
    return (f"Zachyceno článků: {rep.n_collected}; kandidátů podle klíčových slov: {rep.n_candidates}; "
            f"relevantních k energetice/klimatu: {rep.n_relevant}.\n"
            f"Relevantní/všechny podle médií: {outlets}.\nTémata: {fmt(rep.topics, TOPIC_LABELS)}.\n"
            f"Rámce: {fmt(rep.frames, FRAME_LABELS)}.\nŽánry: {fmt(rep.genres, GENRE_LABELS)}.\n"
            f"Průměrné vyznění vůči dekarbonizaci (−100 až +100): {rep.tone_index}.")


def _validate_draft(draft: MediaDraft, valid: set[str], notes: list[str]) -> MediaDraft:
    for lst_name in ("themes", "notable"):
        kept = []
        for t in getattr(draft, lst_name):
            ids = [i for i in dict.fromkeys(t.evidence_item_ids) if i in valid]
            if not ids:
                notes.append(f"komentář: vyřazen bod bez platného doložení ({t.title_cs[:60]})")
                continue
            t.evidence_item_ids = ids
            kept.append(t)
        setattr(draft, lst_name, kept)
    return draft


def analyze(week: str, settings: Settings, client: ClaudeClient | None) -> MediaReport | None:
    """Mediální obraz týdne z úložiště. Nikdy nevyhazuje výjimku – při chybě vrátí aspoň čísla nebo None."""
    if not settings.media.enabled:
        return None
    store, meta = load_store(week)
    if not store:
        return None
    start, end = week_bounds(week)
    week_items = [i for i in store.values() if start <= i.effective_date <= end]
    collected = Counter(i.source_name for i in week_items)
    matcher = media_matcher()
    candidates = [i for i in week_items if not matcher.is_excluded(i.title) and matcher.matches(item_text(i))]
    notes: list[str] = []
    if len(candidates) > settings.media.max_classify:
        notes.append(f"kandidátů bylo {len(candidates)}, klasifikováno prvních {settings.media.max_classify} (limit nákladů)")
        candidates = sorted(candidates, key=lambda i: i.effective_date, reverse=True)[: settings.media.max_classify]
    classes, cnotes = classify(candidates, settings, client)
    notes += cnotes
    relevant = [(i, classes[i.id]) for i in candidates if i.id in classes and classes[i.id].relevant]
    rel_by_outlet = Counter(i.source_name for i, _ in relevant)
    tones = [c.tone for _, c in relevant]
    rep = MediaReport(
        n_collected=len(week_items), n_candidates=len(candidates), n_relevant=len(relevant),
        collection_since=datetime.fromisoformat(meta["first_collection"]) if meta.get("first_collection") else None,
        outlets=[MediaOutletStat(outlet=o, collected=n, relevant=rel_by_outlet.get(o, 0))
                 for o, n in sorted(collected.items(), key=lambda kv: (-rel_by_outlet.get(kv[0], 0), -kv[1]))],
        topics=dict(Counter(c.topic for _, c in relevant).most_common()),
        frames=dict(Counter(c.frame for _, c in relevant).most_common()),
        genres=dict(Counter(c.genre for _, c in relevant).most_common()),
        tone_index=round(sum(tones) / len(tones) * 50) if tones else 0,
        articles=[MediaArticleRef(id=i.id, title=i.title, url=i.url, outlet=i.source_name, published_at=i.published_at,
                                  topic=c.topic, frame=c.frame, genre=c.genre, tone=c.tone)
                  for i, c in sorted(relevant, key=lambda t: t[0].effective_date, reverse=True)],
        notes=notes,
    )
    if client is None or len(relevant) < settings.media.min_relevant:
        return rep
    # komentář: nad čísly + seznam článků (komentáře/analýzy/rozhovory přednostně, pak nejnovější zprávy)
    ordered = sorted(rep.articles, key=lambda a: (a.genre == "zprava", -(a.published_at or start).timestamp()))
    shown = ordered[: settings.media.max_articles_for_digest]
    user = (f"Týden {week}.\n\n## Čísla (spočítal kód)\n{_stats_text(rep)}\n\n## Relevantní články"
            f"{' (výběr ' + str(len(shown)) + ' z ' + str(len(ordered)) + ')' if len(shown) < len(ordered) else ''}\n"
            + "\n".join(f"[ID: {a.id}] {a.outlet} | {GENRE_LABELS[a.genre]} | {TOPIC_LABELS[a.topic]} | "
                        f"{FRAME_LABELS[a.frame]} | vyznění {a.tone:+d} | {a.title}" for a in shown))
    for max_tokens in (12000, 24000):
        try:
            draft, _ = client.structured(model=settings.llm.model_synthesis,
                                         system=prompt_path("media_digest.md").read_text(encoding="utf-8"),
                                         user=user, schema=MediaDraft, max_tokens=max_tokens, effort="low",
                                         cache_system=False)
            rep.draft = _validate_draft(draft, {a.id for a in rep.articles}, rep.notes)
            break
        except LLMMaxTokens:
            continue
        except LLMError as exc:
            rep.notes.append(f"komentář se nepodařilo vytvořit ({exc})")
            break
    log.info("Média %s: %d zachyceno, %d kandidátů, %d relevantních", week, rep.n_collected, rep.n_candidates,
             rep.n_relevant)
    return rep
