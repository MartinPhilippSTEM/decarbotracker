"""Celá týdenní pipeline: fetch → okno + deduplikace → předfiltr → skórování → syntéza → uložení."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from decarbotracker.config import Settings, load_sources
from decarbotracker.dedup import SUPPRESSED, SeenState, deduplicate, select_window
from decarbotracker.fetch.academic import fetch_academic
from decarbotracker.filter import prefilter
from decarbotracker.llm import ClaudeClient, LLMError, UsageTracker
from decarbotracker.models import Item, WeeklyReport
from decarbotracker.opportunities import find_opportunities
from decarbotracker.scoring import score_items, select_for_synthesis
from decarbotracker.sentiment import summarize as summarize_sentiment
from decarbotracker.sources import SourceHealth, disabled_health, fetch_all, save_health
from decarbotracker.storage import items_path, read_json, week_path, write_json
from decarbotracker.synthesis import fallback_report, synthesize

log = logging.getLogger(__name__)

Collector = Callable[[Settings, datetime], tuple[list[Item], list[SourceHealth]]]


def collect_items(settings: Settings, now: datetime) -> tuple[list[Item], list[SourceHealth]]:
    """Stáhne všechny povolené zdroje + akademická API. Nikdy nevyhazuje výjimku kvůli zdroji."""
    results = fetch_all(settings, now=now)
    items = [i for r in results for i in r.items]
    healths = [r.health for r in results]
    try:
        acad_items, acad_health = fetch_academic(settings, now=now)
    except Exception:  # noqa: BLE001
        log.exception("Akademická API selhala")
        acad_items, acad_health = [], []
    return items + acad_items, healths + acad_health


def load_stored_items(week: str) -> list[Item]:
    data = read_json(items_path(week), default=None)
    if not data:
        return []
    return [Item.model_validate(d) for d in data.get("items", [])]


def merge_with_stored(fresh: list[Item], stored: list[Item]) -> list[Item]:
    """Uložené položky mají přednost (zachová first_seen) → opakovaný běh pro stejný týden dá stejný vstup."""
    by_id = {i.id: i for i in fresh}
    for s in stored:
        by_id[s.id] = s
    return list(by_id.values())


def run_pipeline(week: str, settings: Settings, *, dry_run: bool = False, reuse_items: bool = False,
                 now: datetime | None = None, collector: Collector | None = None) -> WeeklyReport:
    now = now or datetime.now(UTC)
    log.info("=== decarbotracker: týden %s%s ===", week, " (dry-run)" if dry_run else "")

    stored = load_stored_items(week)
    if reuse_items and stored:
        log.info("Používám uložené položky týdne (%d), bez stahování", len(stored))
        fetched: list[Item] = []
    else:
        fetched, healths = (collector or collect_items)(settings, now)
        save_health(healths, extra=disabled_health(now))
        errors = [h for h in healths if h.status not in ("ok", "empty", "stale")]
        log.info("Staženo %d položek z %d zdrojů (%d s chybou)", len(fetched), len(healths), len(errors))
        for h in errors:
            log.warning("Zdroj %s: %s %s", h.source_id, h.status, h.detail)

    seen = SeenState.load()
    # výzvy z grantových portálů nejsou zprávy – jdou jen do sekce Příležitosti
    all_items = [i for i in merge_with_stored(fetched, stored) if i.source_type != "funding"]
    windowed, suppressed = select_window(all_items, week, seen, settings.selection.undated_first_run_cap)
    # deduplikace až nad týdenním oknem (fuzzy porovnání je O(n²)); vrací deterministické pořadí
    windowed = deduplicate(windowed, settings.selection.fuzzy_title_threshold)
    write_json(items_path(week), {"week": week, "generated_at": now.isoformat(),
                                  "items": [i.model_dump(mode="json") for i in windowed]})
    per_source = Counter(i.source_id for i in windowed)
    log.info("V okně týdne %s: %d položek; po zdrojích: %s", week, len(windowed),
             ", ".join(f"{k}={v}" for k, v in per_source.most_common()))

    candidates = prefilter(windowed)

    client: ClaudeClient | None = None
    if not dry_run:
        try:
            client = ClaudeClient(settings, UsageTracker(settings.llm))
        except LLMError as exc:
            log.error("%s – pokračuji bez LLM (záložní přehled)", exc)

    if not dry_run and client is None:
        scored = score_items(candidates, settings, None)
        report = fallback_report(week, select_for_synthesis(scored, settings.selection), len(candidates), settings,
                                 "chybí nebo je neplatný ANTHROPIC_API_KEY")
    else:
        scored = score_items(candidates, settings, client)
        selected = select_for_synthesis(scored, settings.selection)
        n_cz = sum(1 for s in selected if s.score.geo_focus == "CZ")
        n_att = sum(1 for s in selected if s.score.is_public_attitudes_or_communication)
        log.info("Do syntézy vybráno %d položek (CZ: %d, postoje/komunikace: %d)", len(selected), n_cz, n_att)
        report = synthesize(week, selected, len(candidates), settings, client)

    # Orientační odhad nálady diskurzu (z hodnocení AI; bez dalšího volání)
    report.sentiment = summarize_sentiment(scored, {i.id for i in report.items})

    # Příležitosti (výzvy, granty) – z čerstvě stažených položek; s --reuse-items se přeskočí
    funding_ids = {s.id for s in load_sources() if s.funding}
    opportunities, opp_log = find_opportunities(fetched, settings, client, seen, funding_ids, now, week)
    report.opportunities_cs = opportunities
    report.validation_log.extend(opp_log)
    if client is not None:
        report.usage = client.tracker.info

    write_json(week_path(week), report)
    log.info("Uložen %s (stav: %s)", week_path(week).name, report.status)
    u = report.usage
    if u.calls:
        log.info("Spotřeba LLM: %d volání, vstup %d, výstup %d, cache zápis %d / čtení %d tokenů, odhad ceny $%.4f",
                 u.calls, u.input_tokens, u.output_tokens, u.cache_creation_input_tokens,
                 u.cache_read_input_tokens, u.cost_usd)

    if not dry_run:
        seen.mark(windowed, week)
        for item in suppressed:
            seen.items.setdefault(item.id, SUPPRESSED)
        seen.sources.update(i.source_id for i in fetched)
        for opp in opportunities:
            seen.opportunities.setdefault(opp.item_id, week)
        seen.prune(week, settings.selection.seen_retention_weeks)
        seen.save()
    return report
