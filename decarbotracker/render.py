"""Statický web z uložených JSON dat (Jinja2 → HTML/CSS, bez JS)."""

from __future__ import annotations

import logging
import re
import shutil
import time
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup, escape
from pydantic import ValidationError

from decarbotracker.config import CODE_ROOT, Settings, load_sources, normalize_base_url, project_root
from decarbotracker.models import TopicBrief, WeeklyReport
from decarbotracker.sources import STATUS_LABELS, load_health
from decarbotracker.storage import briefs_dir, read_json
from decarbotracker.weeks import format_period_cs, week_slug

log = logging.getLogger(__name__)

REGION_LABELS = {"CZ": "Česko", "EU": "EU/Evropa", "US": "USA", "GLOBAL": "Globální"}
SOURCE_TYPE_LABELS = {
    "think_tank": "think tank", "research": "výzkum", "polling": "průzkum", "government": "vláda/instituce",
    "media": "média", "journal": "odborný časopis", "ngo": "NGO", "industry": "průmysl",
    "funding": "grantový portál",
}
TOPIC_LABELS = {
    "decarbonization_policy": "klimatická politika", "energy_markets": "energetické trhy",
    "public_attitudes": "postoje veřejnosti", "communication": "komunikace", "forecast": "prognóza",
    "study": "studie", "just_transition": "spravedlivá transformace", "technology": "technologie",
}
MONTHS_CS = ["ledna", "února", "března", "dubna", "května", "června", "července", "srpna", "září", "října",
             "listopadu", "prosince"]
MANUAL_SOURCES = [
    ("Eurobarometr (klima, energie)", "https://europa.eu/eurobarometer/", "nízká frekvence, JS web"),
    ("EIB Climate Survey", "https://www.eib.org/en/surveys/climate-survey/index", "jednou ročně, JS web"),
    ("IPCC", "https://www.ipcc.ch/", "nízká frekvence, JS web"),
]


def date_cs(value: Any, with_year: bool = True) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    s = f"{value.day}. {MONTHS_CS[value.month - 1]}"
    return f"{s} {value.year}" if with_year else s


_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)


def rich(value: Any) -> Markup:
    """Text z modelu → HTML: vše se escapuje, jen **tučně** se převede na <strong>."""
    if not value:
        return Markup("")
    escaped = str(escape(str(value)))
    return Markup(_BOLD.sub(r"<strong>\1</strong>", escaped).replace("**", ""))


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„\"(*])")


def first_sentence(value: Any) -> str:
    """První věta textu (pro krátký sloupec tabulky); nerozdělí zkratky typu „tzv.“ uprostřed věty."""
    text = str(value or "").strip()
    parts = _SENTENCE_END.split(text, maxsplit=1)
    first = parts[0].strip()
    if first.count("**") % 2:  # nerozbij tučné zvýraznění přes hranici věty
        first = first.replace("**", "")
    return first


def plain(value: Any) -> str:
    """Stejný text bez značek ** (pro <title>, RSS, meta description)."""
    return str(value or "").replace("**", "")


def load_reports() -> list[WeeklyReport]:
    reports = []
    for path in sorted((project_root() / "data" / "weeks").glob("*.json"), reverse=True):
        try:
            reports.append(WeeklyReport.model_validate(read_json(path)))
        except (ValidationError, ValueError) as exc:
            log.error("Soubor %s nelze načíst: %s", path.name, exc)
    return reports


def load_briefs() -> list[TopicBrief]:
    briefs = []
    for path in sorted(briefs_dir().glob("*.json"), reverse=True):
        try:
            briefs.append(TopicBrief.model_validate(read_json(path)))
        except (ValidationError, ValueError) as exc:
            log.error("Dotaz %s nelze načíst: %s", path.name, exc)
    return briefs


def make_env(base_url: str, settings: Settings) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(CODE_ROOT / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(
        base=base_url,
        site=settings.site,
        region_labels=REGION_LABELS,
        source_type_labels=SOURCE_TYPE_LABELS,
        topic_labels=TOPIC_LABELS,
        status_labels=STATUS_LABELS,
        now=datetime.now(UTC),
    )
    env.filters["date_cs"] = date_cs
    env.filters["rich"] = rich
    env.filters["plain"] = plain
    env.filters["first_sentence"] = first_sentence
    env.filters["rfc822"] = lambda d: format_datetime(d if isinstance(d, datetime) else datetime.fromisoformat(d))
    env.filters["week_slug"] = week_slug
    return env


def _clean_dir(out: Path) -> None:
    """Smaže starý výstup. Na Windows/OneDrive bývají složky dočasně zamčené – pak se soubory jen přepíšou."""
    if not out.exists():
        return
    for attempt in range(3):
        try:
            shutil.rmtree(out)
            return
        except OSError:
            time.sleep(0.5 * (attempt + 1))
    # nepovedlo se smazat celou složku: smaž alespoň soubory, prázdné složky nevadí
    for f in sorted(out.rglob("*"), reverse=True):
        try:
            if f.is_file():
                f.unlink()
            else:
                f.rmdir()
        except OSError:
            pass
    log.debug("Výstupní složku %s nešlo celou smazat (zámek souborů) – soubory se přepíší", out)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def build_site(settings: Settings, out_dir: Path | None = None, base_url: str | None = None) -> Path:
    base = normalize_base_url(base_url or settings.site.base_url)
    out = out_dir or (project_root() / settings.site.output_dir)
    _clean_dir(out)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(CODE_ROOT / "static", out / "static", dirs_exist_ok=True)
    env = make_env(base, settings)
    reports = load_reports()
    briefs = load_briefs()
    health = load_health()

    def ctx(r: WeeklyReport) -> dict[str, Any]:
        items = {i.id: i for i in r.items}
        return {"r": r, "items": items, "period": format_period_cs(r.period_from, r.period_to)}

    week_tpl = env.get_template("week.html")
    if reports:
        latest = reports[0]
        _write(out / "index.html", week_tpl.render(**ctx(latest), is_latest=True, page="home", recent=reports[:8],
                                                   briefs=briefs[:3]))
    else:
        _write(out / "index.html", env.get_template("empty.html").render(page="home"))
    for r in reports:
        _write(out / "tydny" / week_slug(r.week) / "index.html",
               week_tpl.render(**ctx(r), is_latest=False, page="week", recent=[], briefs=[]))
    _write(out / "archiv" / "index.html", env.get_template("archive.html").render(
        reports=reports, page="archive", period_of=lambda r: format_period_cs(r.period_from, r.period_to)))

    sources = load_sources()
    rows = []
    for s in sources:
        h = health.get(s.id, {})
        rows.append({"s": s, "h": h})
    api_rows = [h for k, h in health.items() if k.startswith("api-")]
    _write(out / "metodika" / "index.html", env.get_template("methodology.html").render(
        page="methodology", rows=rows, api_rows=api_rows, sel=settings.selection, llm=settings.llm,
        manual=MANUAL_SOURCES, checked_at=(read_json(project_root() / "data" / "source_health.json", {}) or {}).get("checked_at"),
    ))

    brief_tpl = env.get_template("brief.html")
    for b in briefs:
        _write(out / "dotazy" / b.slug / "index.html",
               brief_tpl.render(b=b, items={i.id: i for i in b.items}, page="briefs"))
    _write(out / "dotazy" / "index.html", env.get_template("briefs.html").render(briefs=briefs, page="briefs"))
    _write(out / "404.html", env.get_template("404.html").render(page="404"))
    _write(out / "feed.xml", env.get_template("feed.xml").render(
        reports=reports[:20], site_url=settings.site.site_url,
        period_of=lambda r: format_period_cs(r.period_from, r.period_to)))
    _write(out / ".nojekyll", "")
    log.info("Web vygenerován do %s (base_url=%s, %d týdnů, %d dotazů)", out, base, len(reports), len(briefs))
    return out
