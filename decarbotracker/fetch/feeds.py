"""RSS 2.0 / Atom / RSS 1.0 (RDF) a WordPress REST API (wp_json)."""

from __future__ import annotations

import json
from datetime import datetime

import feedparser
import httpx

from decarbotracker.fetch.http import FEED_ACCEPT, JSON_ACCEPT, get
from decarbotracker.models import Item, Source
from decarbotracker.normalize import build_item, from_struct_time, parse_date


class ParseError(Exception):
    """Odpověď nejde zpracovat; kind = parse_error / html_instead_of_feed / cloudflare."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


# Jen značky skutečné výzvy/blokace – běžné stránky za Cloudflare obsahují i "challenge-platform" skripty.
_CF_MARKERS = (
    b"cf-browser-verification",
    b"cf_chl_opt",
    b"<title>just a moment",
    b"attention required! | cloudflare",
)


def detect_html_instead_of_feed(content: bytes, content_type: str = "") -> str | None:
    """Vrátí 'cloudflare' nebo 'html_instead_of_feed', pokud odpověď není feed."""
    head = content[:6000].lstrip().lower()
    if any(m in head for m in _CF_MARKERS):
        return "cloudflare"
    looks_html = head.startswith(b"<!doctype html") or head.startswith(b"<html")
    has_feed_root = any(tag in head for tag in (b"<rss", b"<feed", b"<rdf:rdf", b"<channel"))
    if looks_html and not has_feed_root:
        return "html_instead_of_feed"
    if "text/html" in content_type.lower() and not has_feed_root and not head.startswith(b"<?xml"):
        return "html_instead_of_feed"
    return None


def _entry_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        dt = from_struct_time(entry.get(key))
        if dt:
            return dt
    for key in ("published", "updated", "created", "dc_date", "date"):
        if entry.get(key):
            dt = parse_date(entry.get(key))
            if dt:
                return dt
    return None


def _entry_summary(entry) -> str:
    if entry.get("summary"):
        return entry.get("summary")
    content = entry.get("content") or []
    if content and isinstance(content, list):
        return content[0].get("value", "")
    return entry.get("description", "")


def _entry_authors(entry) -> list[str]:
    authors = [a.get("name", "") for a in entry.get("authors", []) if isinstance(a, dict)]
    if not authors and entry.get("author"):
        authors = [entry.get("author")]
    return [a.strip() for a in authors if a and a.strip()]


def _entry_doi(entry) -> str | None:
    if entry.get("prism_doi"):
        return entry.get("prism_doi")
    ident = str(entry.get("dc_identifier", ""))
    return ident if "10." in ident and "doi" in ident.lower() else None


def parse_feed_bytes(content: bytes, source: Source, *, content_type: str = "", now: datetime | None = None,
                     base_url: str | None = None) -> list[Item]:
    """Zpracuje bajty feedu. `bozo` je fatální jen tehdy, když nejsou žádné položky."""
    problem = detect_html_instead_of_feed(content, content_type)
    if problem:
        raise ParseError(problem, "Zdroj vrátil HTML stránku místo feedu" if problem != "cloudflare"
                         else "Zdroj vrátil Cloudflare ochranu místo feedu")
    headers = {"content-location": base_url or source.url}
    if content_type:
        headers["content-type"] = content_type
    parsed = feedparser.parse(content, response_headers=headers)
    entries = parsed.get("entries", [])
    if not entries:
        # platný, jen prázdný feed (rozpoznaná verze) není chyba, i když feedparser nastaví bozo
        # např. kvůli chybějícímu Content-Type
        if parsed.get("version"):
            return []
        if parsed.get("bozo"):
            exc = parsed.get("bozo_exception")
            raise ParseError("parse_error", f"Feed nelze zpracovat: {type(exc).__name__}: {exc}"[:300])
        return []
    items: list[Item] = []
    for entry in entries:
        link = entry.get("link") or ""
        if not link:
            for lnk in entry.get("links", []):
                if lnk.get("href"):
                    link = lnk["href"]
                    break
        item = build_item(
            source,
            title=entry.get("title", ""),
            url=link,
            published=_entry_date(entry),
            summary=_entry_summary(entry),
            authors=_entry_authors(entry),
            doi=_entry_doi(entry),
            now=now,
        )
        if item:
            items.append(item)
    return items


def parse_wp_json_bytes(content: bytes, source: Source, *, now: datetime | None = None) -> list[Item]:
    detected = detect_html_instead_of_feed(content)
    if detected:
        raise ParseError(detected, "Zdroj vrátil HTML místo JSON")
    try:
        data = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError("parse_error", f"Neplatný JSON: {exc}"[:300]) from exc
    if isinstance(data, dict) and "code" in data:
        raise ParseError("parse_error", f"WP API chyba: {data.get('code')}")
    if not isinstance(data, list):
        raise ParseError("parse_error", "WP API nevrátilo seznam")
    items: list[Item] = []
    for post in data:
        if not isinstance(post, dict):
            continue
        item = build_item(
            source,
            title=_rendered(post.get("title")),
            # standardní WP: link; vlastní endpointy (např. RMI /resource): url
            url=post.get("link") or post.get("url") or "",
            published=_wp_date(post),
            summary=_rendered(post.get("excerpt")) or _rendered(post.get("description")),
            authors=[post["author"]] if isinstance(post.get("author"), str) else None,
            now=now,
        )
        if item:
            items.append(item)
    return items


def _rendered(value) -> str:
    if isinstance(value, dict):
        return value.get("rendered", "") or ""
    return value if isinstance(value, str) else ""


def _wp_date(post: dict) -> datetime | None:
    if post.get("date_gmt"):
        raw = str(post["date_gmt"])
        return parse_date(raw if raw.endswith("Z") else raw + "Z")
    for key in ("pubDate", "date", "modified_gmt", "modified"):
        if post.get(key):
            dt = parse_date(post[key])
            if dt:
                return dt
    return None


def fetch_feed(client: httpx.Client, source: Source, *, max_retries: int = 2, now: datetime | None = None) -> list[Item]:
    if source.type == "wp_json":
        resp = get(client, source.url, accept=JSON_ACCEPT, max_retries=max_retries)
        return parse_wp_json_bytes(resp.content, source, now=now)
    resp = get(client, source.url, accept=FEED_ACCEPT, max_retries=max_retries)
    return parse_feed_bytes(
        resp.content,
        source,
        content_type=resp.headers.get("content-type", ""),
        now=now,
        base_url=str(resp.url),
    )
