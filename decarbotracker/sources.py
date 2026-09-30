"""Stahování všech zdrojů a health report (check-sources)."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from decarbotracker.config import Settings, load_sources
from decarbotracker.fetch.feeds import ParseError, fetch_feed
from decarbotracker.fetch.http import FetchError, HttpConfig, make_client
from decarbotracker.fetch.scrape import RobotsBlocked, fetch_scrape
from decarbotracker.models import Item, Source
from decarbotracker.storage import health_path, read_json, write_json

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "ok": "OK",
    "empty": "prázdný",
    "stale": "zastaralý",
    "http_error": "chyba HTTP",
    "parse_error": "chyba parsování",
    "timeout": "timeout",
    "robots": "zakázáno robots.txt",
    "disabled": "vypnutý",
}


class SourceHealth(BaseModel):
    source_id: str
    name: str
    type: str
    url: str
    region: str
    enabled: bool = True
    status: str
    detail: str = ""
    http_status: int | None = None
    count: int = 0
    newest: datetime | None = None
    duration_s: float = 0.0
    checked_at: datetime
    notes: str = ""


class FetchResult(BaseModel):
    health: SourceHealth
    items: list[Item] = []


def fetch_source(source: Source, settings: Settings, client, now: datetime | None = None) -> FetchResult:
    """Stáhne jeden zdroj. Nikdy nevyhazuje výjimku – chyby jdou do health."""
    now = now or datetime.now(UTC)
    started = time.monotonic()
    status, detail, http_status = "ok", "", None
    items: list[Item] = []
    try:
        if source.type in ("rss", "wp_json"):
            items = fetch_feed(client, source, max_retries=settings.fetch.max_retries, now=now)
        elif source.type == "scrape":
            items = fetch_scrape(client, source, user_agent=settings.ua,
                                 max_retries=settings.fetch.max_retries, now=now)
        else:  # pragma: no cover - validace modelu to nedovolí
            raise ParseError("parse_error", f"Neznámý typ zdroje {source.type}")
    except FetchError as exc:
        status = "timeout" if exc.kind == "timeout" else "http_error"
        detail, http_status = str(exc), exc.status
    except ParseError as exc:
        status = "parse_error"
        detail = f"{exc.kind}: {exc}"
    except RobotsBlocked as exc:
        status, detail = "robots", str(exc)
    except Exception as exc:  # scrapery jsou křehké – nikdy neshodit běh
        log.exception("Neočekávaná chyba zdroje %s", source.id)
        status, detail = "parse_error", f"{type(exc).__name__}: {exc}"[:300]

    items = items[: settings.fetch.max_items_per_source]
    dated = [i.published_at for i in items if i.published_at]
    newest = max(dated) if dated else None
    if status == "ok":
        if not items:
            status = "empty"
        elif newest and newest < now - timedelta(days=settings.fetch.stale_days):
            status = "stale"
            detail = f"nejnovější položka je starší než {settings.fetch.stale_days} dní"
        elif not dated:
            detail = "bez dat (použije se datum prvního spatření)"
    health = SourceHealth(
        source_id=source.id, name=source.name, type=source.type, url=source.url, region=source.region,
        enabled=source.enabled, status=status, detail=detail, http_status=http_status, count=len(items),
        newest=newest, duration_s=round(time.monotonic() - started, 2), checked_at=now, notes=source.notes,
    )
    return FetchResult(health=health, items=items)


def fetch_all(settings: Settings, sources: list[Source] | None = None, now: datetime | None = None) -> list[FetchResult]:
    sources = [s for s in (sources if sources is not None else load_sources()) if s.enabled]
    cfg = HttpConfig(user_agent=settings.ua, connect_timeout=settings.fetch.connect_timeout,
                     read_timeout=settings.fetch.read_timeout, max_retries=settings.fetch.max_retries)
    results: list[FetchResult] = []
    with make_client(cfg) as client, ThreadPoolExecutor(max_workers=settings.fetch.max_workers) as pool:
        futures = {pool.submit(fetch_source, s, settings, client, now): s for s in sources}
        for fut in as_completed(futures):
            res = fut.result()
            h = res.health
            log.info("%-28s %-15s %3d položek %s", h.source_id, STATUS_LABELS.get(h.status, h.status), h.count,
                     f"({h.detail})" if h.detail and h.status != "ok" else "")
            results.append(res)
    order = {s.id: i for i, s in enumerate(sources)}
    results.sort(key=lambda r: order.get(r.health.source_id, 999))
    return results


def disabled_health(now: datetime | None = None) -> list[SourceHealth]:
    now = now or datetime.now(UTC)
    return [
        SourceHealth(source_id=s.id, name=s.name, type=s.type, url=s.url, region=s.region, enabled=False,
                     status="disabled", detail=s.notes, checked_at=now, notes=s.notes)
        for s in load_sources()
        if not s.enabled
    ]


def save_health(healths: list[SourceHealth], extra: list[SourceHealth] | None = None) -> None:
    """Uloží health report; u zdrojů, které se tentokrát nestahovaly, ponechá poslední známý stav."""
    previous = read_json(health_path(), default={}) or {}
    merged: dict[str, dict] = {h["source_id"]: h for h in previous.get("sources", []) if isinstance(h, dict)}
    for h in list(healths) + list(extra or []):
        merged[h.source_id] = h.model_dump(mode="json")
    valid_ids = {s.id for s in load_sources()} | {h.source_id for h in extra or []}
    rows = [v for k, v in merged.items() if k in valid_ids or k.startswith("api-")]
    write_json(health_path(), {"checked_at": datetime.now(UTC).isoformat(), "sources": rows})


def load_health() -> dict[str, dict]:
    data = read_json(health_path(), default={}) or {}
    return {h["source_id"]: h for h in data.get("sources", []) if isinstance(h, dict)}


def format_health_table(healths: list[SourceHealth]) -> str:
    header = f"{'ZDROJ':<30} {'TYP':<8} {'REG':<6} {'STAV':<20} {'POČET':>5}  {'NEJNOVĚJŠÍ':<10}  DETAIL"
    lines = [header, "-" * len(header)]
    for h in healths:
        newest = h.newest.strftime("%Y-%m-%d") if h.newest else "—"
        label = STATUS_LABELS.get(h.status, h.status)
        lines.append(f"{h.source_id[:30]:<30} {h.type:<8} {h.region:<6} {label:<20} {h.count:>5}  {newest:<10}  {h.detail[:70]}")
    enabled = [h for h in healths if h.enabled]
    ok = sum(1 for h in enabled if h.status == "ok")
    if enabled:
        lines.append("-" * len(header))
        lines.append(f"OK: {ok}/{len(enabled)} povolených zdrojů ({ok / len(enabled):.0%})")
    return "\n".join(lines)
