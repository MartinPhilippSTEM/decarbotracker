"""Výzvy EU Funding & Tenders Portal přes veřejné vyhledávací API (stejné, jaké používá web portálu).

Web portálu je aplikace v JavaScriptu, proto se nescrapuje. Bereme jen otevřené a ohlášené výzvy
se štítkem SSH (společenské a humanitní vědy), které se týkají klimatu, energetiky nebo transformace.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import datetime
from typing import Any

import httpx

from decarbotracker.fetch.http import FetchError
from decarbotracker.models import Item, Source
from decarbotracker.normalize import build_item, clean_text, parse_date

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
STATUS_FORTHCOMING, STATUS_OPEN = "31094501", "31094502"
# tematický filtr nad SSH výzvami (název + popis, bez diakritiky, malá písmena)
TOPIC_RE = re.compile(
    r"\b(climate|energy|decarboni|green transition|energy transition|just transition|net.zero|emission|"
    r"carbon|renewable|fossil|heating|fuel poverty|energy poverty|electrif)", re.I)


def _first(md: dict[str, Any], key: str) -> str:
    v = md.get(key) or [""]
    return str(v[0]) if isinstance(v, list) and v else str(v or "")


def _fmt_date(raw: str) -> str:
    dt = parse_date(raw)
    return f"{dt.day}. {dt.month}. {dt.year}" if dt else "neuvedeno"


def _fmt_eur(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{n:,.0f} EUR".replace(",", " ")


def budget_info(md: dict[str, Any], identifier: str) -> dict[str, str]:
    """Z budgetOverview vytáhne min./max. příspěvek na projekt, rozpočet tématu a počet grantů."""
    try:
        data = json.loads(_first(md, "budgetOverview") or "{}")
    except json.JSONDecodeError:
        return {}
    actions = [a for lst in (data.get("budgetTopicActionMap") or {}).values() for a in lst
               if str(a.get("action", "")).startswith(identifier)]
    if not actions:
        return {}
    mins = [a.get("minContribution") for a in actions if a.get("minContribution")]
    maxs = [a.get("maxContribution") for a in actions if a.get("maxContribution")]
    total = 0.0
    for a in actions:
        for v in (a.get("budgetYearMap") or {}).values():
            with contextlib.suppress(TypeError, ValueError):
                total += float(v)
    grants = sum(int(a.get("expectedGrants") or 0) for a in actions)
    return {
        "min": _fmt_eur(min(mins)) if mins else "",
        "max": _fmt_eur(max(maxs)) if maxs else "",
        "total": _fmt_eur(total) if total else "",
        "grants": str(grants) if grants else "",
    }


def parse_eu_results(data: dict[str, Any], source: Source, *, now: datetime | None = None,
                     require_topic: bool = True) -> list[Item]:
    items: list[Item] = []
    for res in data.get("results", []):
        md = res.get("metadata") or {}
        identifier = _first(md, "identifier")
        title = _first(md, "title") or res.get("title") or ""
        desc = clean_text(_first(md, "descriptionByte"), limit=900)
        if require_topic and not TOPIC_RE.search(f"{title} {_first(md, 'callTitle')} {desc}"):
            continue
        b = budget_info(md, identifier)
        status = "otevřená" if STATUS_OPEN in (md.get("status") or []) else "ohlášená (zatím neotevřená)"
        facts = [
            f"Výzva EU Funding & Tenders: {identifier}",
            f"Program: {_first(md, 'callTitle')} ({_first(md, 'programmePeriod')})",
            f"Stav: {status}",
            f"Otevření: {_fmt_date(_first(md, 'startDate'))}",
            f"Uzávěrka: {_fmt_date(_first(md, 'deadlineDate'))}",
        ]
        if b.get("min") or b.get("max"):
            facts.append(f"Příspěvek EU na projekt: min. {b.get('min') or 'neuvedeno'}, max. {b.get('max') or 'neuvedeno'}")
        if b.get("total"):
            facts.append(f"Rozpočet tématu: {b['total']}" + (f", očekávaný počet grantů: {b['grants']}" if b.get("grants") else ""))
        if md.get("typesOfAction"):
            facts.append(f"Typ akce: {', '.join(md['typesOfAction'])}")
        facts.append(f"Popis: {desc}")
        url = _first(md, "url") or res.get("url") or ""
        item = build_item(source, title=f"{title} ({identifier})" if identifier else title, url=url,
                          published=parse_date(_first(md, "startDate")) or now, summary=" | ".join(facts), now=now)
        if item:
            item.summary_raw = " | ".join(facts)[:1500]  # bez zkracování na hranici slova uprostřed faktů
            items.append(item)
    return items


def fetch_eu_funding(client: httpx.Client, source: Source, *, now: datetime | None = None,
                     page_size: int = 100) -> list[Item]:
    query = {"bool": {"must": [
        {"terms": {"type": ["1", "2", "8"]}},
        {"terms": {"status": [STATUS_FORTHCOMING, STATUS_OPEN]}},
        {"terms": {"crossCuttingPriorities": ["SSH"]}},
    ]}}
    files = {
        "query": ("blob", json.dumps(query), "application/json"),
        "sort": ("blob", json.dumps({"field": "startDate", "order": "DESC"}), "application/json"),
        "languages": ("blob", json.dumps(["en"]), "application/json"),
    }
    params = {"apiKey": "SEDIA", "text": "***", "pageSize": str(page_size), "pageNumber": "1"}
    last_exc: Exception | None = None
    for _attempt in range(2):
        try:
            resp = client.post(SEARCH_URL, params=params, files=files, timeout=60)
            if resp.status_code >= 400:
                raise FetchError("http_error", f"HTTP {resp.status_code}", status=resp.status_code)
            return parse_eu_results(resp.json(), source, now=now)
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
    raise FetchError("network", f"EU Funding & Tenders API: {last_exc}"[:300])
