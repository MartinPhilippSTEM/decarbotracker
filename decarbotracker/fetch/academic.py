"""Odborné články: OpenAlex (volitelný API klíč) a Crossref (bez klíče, polite pool)."""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from decarbotracker.config import JournalSettings, Settings
from decarbotracker.fetch.http import JSON_ACCEPT, FetchError, HttpConfig, get, make_client
from decarbotracker.models import Item, Source
from decarbotracker.normalize import build_item, parse_date, strip_diacritics
from decarbotracker.sources import SourceHealth

log = logging.getLogger(__name__)

OPENALEX_URL = "https://api.openalex.org/works"
CROSSREF_URL = "https://api.crossref.org/journals/{issn}/works"
OPENALEX_SELECT = "id,doi,title,publication_date,primary_location,authorships,abstract_inverted_index,language"


class Budget:
    """Rozpočet API dotazů na jeden běh."""

    def __init__(self, total: int):
        self.total = total
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.total:
            return False
        self.used += 1
        return True


def reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str:
    """Rekonstruuje abstrakt z OpenAlex abstract_inverted_index (ošetřuje None a prázdné)."""
    if not inverted or not isinstance(inverted, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        for i in idxs or []:
            if isinstance(i, int):
                positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def journal_source(name: str, topic_filter: bool) -> Source:
    slug = re.sub(r"[^a-z0-9]+", "-", strip_diacritics(name).lower()).strip("-")
    return Source(
        id=f"journal-{slug}", name=name, type="rss", url="https://api.crossref.org", region="GLOBAL",
        source_type="journal", language="en", topic_filter=topic_filter, weight=1.1, enabled=True,
    )


# --------------------------------------------------------------------------- OpenAlex


def parse_openalex_work(work: dict[str, Any], journals_by_issn: dict[str, JournalSettings],
                        now: datetime | None = None, default_topic_filter: bool = True) -> Item | None:
    loc = work.get("primary_location") or {}
    src = loc.get("source") or {}
    venue = src.get("display_name") or "OpenAlex"
    issns = set(src.get("issn") or [])
    journal = next((journals_by_issn[i] for i in issns if i in journals_by_issn), None)
    source = journal_source(journal.name if journal else venue,
                            journal.topic_filter if journal else default_topic_filter)
    doi = work.get("doi")
    url = loc.get("landing_page_url") or doi or work.get("id") or ""
    if doi and not url.startswith("http"):
        url = f"https://doi.org/{doi}"
    authors = [
        (a.get("author") or {}).get("display_name", "")
        for a in (work.get("authorships") or [])
    ]
    return build_item(
        source,
        title=work.get("title") or work.get("display_name") or "",
        url=doi if (doi and str(doi).startswith("http")) else url,
        published=parse_date(work.get("publication_date")),
        summary=reconstruct_abstract(work.get("abstract_inverted_index")),
        authors=authors,
        doi=doi,
        now=now,
        language=work.get("language") or "en",
    )


def fetch_openalex(client: httpx.Client, settings: Settings, budget: Budget, since: datetime,
                   now: datetime) -> tuple[list[Item], str]:
    ac = settings.academic
    key = settings.openalex_api_key
    limit = ac.max_requests if key else ac.openalex_no_key_max_requests
    params_base: dict[str, Any] = {"per_page": ac.openalex_per_page, "select": OPENALEX_SELECT}
    headers = {"Authorization": f"Bearer {key}"} if key else None
    journals_by_issn = {issn: j for j in ac.journals for issn in j.issn}
    date_filter = f"from_publication_date:{since.date().isoformat()},type:article"

    requests: list[dict[str, Any]] = []
    for q in ac.openalex_queries:
        requests.append({"filter": date_filter, "search": q["search"]})
    issns = [i for j in ac.journals if not j.topic_filter for i in j.issn]
    if issns:
        requests.insert(0, {"filter": f"{date_filter},primary_location.source.issn:{'|'.join(issns)}"})

    items: list[Item] = []
    done = failed = 0
    for req in requests[:limit]:
        if failed >= 2:
            return items, f"OpenAlex: {done} dotazů OK, po {failed} chybách (např. 503) zbytek přeskočen"
        if not budget.take():
            break
        try:
            resp = get(client, OPENALEX_URL, accept=JSON_ACCEPT, params={**params_base, **req},
                       headers=headers, max_retries=0, browser_fallback=False)
        except FetchError as exc:
            if exc.status == 429:
                return items, "OpenAlex: vyčerpaný denní rozpočet (bez API klíče je sdílený podle IP) – přeskočeno"
            log.warning("OpenAlex dotaz selhal: %s", exc)
            failed += 1
            continue
        done += 1
        for work in (resp.json() or {}).get("results", []):
            item = parse_openalex_work(work, journals_by_issn, now=now)
            if item:
                items.append(item)
    note = f"OpenAlex: {done} dotazů{' (bez API klíče – omezený počet)' if not key else ''}"
    return items, note


# --------------------------------------------------------------------------- Crossref


def _crossref_date(work: dict[str, Any]) -> datetime | None:
    created = (work.get("created") or {}).get("date-time")
    if created:
        return parse_date(created)
    for key in ("published-online", "published-print", "published", "issued"):
        parts = ((work.get(key) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            y, m, d = (list(parts) + [1, 1])[:3]
            try:
                return datetime(int(y), int(m or 1), int(d or 1), tzinfo=UTC)
            except (TypeError, ValueError):
                continue
    return None


def parse_crossref_work(work: dict[str, Any], journal: JournalSettings, now: datetime | None = None) -> Item | None:
    if work.get("type") not in (None, "journal-article"):
        return None
    title = " ".join(work.get("title") or [])
    doi = work.get("DOI")
    authors = [" ".join(p for p in (a.get("given"), a.get("family")) if p) for a in work.get("author") or []]
    abstract = re.sub(r"</?jats:[^>]+>", " ", work.get("abstract") or "")
    return build_item(
        journal_source(journal.name, journal.topic_filter),
        title=title,
        url=work.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
        published=_crossref_date(work),
        summary=abstract,
        authors=authors,
        doi=doi,
        now=now,
    )


def fetch_crossref(client: httpx.Client, settings: Settings, budget: Budget, since: datetime,
                   now: datetime) -> tuple[list[Item], str]:
    ac = settings.academic
    items: list[Item] = []
    done, failed = 0, 0
    for journal in ac.journals:
        for issn in journal.issn:
            if not budget.take():
                return items, f"Crossref: {done} časopisů, rozpočet dotazů vyčerpán"
            params: dict[str, Any] = {
                "filter": f"from-created-date:{since.date().isoformat()},type:journal-article",
                "rows": ac.crossref_rows,
                "select": "DOI,title,URL,created,published,issued,author,abstract,container-title,type",
            }
            if settings.contact_email:
                params["mailto"] = settings.contact_email
            try:
                resp = get(client, CROSSREF_URL.format(issn=issn), accept=JSON_ACCEPT, params=params,
                           max_retries=2, browser_fallback=False)
            except FetchError as exc:
                failed += 1
                log.warning("Crossref %s (%s) selhal: %s", journal.name, issn, exc)
                continue
            done += 1
            for work in ((resp.json() or {}).get("message") or {}).get("items", []):
                item = parse_crossref_work(work, journal, now=now)
                if item:
                    items.append(item)
            # respektuj x-rate-limit (typicky 10 req/s) – s rezervou
            limit = resp.headers.get("x-rate-limit-limit")
            interval = resp.headers.get("x-rate-limit-interval", "1s").rstrip("s")
            try:
                time.sleep(max(0.2, float(interval or 1) / max(1, int(limit or 5))))
            except ValueError:
                time.sleep(0.5)
    return items, f"Crossref: {done} časopisů OK, {failed} chyb"


def fetch_academic(settings: Settings, now: datetime | None = None,
                   extra_query: str | None = None) -> tuple[list[Item], list[SourceHealth]]:
    """Stáhne články za posledních `lookback_days` dní. Chyby nikdy neshodí běh."""
    now = now or datetime.now(UTC)
    if not settings.academic.enabled:
        return [], []
    since = now - timedelta(days=settings.academic.lookback_days)
    budget = Budget(settings.academic.max_requests)
    cfg = HttpConfig(user_agent=settings.ua + (f" mailto:{settings.contact_email}" if settings.contact_email else ""),
                     connect_timeout=settings.fetch.connect_timeout, read_timeout=30.0)
    healths: list[SourceHealth] = []
    items: list[Item] = []
    with make_client(cfg) as client:
        for name, fn in (("api-crossref", fetch_crossref), ("api-openalex", fetch_openalex)):
            started = time.monotonic()
            try:
                if extra_query and name == "api-openalex":
                    got, note = fetch_openalex_query(client, settings, budget, since, now, extra_query)
                else:
                    got, note = fn(client, settings, budget, since, now)
                status = "ok" if got else "empty"
            except Exception as exc:  # noqa: BLE001 - API nesmí shodit běh
                log.exception("Chyba akademického API %s", name)
                got, note, status = [], f"{type(exc).__name__}: {exc}"[:300], "parse_error"
            if "vyčerpaný" in note:
                status = "http_error"
            items.extend(got)
            dated = [i.published_at for i in got if i.published_at]
            healths.append(SourceHealth(
                source_id=name, name="Crossref API" if name == "api-crossref" else "OpenAlex API",
                type="api", url="https://api.crossref.org" if name == "api-crossref" else OPENALEX_URL,
                region="GLOBAL", status=status, detail=note, count=len(got),
                newest=max(dated) if dated else None, duration_s=round(time.monotonic() - started, 2),
                checked_at=now,
            ))
            log.info("%s: %d položek (%s)", name, len(got), note)
    log.info("Akademická API: %d dotazů z rozpočtu %d", budget.used, budget.total)
    return items, healths


def fetch_openalex_query(client: httpx.Client, settings: Settings, budget: Budget, since: datetime,
                         now: datetime, query: str) -> tuple[list[Item], str]:
    """Jednorázový tematický dotaz (pro příkaz ask)."""
    key = settings.openalex_api_key
    if not key:
        return [], "OpenAlex: bez API klíče se tematický dotaz neprovádí"
    if not budget.take():
        return [], "OpenAlex: rozpočet vyčerpán"
    journals_by_issn = {issn: j for j in settings.academic.journals for issn in j.issn}
    try:
        resp = get(client, OPENALEX_URL, accept=JSON_ACCEPT, headers={"Authorization": f"Bearer {key}"},
                   params={"filter": f"from_publication_date:{since.date().isoformat()},type:article",
                           "search": query, "per_page": 25, "select": OPENALEX_SELECT},
                   max_retries=1, browser_fallback=False)
    except FetchError as exc:
        return [], f"OpenAlex: {exc}"
    items = [i for w in (resp.json() or {}).get("results", [])
             if (i := parse_openalex_work(w, journals_by_issn, now=now, default_topic_filter=False))]
    return items, f"OpenAlex: tematický dotaz, {len(items)} článků"
