"""Deduplikace (URL, DOI, fuzzy titulek), týdenní okno a stav data/seen.json."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from rapidfuzz import fuzz

from decarbotracker.models import Item
from decarbotracker.normalize import normalize_url, strip_diacritics
from decarbotracker.storage import read_json, seen_path, write_json
from decarbotracker.weeks import parse_week, week_bounds

log = logging.getLogger(__name__)

# primární zdroje (think tank / výzkum) mají přednost před médiem
SOURCE_PRIORITY = {
    "research": 0, "polling": 0, "think_tank": 1, "journal": 1, "government": 2, "ngo": 3, "industry": 4, "media": 5,
}


def _norm_title(title: str) -> str:
    t = strip_diacritics(title).lower()
    return re.sub(r"[^a-z0-9 ]+", " ", t).strip()


def _preference(item: Item) -> tuple:
    """Nižší = lepší."""
    return (SOURCE_PRIORITY.get(item.source_type, 9), -len(item.summary_raw), item.published_at is None, item.id)


def deduplicate(items: list[Item], threshold: int = 90) -> list[Item]:
    """Odstraní duplicity podle normalizované URL, DOI a fuzzy titulku; u duplicit ponechá primární zdroj."""
    ordered = sorted(items, key=_preference)
    kept: list[Item] = []
    by_url: dict[str, Item] = {}
    by_doi: dict[str, Item] = {}
    titles: list[tuple[str, Item]] = []
    for item in ordered:
        nurl = normalize_url(item.url)
        if nurl in by_url or (item.doi and item.doi in by_doi):
            continue
        nt = _norm_title(item.title)
        dup = False
        if len(nt) >= 20:
            for other_t, _other in titles:
                if abs(len(other_t) - len(nt)) > max(len(nt), len(other_t)) * 0.6:
                    continue
                if fuzz.token_set_ratio(nt, other_t) >= threshold:
                    dup = True
                    break
        if dup:
            continue
        kept.append(item)
        by_url[nurl] = item
        if item.doi:
            by_doi[item.doi] = item
        titles.append((nt, item))
    removed = len(items) - len(kept)
    if removed:
        log.info("Deduplikace: odstraněno %d duplicit (%d → %d)", removed, len(items), len(kept))
    # deterministické pořadí
    return sorted(kept, key=lambda i: (i.effective_date, i.id), reverse=True)


# --------------------------------------------------------------------------- seen.json


class SeenState:
    """Mapa id položky → týden, kdy byla poprvé zařazena. Brání opakování položek z minulých týdnů."""

    def __init__(self, items: dict[str, str] | None = None, sources: list[str] | None = None,
                 dois: dict[str, str] | None = None, opportunities: dict[str, str] | None = None):
        self.items: dict[str, str] = dict(items or {})
        self.dois: dict[str, str] = dict(dois or {})
        self.sources: set[str] = set(sources or [])
        # výzvy už zobrazené v sekci Příležitosti (id → týden), aby se neopakovaly
        self.opportunities: dict[str, str] = dict(opportunities or {})

    @classmethod
    def load(cls) -> SeenState:
        data = read_json(seen_path(), default={}) or {}
        return cls(data.get("items"), data.get("sources_initialized"), data.get("dois"), data.get("opportunities"))

    def save(self) -> None:
        write_json(seen_path(), {
            "items": dict(sorted(self.items.items())),
            "dois": dict(sorted(self.dois.items())),
            "sources_initialized": sorted(self.sources),
            "opportunities": dict(sorted(self.opportunities.items())),
        })

    def seen_before(self, item: Item, week: str) -> bool:
        """True, pokud byla položka zařazena do JINÉHO (dřívějšího) týdne."""
        w = self.items.get(item.id) or (self.dois.get(item.doi) if item.doi else None)
        return w is not None and w < week

    def mark(self, items: list[Item], week: str) -> None:
        for item in items:
            self.items.setdefault(item.id, week)
            if item.doi:
                self.dois.setdefault(item.doi, week)

    def prune(self, current_week: str, keep_weeks: int) -> None:
        parse_week(current_week)
        start, _ = week_bounds(current_week)
        y, w, _ = (start - timedelta(weeks=keep_weeks)).isocalendar()
        cutoff = f"{y}-W{w:02d}"
        # potlačené položky (SUPPRESSED) se nemažou, jinak by se archivy výpisů vrátily
        self.items = {k: v for k, v in self.items.items() if v >= cutoff or v == SUPPRESSED}
        self.dois = {k: v for k, v in self.dois.items() if v >= cutoff}


def in_window(item: Item, start: datetime, end: datetime, grace_days: int = 3) -> bool:
    """Položka publikovaná v týdnu; nedatované položky podle data prvního spatření (s tolerancí pro pondělní běh)."""
    if item.published_at is not None:
        return start <= item.published_at <= end
    return start <= item.first_seen <= end + timedelta(days=grace_days)


SUPPRESSED = "0000-W00"  # značka pro nedatované položky, které se nikdy nemají zařadit


def select_window(items: list[Item], week: str, seen: SeenState,
                  undated_first_run_cap: int = 3) -> tuple[list[Item], list[Item]]:
    """Vrátí (položky v okně, potlačené nedatované položky).

    Nedatované položky ze zdroje, který stahujeme poprvé, jsou většinou starý archiv výpisu –
    zařadí se jen `undated_first_run_cap` nejnovějších, ostatní se potlačí natrvalo."""
    start, end = week_bounds(week)
    result: list[Item] = []
    suppressed: list[Item] = []
    undated_by_source: dict[str, int] = {}
    for item in items:
        if seen.items.get(item.id) == SUPPRESSED or seen.seen_before(item, week):
            continue
        first_run_undated = (item.published_at is None and seen.items.get(item.id) != week
                             and item.source_id not in seen.sources)
        if first_run_undated:
            n = undated_by_source.get(item.source_id, 0)
            undated_by_source[item.source_id] = n + 1
            if n >= undated_first_run_cap:
                suppressed.append(item)
                continue
        if not in_window(item, start, end):
            continue
        result.append(item)
    return result, suppressed
