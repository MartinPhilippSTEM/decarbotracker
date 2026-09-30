"""Levný předfiltr podle klíčových slov (CZ i EN, kmeny, bez diakritiky)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache

from decarbotracker.config import load_keywords
from decarbotracker.models import Item
from decarbotracker.normalize import strip_diacritics

log = logging.getLogger(__name__)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", strip_diacritics(text or "").lower())


def _stem_pattern(phrase: str) -> str:
    words = [re.escape(w) for w in normalize_text(phrase).split()]
    # každé slovo je kmen (libovolná koncovka), slova jdou za sebou
    return r"\b" + r"\w*[\s\-]+".join(words)


def _exact_pattern(term: str) -> str:
    words = [re.escape(w) for w in normalize_text(term).split()]
    return r"\b" + r"\s+".join(words) + r"\b"


@dataclass
class KeywordMatcher:
    stems: list[str]
    exact: list[str]
    categories: dict[str, list[str]] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        parts = [_stem_pattern(s) for s in self.stems] + [_exact_pattern(e) for e in self.exact]
        self._any = re.compile("|".join(parts)) if parts else None
        self._each = [(s, re.compile(_stem_pattern(s))) for s in self.stems] + [
            (e, re.compile(_exact_pattern(e))) for e in self.exact
        ]
        self._cats = {
            cat: [re.compile(_stem_pattern(t)) if len(t) > 3 else re.compile(_exact_pattern(t)) for t in terms]
            for cat, terms in self.categories.items()
        }
        self._exclude = [re.compile(_stem_pattern(p)) for p in self.exclude]

    def matches(self, text: str) -> bool:
        return bool(self._any and self._any.search(normalize_text(text)))

    def hits(self, text: str) -> list[str]:
        norm = normalize_text(text)
        return [term for term, rx in self._each if rx.search(norm)]

    def categories_for(self, text: str) -> list[str]:
        norm = normalize_text(text)
        return [cat for cat, rxs in self._cats.items() if any(rx.search(norm) for rx in rxs)]

    def is_excluded(self, title: str) -> bool:
        norm = normalize_text(title)
        return any(rx.search(norm) for rx in self._exclude)


@lru_cache(maxsize=1)
def default_matcher() -> KeywordMatcher:
    kw = load_keywords()
    stems = list((kw.get("stems") or {}).get("cs", [])) + list((kw.get("stems") or {}).get("en", []))
    return KeywordMatcher(
        stems=stems,
        exact=list(kw.get("exact") or []),
        categories=dict(kw.get("categories") or {}),
        exclude=list(kw.get("exclude_title_patterns") or []),
    )


def item_text(item: Item) -> str:
    return f"{item.title} {item.summary_raw}"


def prefilter(items: list[Item], matcher: KeywordMatcher | None = None) -> list[Item]:
    """Zdroje s topic_filter=False projdou vždy; ostatní musí mít shodu v titulku nebo perexu.
    Inzeráty a výzvy k darům (exclude_title_patterns) se vyřadí vždy."""
    matcher = matcher or default_matcher()
    kept: list[Item] = []
    excluded = 0
    for item in items:
        if matcher.is_excluded(item.title):
            excluded += 1
            continue
        if not item.topic_filter or matcher.matches(item_text(item)):
            kept.append(item)
    log.info("Předfiltr: %d → %d položek (%d vyřazeno jako inzerce/sbírky)", len(items), len(kept), excluded)
    return kept
