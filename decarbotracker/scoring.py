"""Druhý stupeň výběru: LLM skórování v dávkách (nebo deterministická heuristika) a kvóty."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from decarbotracker.config import SelectionSettings, Settings, prompt_path
from decarbotracker.filter import KeywordMatcher, default_matcher, item_text, normalize_text
from decarbotracker.llm import (
    ClaudeClient,
    LLMAuthError,
    LLMError,
    LLMMaxTokens,
    LLMModelUnavailable,
    UsageTracker,
    estimate_tokens,
)
from decarbotracker.models import REGION_ORDER, Item, ItemScore, ScoreBatch, ScoredItem

log = logging.getLogger(__name__)

EST_OUTPUT_TOKENS_PER_ITEM = 130


# --------------------------------------------------------------------------- finální skóre


def final_score(item: Item, score: ItemScore, sel: SelectionSettings) -> float:
    """relevance × geo váha × váha typu zdroje × váha zdroje, s bonusy (+30 % postoje, +15 % výzkum)."""
    geo = sel.geo_weights.get(score.geo_focus, 0.5)
    if score.geo_focus == "GLOBAL" and score.relevant_to_cz_eu:
        geo += sel.global_relevance_bonus
    value = score.relevance * geo * sel.source_type_weights.get(item.source_type, 1.0) * item.source_weight
    if score.is_public_attitudes_or_communication:
        value *= 1 + sel.bonus_attitudes
    if score.is_original_research:
        value *= 1 + sel.bonus_original_research
    if score.is_opinion:
        value *= 1 - sel.opinion_penalty
    return round(value, 4)


# --------------------------------------------------------------------------- heuristika (dry-run / záloha)

_CAT_TO_TOPIC = {
    "public_attitudes": "public_attitudes",
    "communication": "communication",
    "forecast": "forecast",
    "just_transition": "just_transition",
    "energy_markets": "energy_markets",
    "decarbonization_policy": "decarbonization_policy",
    "technology": "technology",
    "study": "study",
}
_CZ_MARKERS = ("cesk", "cesi ", "cech", "czech", " cr ", " cr,", "praha", "prague", "moravsk", "brno", "ostrav")


def heuristic_score(item: Item, matcher: KeywordMatcher | None = None) -> ItemScore:
    """Deterministické skóre bez LLM – pro --dry-run a jako záloha při chybě API."""
    matcher = matcher or default_matcher()
    text = item_text(item)
    hits = matcher.hits(text)
    cats = matcher.categories_for(text)
    topics = [_CAT_TO_TOPIC[c] for c in cats if c in _CAT_TO_TOPIC] or ["decarbonization_policy"]
    attitudes = "public_attitudes" in topics or "communication" in topics
    base = 2 + min(len(hits), 5) + (2 if attitudes else 0) + (1 if item.source_type in ("research", "polling") else 0)
    if not item.topic_filter:
        base += 1
    geo = item.region
    norm = " " + normalize_text(text) + " "
    if geo != "CZ" and any(m in norm for m in _CZ_MARKERS):
        geo = "CZ"
    return ItemScore(
        item_id=item.id,
        relevance=max(0, min(10, base)),
        topics=list(dict.fromkeys(topics))[:4],  # type: ignore[arg-type]
        geo_focus=geo,
        is_public_attitudes_or_communication=attitudes,
        is_original_research=item.source_type in ("research", "polling", "journal"),
        relevant_to_cz_eu=item.region in ("CZ", "EU"),
        is_opinion=False,
        title_cs="",
        one_line_cs=(item.summary_raw[:180] + "…") if len(item.summary_raw) > 180 else item.summary_raw or item.title,
        sentiment=0,
    )


# --------------------------------------------------------------------------- LLM


def _format_item(item: Item, chars: int) -> str:
    date = item.published_at.date().isoformat() if item.published_at else f"{item.first_seen.date().isoformat()} (datum spatření)"
    summary = item.summary_raw[:chars]
    return (
        f"[ID: {item.id}]\n"
        f"Titulek: {item.title}\n"
        f"Zdroj: {item.source_name} | typ: {item.source_type} | region zdroje: {item.region} | jazyk: {item.language} | datum: {date}\n"
        f"Výtah: {summary or '(bez výtahu)'}\n"
    )


def _batches(items: list[Item], size: int) -> Iterable[list[Item]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _sanitize(score: ItemScore, item: Item) -> ItemScore:
    score.item_id = item.id
    score.relevance = max(0, min(10, int(score.relevance)))
    score.sentiment = max(-2, min(2, int(score.sentiment)))
    if not score.topics:
        score.topics = ["decarbonization_policy"]
    score.topics = list(dict.fromkeys(score.topics))
    if item.language == "cs":
        score.title_cs = ""
    return score


def score_items(items: list[Item], settings: Settings, client: ClaudeClient | None,
                tracker: UsageTracker | None = None) -> list[ScoredItem]:
    """Ohodnotí položky. Bez klienta (dry-run) nebo po vyčerpání rozpočtu použije heuristiku."""
    sel, llm = settings.selection, settings.llm
    matcher = default_matcher()
    # předřazení heuristikou a oříznutí na max_items_scoring (kvůli nákladům)
    prelim = sorted(items, key=lambda i: final_score(i, heuristic_score(i, matcher), sel), reverse=True)
    to_llm = prelim[: llm.max_items_scoring] if client else []
    rest = prelim[len(to_llm):]
    if client and rest:
        log.info("Skórování: %d položek do LLM, %d nejslabších jen heuristicky", len(to_llm), len(rest))

    results: dict[str, ScoredItem] = {}
    if client:
        system = prompt_path("scoring.md").read_text(encoding="utf-8")
        model = llm.model_scoring
        budget = llm.max_cost_usd * llm.scoring_budget_share
        queue = list(_batches(to_llm, llm.scoring_batch_size))
        while queue:
            batch = queue.pop(0)
            user = "Ohodnoť tyto položky:\n\n" + "\n".join(_format_item(i, llm.scoring_summary_chars) for i in batch)
            est = (tracker or client.tracker).cost(model, estimate_tokens(system + user),
                                                   EST_OUTPUT_TOKENS_PER_ITEM * len(batch))
            if client.tracker.spent + est > budget:
                log.warning("Rozpočet na skórování ($%.2f) by byl překročen – zbytek (%d dávek) heuristicky",
                            budget, len(queue) + 1)
                rest = [i for b in [batch, *queue] for i in b] + rest
                break
            try:
                parsed, _ = client.structured(
                    model=model, system=system, user=user, schema=ScoreBatch,
                    max_tokens=llm.scoring_max_tokens, effort=llm.scoring_effort, cache_system=True,
                )
            except LLMModelUnavailable as exc:
                if model != llm.model_scoring_fallback:
                    log.warning("%s – přepínám skórování na %s", exc, llm.model_scoring_fallback)
                    model = llm.model_scoring_fallback
                    queue.insert(0, batch)
                    continue
                log.error("%s – dávka skórována heuristicky", exc)
                rest.extend(batch)
                continue
            except LLMMaxTokens:
                if len(batch) > 1:
                    half = len(batch) // 2
                    queue[:0] = [batch[:half], batch[half:]]
                    log.warning("max_tokens při skórování – dělím dávku na %d + %d", half, len(batch) - half)
                else:
                    rest.extend(batch)
                continue
            except LLMAuthError as exc:
                log.error("%s – skórování zbytku heuristicky", exc)
                rest = [i for b in [batch, *queue] for i in b] + rest
                break
            except LLMError as exc:
                log.error("Skórování dávky selhalo (%s) – použita heuristika", exc)
                rest.extend(batch)
                continue
            by_id = {s.item_id: s for s in parsed.scores}
            for item in batch:
                s = by_id.get(item.id)
                if s is None:
                    rest.append(item)
                    continue
                s = _sanitize(s, item)
                results[item.id] = ScoredItem(item=item, score=s, final_score=final_score(item, s, sel), scored_by="llm")

    for item in rest if client else prelim:
        if item.id in results:
            continue
        s = heuristic_score(item, matcher)
        results[item.id] = ScoredItem(item=item, score=s, final_score=final_score(item, s, sel), scored_by="heuristic")
    scored = sorted(results.values(), key=lambda s: (-s.final_score, s.item.id))
    n_llm = sum(1 for s in scored if s.scored_by == "llm")
    log.info("Skórováno %d položek (%d LLM, %d heuristicky)", len(scored), n_llm, len(scored) - n_llm)
    return scored


# --------------------------------------------------------------------------- výběr s kvótami


def select_for_synthesis(scored: list[ScoredItem], sel: SelectionSettings, min_relevance: int = 3) -> list[ScoredItem]:
    """Top N podle skóre se zajištěním min. zastoupení CZ a postojů/komunikace (pokud existují)."""
    pool = [s for s in scored if s.score.relevance >= min_relevance]
    pool.sort(key=lambda s: (-s.final_score, s.item.id))
    chosen: dict[str, ScoredItem] = {}
    for s in [x for x in pool if x.score.geo_focus == "CZ"][: sel.min_cz]:
        chosen[s.item.id] = s
    for s in [x for x in pool if x.score.is_public_attitudes_or_communication][: sel.min_attitudes]:
        chosen.setdefault(s.item.id, s)
    for s in pool:
        if len(chosen) >= sel.max_items_synthesis:
            break
        chosen.setdefault(s.item.id, s)
    result = list(chosen.values())[: max(sel.max_items_synthesis, sel.min_cz + sel.min_attitudes)]
    # CZ vždy první, pak podle skóre
    result.sort(key=lambda s: (REGION_ORDER.get(s.score.geo_focus, 9) != 0, -s.final_score, s.item.id))
    return result
