"""Hlídání výkonu zdrojů a rotace: zdroj, který dva týdny po sobě publikuje, ale nic z něj neprojde
do výběru, se dočasně pozastaví a místo něj se aktivuje náhradní zdroj ze zásobníku (reserve: true).

Stav je v data/source_performance.json:
  weeks:     {týden: {zdroj: [položek v okně, položek ve výběru]}}
  paused:    {zdroj: {since, until, replaced_by}}       – pozastavené (po `until` se vrací)
  activated: {náhrada: {since, replaces}}               – aktivované náhrady ze zásobníku
  flagged:   {zdroj: týden}                             – nevýkonné, ale chybí náhrada
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from decarbotracker.config import load_sources
from decarbotracker.models import Item, Source, WeeklyReport
from decarbotracker.storage import read_json, write_json
from decarbotracker.weeks import week_bounds, week_of

log = logging.getLogger(__name__)

UNDERPERFORM_WEEKS = 2      # kolik týdnů po sobě bez jediné položky ve výběru
PAUSE_WEEKS = 4             # na jak dlouho se pozastaví (pak dostane novou šanci)
MAX_ROTATIONS_PER_RUN = 5   # nejvýš tolik výměn za jeden běh (aby se soubor zdrojů neměnil skokově)


def perf_path():
    from decarbotracker.config import data_dir

    return data_dir() / "source_performance.json"


def load_state() -> dict[str, Any]:
    state = read_json(perf_path(), default=None) or {}
    for key in ("weeks", "paused", "activated", "flagged"):
        state.setdefault(key, {})
    return state


def save_state(state: dict[str, Any]) -> None:
    state["weeks"] = dict(sorted(state["weeks"].items())[-16:])  # stačí posledních 16 týdnů
    write_json(perf_path(), state)


def is_protected(s: Source) -> bool:
    """Klíčové zdroje se nikdy automaticky nepozastavují."""
    if s.protected is not None:
        return s.protected
    return s.weight >= 1.3 or s.source_type in ("polling", "funding") or s.funding


def active_sources(state: dict[str, Any] | None = None) -> list[Source]:
    """Zdroje ke stažení: povolené, bez pozastavených, rezervy jen pokud jsou aktivované."""
    state = state if state is not None else load_state()
    return [s for s in load_sources()
            if s.enabled and not s.media_only and s.id not in state["paused"]
            and (not s.reserve or s.id in state["activated"])]


def record_week(state: dict[str, Any], week: str, windowed: list[Item], report: WeeklyReport,
                fetched_source_ids: set[str]) -> None:
    """Uloží, kolik položek měl každý stažený zdroj v okně týdne a kolik z nich prošlo do výběru."""
    selected = {i.id for i in report.items}
    stats: dict[str, list[int]] = {sid: [0, 0] for sid in fetched_source_ids}
    for it in windowed:
        row = stats.setdefault(it.source_id, [0, 0])
        row[0] += 1
        if it.id in selected:
            row[1] += 1
    state["weeks"][week] = dict(sorted(stats.items()))


def _previous_week(week: str) -> str:
    start, _ = week_bounds(week)
    return week_of(start - timedelta(days=1))


def _plus_weeks(week: str, n: int) -> str:
    start, _ = week_bounds(week)
    return week_of(start + timedelta(weeks=n))


def underperformers(state: dict[str, Any], week: str) -> list[tuple[str, int]]:
    """Zdroje, které v posledních UNDERPERFORM_WEEKS týdnech neměly nic ve výběru, i když publikovaly.
    Vrací (id, počet položek v okně za ty týdny), seřazené od nejvíc publikujících."""
    weeks = [week]
    for _ in range(UNDERPERFORM_WEEKS - 1):
        weeks.append(_previous_week(weeks[-1]))
    if any(w not in state["weeks"] for w in weeks):
        return []  # málo historie – zatím nerozhodovat
    sources = {s.id: s for s in load_sources()}
    out = []
    for sid in state["weeks"][week]:
        s = sources.get(sid)
        # už pozastavené zdroje neposuzovat znovu (při opakovaném běhu téhož týdne se načtou i jejich uložené položky)
        if s is None or is_protected(s) or s.source_type == "funding" or sid in state["paused"]:
            continue
        rows = [state["weeks"][w].get(sid) for w in weeks]
        if any(r is None for r in rows):
            continue  # zdroj v některém týdnu nestahován (nový / byl pozastaven)
        published = sum(r[0] for r in rows)
        if published > 0 and all(r[1] == 0 for r in rows):
            out.append((sid, published))
    return sorted(out, key=lambda x: -x[1])


def rotate(state: dict[str, Any], week: str) -> list[str]:
    """Vrátí pozastaveným zdrojům šanci, pozastaví nevýkonné a aktivuje náhrady. Vrací popis změn."""
    changes: list[str] = []
    sources = {s.id: s for s in load_sources()}
    # 1) návrat po uplynutí pauzy
    for sid, info in list(state["paused"].items()):
        if info.get("until", "") <= week:
            del state["paused"][sid]
            changes.append(f"{sid}: návrat po pauze (nová šance)")
    # 2) pozastavení nevýkonných + náhrada ze zásobníku
    reserves = [s for s in sources.values() if s.reserve and s.enabled and s.id not in state["activated"]]
    for sid, published in underperformers(state, week)[:MAX_ROTATIONS_PER_RUN]:
        src = sources[sid]
        pick = next((r for r in reserves if r.region == src.region), None) or (reserves[0] if reserves else None)
        if pick is None:
            if sid not in state["flagged"]:
                state["flagged"][sid] = week
                changes.append(f"{sid}: {UNDERPERFORM_WEEKS} týdny nic ve výběru ({published} položek), "
                               "náhrada v zásobníku chybí – zůstává aktivní")
            continue
        reserves.remove(pick)
        state["paused"][sid] = {"since": week, "until": _plus_weeks(week, PAUSE_WEEKS), "replaced_by": pick.id}
        state["activated"][pick.id] = {"since": week, "replaces": sid}
        state["flagged"].pop(sid, None)
        changes.append(f"{sid}: {UNDERPERFORM_WEEKS} týdny nic ve výběru ({published} položek) → pozastaveno do "
                       f"{state['paused'][sid]['until']}, náhrada: {pick.id}")
    for line in changes:
        log.info("Rotace zdrojů: %s", line)
    return changes


def performance_rows(state: dict[str, Any], last_n: int = 4) -> dict[str, dict[str, int]]:
    """Pro web: {zdroj: {window, selected, weeks}} za posledních N zaznamenaných týdnů."""
    weeks = sorted(state["weeks"])[-last_n:]
    out: dict[str, dict[str, int]] = {}
    for w in weeks:
        for sid, (n_win, n_sel) in state["weeks"][w].items():
            row = out.setdefault(sid, {"window": 0, "selected": 0, "weeks": 0})
            row["window"] += n_win
            row["selected"] += n_sel
            row["weeks"] += 1
    return out


def rebuild_from_history() -> dict[str, Any]:
    """Doplní historii výkonu z uložených týdnů (data/items + data/weeks), bez stahování a bez AI."""
    from decarbotracker.config import data_dir

    state = load_state()
    for wp in sorted((data_dir() / "weeks").glob("*.json")):
        week = wp.stem
        items_file = data_dir() / "items" / f"{week}.json"
        if not items_file.exists():
            continue
        report = WeeklyReport.model_validate(read_json(wp))
        windowed = [Item.model_validate(d) for d in (read_json(items_file) or {}).get("items", [])]
        record_week(state, week, windowed, report, {i.source_id for i in windowed})
    return state
