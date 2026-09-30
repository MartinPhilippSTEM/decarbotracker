"""Obecný konfigurovatelný scraper výpisů (CSS selektory v sources.yaml) s respektem k robots.txt."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from urllib import robotparser
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

from decarbotracker.fetch.feeds import ParseError, detect_html_instead_of_feed
from decarbotracker.fetch.http import HTML_ACCEPT, get
from decarbotracker.models import Item, Source
from decarbotracker.normalize import absolutize, build_item, parse_date

log = logging.getLogger(__name__)


class RobotsBlocked(Exception):
    pass


class RobotsCache:
    """Cache robots.txt per host (vlákna sdílí)."""

    def __init__(self) -> None:
        self._cache: dict[str, robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allowed(self, client: httpx.Client, url: str, user_agent: str) -> bool:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            known = host in self._cache
        if not known:
            rp: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            try:
                resp = client.get(f"{host}/robots.txt", headers={"Accept": "text/plain"}, timeout=10)
                if resp.status_code in (401, 403):
                    rp.disallow_all = True  # type: ignore[union-attr]
                elif resp.status_code >= 400 or "html" in resp.headers.get("content-type", "") and b"user-agent" not in resp.content[:5000].lower():
                    rp = None  # robots.txt neexistuje → vše povoleno
                else:
                    rp.parse(resp.text.splitlines())  # type: ignore[union-attr]
            except httpx.HTTPError:
                rp = None
            with self._lock:
                self._cache[host] = rp
        rp = self._cache[host]
        if rp is None:
            return True
        agent = user_agent.split("/")[0]
        return rp.can_fetch(agent, url) and rp.can_fetch("*", url)


ROBOTS = RobotsCache()


def _text(el: Tag | None) -> str:
    return " ".join(el.get_text(" ").split()) if el else ""


def _select_one(root: Tag, selector: str) -> Tag | None:
    if not selector:
        return None
    if selector == ":self":
        return root
    return root.select_one(selector)


def parse_listing(html: bytes | str, source: Source, *, page_url: str | None = None,
                  now: datetime | None = None) -> list[Item]:
    """Zpracuje HTML výpisu podle selektorů ze sources.yaml."""
    cfg = source.scrape
    if cfg is None:
        raise ParseError("parse_error", "Zdroj typu scrape nemá sekci 'scrape' se selektory")
    raw = html.encode("utf-8") if isinstance(html, str) else html
    soup = BeautifulSoup(raw, "html.parser")
    base = page_url or source.url
    items: list[Item] = []
    seen_urls: set[str] = set()
    nodes = soup.select(cfg.item)
    if not nodes:
        if detect_html_instead_of_feed(raw) == "cloudflare":
            raise ParseError("cloudflare", "Stránka vrátila Cloudflare ochranu")
        raise ParseError("parse_error", f"Selektor položky '{cfg.item}' nic nenašel (změnila se stránka?)")
    for node in nodes:
        title_el = _select_one(node, cfg.title)
        title = _text(title_el)
        link_el = _select_one(node, cfg.link) if cfg.link else None
        if link_el is None:
            if title_el is not None and title_el.name == "a":
                link_el = title_el
            elif title_el is not None and title_el.find_parent("a") is not None:
                link_el = title_el.find_parent("a")
            elif node.name == "a":
                link_el = node
            else:
                link_el = (title_el.select_one("a[href]") if title_el else None) or node.select_one("a[href]")
        href = link_el.get("href") if link_el is not None else None
        if not title or not href or str(href).startswith(("javascript:", "mailto:", "#")):
            continue
        url = absolutize(str(href), base)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        published = None
        if cfg.date:
            date_el = _select_one(node, cfg.date)
            if date_el is not None:
                value = date_el.get(cfg.date_attr) if cfg.date_attr else None
                published = parse_date(value or _text(date_el))
        summary = _text(_select_one(node, cfg.summary)) if cfg.summary else ""
        item = build_item(source, title=title, url=url, published=published, summary=summary, now=now)
        if item:
            items.append(item)
    return items


def fetch_scrape(client: httpx.Client, source: Source, *, user_agent: str, max_retries: int = 2,
                 now: datetime | None = None) -> list[Item]:
    if not ROBOTS.allowed(client, source.url, user_agent):
        raise RobotsBlocked(f"robots.txt zakazuje stahování {source.url}")
    # FetchError (vč. 403 po pokusu s prohlížečovým UA) propadne do health reportu
    resp = get(client, source.url, accept=HTML_ACCEPT, max_retries=max_retries)
    return parse_listing(resp.content, source, page_url=str(resp.url), now=now)
