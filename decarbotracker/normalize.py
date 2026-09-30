"""Normalizace URL, dat a textu do jednotného modelu Item."""

from __future__ import annotations

import hashlib
import html
import re
import time
import unicodedata
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from decarbotracker.models import Item, Source

SUMMARY_MAX = 1500

_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "cmpid", "_hsenc", "_hsmi"}


def normalize_url(url: str) -> str:
    """Odstraní utm_* a další sledovací parametry, fragment, koncové lomítko; sjednotí http/https."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    netloc = parts.netloc.lower()
    if netloc.endswith(":443") or netloc.endswith(":80"):
        netloc = netloc.rsplit(":", 1)[0]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path if path != "/" else "", urlencode(query), ""))


def make_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:12]


def absolutize(url: str, base: str) -> str:
    return urljoin(base, (url or "").strip())


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip().lower()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi)
    doi = doi.removeprefix("doi:")
    return doi or None


# --------------------------------------------------------------------------- data

CZ_MONTHS = {
    "ledna": 1, "leden": 1, "unora": 2, "unor": 2, "brezna": 3, "brezen": 3, "dubna": 4, "duben": 4,
    "kvetna": 5, "kveten": 5, "cervna": 6, "cerven": 6, "cervence": 7, "cervenec": 7, "srpna": 8,
    "srpen": 8, "zari": 9, "rijna": 10, "rijen": 10, "listopadu": 11, "listopad": 11, "prosince": 12,
    "prosinec": 12,
}
PL_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6, "lipca": 7,
    "sierpnia": 8, "wrzesnia": 9, "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}


def strip_diacritics(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def to_utc(dt: datetime | None) -> datetime | None:
    """Naivní datum považuj za UTC; aware převeď na UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def from_struct_time(t: time.struct_time | tuple | None) -> datetime | None:
    if not t:
        return None
    try:
        return datetime(*t[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def parse_date(value: Any) -> datetime | None:
    """Obecný parser dat: datetime, ISO, RFC 822, české/polské měsíce, 30.9.2026 …"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, time.struct_time):
        return from_struct_time(value)
    text = " ".join(str(value).split())
    if not text:
        return None
    plain = strip_diacritics(text).lower()
    # "30. září 2026", "30 września 2026"
    m = re.search(r"(\d{1,2})\.?\s+([a-z]+)\s+(\d{4})", plain)
    if m:
        month = CZ_MONTHS.get(m.group(2)) or PL_MONTHS.get(m.group(2))
        if month:
            try:
                return datetime(int(m.group(3)), month, int(m.group(1)), tzinfo=UTC)
            except ValueError:
                return None
    # "30.9.2026" / "30. 9. 2026"
    m = re.search(r"\b(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})\b", plain)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=UTC)
        except ValueError:
            return None
    try:
        dt = dateparser.parse(text, fuzzy=True, dayfirst=not re.match(r"^\d{4}-", text))
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None or dt.year < 1990 or dt.year > 2100:
        return None
    return to_utc(dt)


# --------------------------------------------------------------------------- text


def clean_text(raw: str | None, limit: int = SUMMARY_MAX) -> str:
    """HTML → čistý text, zkrácený na limit znaků (na hranici slova)."""
    if not raw:
        return ""
    text = raw
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(The post .{0,200} appeared first on .{0,100}\.?)$", "", text).strip()
    if len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0]
        text = cut.rstrip(",.;: ") + "…"
    return text


def clean_title(raw: str | None) -> str:
    return clean_text(raw, limit=400).rstrip("…") if raw else ""


def build_item(
    source: Source,
    *,
    title: str,
    url: str,
    published: datetime | None,
    summary: str = "",
    authors: list[str] | None = None,
    doi: str | None = None,
    now: datetime | None = None,
    language: str | None = None,
) -> Item | None:
    title = clean_title(title)
    url = (url or "").strip()
    if not title or not url.startswith("http"):
        return None
    now = to_utc(now) or datetime.now(UTC)
    published = to_utc(published)
    if published and published > now:
        published = now  # budoucí data (časová pásma, embargo) ořízni
    return Item(
        id=make_id(url),
        title=title,
        url=url,
        source_id=source.id,
        source_name=source.name,
        region=source.region,
        source_type=source.source_type,
        language=language or source.language,
        published_at=published,
        first_seen=now,
        summary_raw=clean_text(summary),
        authors=[a for a in (authors or []) if a][:12],
        doi=normalize_doi(doi),
        topic_filter=source.topic_filter,
        source_weight=source.weight,
        date_is_first_seen=published is None,
    )
