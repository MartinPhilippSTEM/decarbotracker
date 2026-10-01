"""Po vygenerování přehledu: 3 nejzajímavější zjištění (min. 1 CZ, min. 1 svět) + návrh vlákna na X."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from decarbotracker.config import Settings, prompt_path
from decarbotracker.llm import ClaudeClient, LLMError, LLMMaxTokens
from decarbotracker.models import Digest, DigestDraft, DigestFinding, DigestFindingOut, WeeklyReport
from decarbotracker.weeks import week_slug

log = logging.getLogger(__name__)

X_LIMIT = 280
X_URL_WEIGHT = 23  # X počítá každý odkaz jako 23 znaků
_URL_RE = re.compile(r"https?://\S+")


def x_length(text: str) -> int:
    return len(_URL_RE.sub("x" * X_URL_WEIGHT, text))


def fit_x(text: str) -> str:
    """Zkrátí příspěvek na limit X (odkaz zachová)."""
    text = " ".join(text.split())
    if x_length(text) <= X_LIMIT:
        return text
    urls = _URL_RE.findall(text)
    body = _URL_RE.sub("", text).strip()
    room = X_LIMIT - sum(X_URL_WEIGHT + 1 for _ in urls) - 1
    body = body[:room].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return " ".join([body, *urls])


def report_url(settings: Settings, week: str) -> str:
    return f"{settings.site.site_url}tydny/{week_slug(week)}/"


def _input_text(report: WeeklyReport) -> str:
    items = {i.id: i for i in report.items}
    lines = [f"Týden: {report.week}", f"Hlavní poselství: {report.headline_cs}",
             f"Shrnutí: {report.executive_summary_cs}", "", "## Nejdůležitější položky"]
    for t in report.top_items:
        it = items.get(t.item_id)
        if not it:
            continue
        lines.append(f"[ID: {it.id}] geo={it.geo_focus} | {it.source_name} | {it.title}"
                     f"{' | NÁZOR' if it.is_opinion else ''}\n  Zjištění: {t.key_finding_cs}\n  Proč: {t.why_it_matters_cs}")
    if report.public_attitudes_cs:
        lines += ["", "## Postoje veřejnosti", report.public_attitudes_cs.summary_cs]
        for s in report.public_attitudes_cs.surveys:
            lines.append(f"- {s.institution} ({s.country}, N={s.sample_n}): {s.finding_cs} [ID: {', '.join(s.evidence_item_ids)}]")
    return "\n".join(lines)


def _deterministic(report: WeeklyReport) -> list[DigestFinding]:
    """Záloha bez AI: první CZ položka, první ze světa a další nejlépe hodnocená."""
    items = {i.id: i for i in report.items}
    tops = [(t, items[t.item_id]) for t in report.top_items if t.item_id in items]
    cz = next(((t, i) for t, i in tops if i.geo_focus == "CZ"), None)
    world = next(((t, i) for t, i in tops if i.geo_focus != "CZ"), None)
    chosen = [x for x in (cz, world) if x]
    for x in tops:
        if len(chosen) >= 3:
            break
        if x not in chosen:
            chosen.append(x)
    return [DigestFinding(item_id=i.id, geo=i.geo_focus, title_cs=i.title_cs or i.title,
                          text_cs=(t.key_finding_cs or t.why_it_matters_cs).replace("**", ""))
            for t, i in chosen[:3]]


def _enforce_mix(findings: list[DigestFinding], report: WeeklyReport, notes: list[str]) -> list[DigestFinding]:
    """Zajistí min. 1 CZ a min. 1 svět (pokud v přehledu existují), jinak doplní deterministicky."""
    valid = {i.id: i for i in report.items}
    out, seen = [], set()
    for f in findings:
        if f.item_id in valid and f.item_id not in seen:
            f.geo = valid[f.item_id].geo_focus  # geo podle přehledu, ne podle modelu
            out.append(f)
            seen.add(f.item_id)
        elif f.item_id not in valid:
            notes.append(f"vyřazeno zjištění s neexistujícím ID {f.item_id}")
    backup = [f for f in _deterministic(report) if f.item_id not in seen]
    for need_cz in (True, False):
        has = any((f.geo == "CZ") == need_cz for f in out)
        exists = any((i.geo_focus == "CZ") == need_cz for i in report.items)
        if not has and exists:
            repl = next((b for b in backup if (b.geo == "CZ") == need_cz), None)
            if repl:
                if len(out) >= 3:
                    # nahraď poslední zjištění, které nepatří k chybějící skupině a není jediné své skupiny
                    for idx in range(len(out) - 1, -1, -1):
                        group = out[idx].geo == "CZ"
                        if sum(1 for f in out if (f.geo == "CZ") == group) > 1:
                            out[idx] = repl
                            break
                else:
                    out.append(repl)
                seen.add(repl.item_id)
                notes.append("doplněno zjištění " + ("z ČR" if need_cz else "ze světa"))
        elif not has:
            notes.append("v přehledu není žádná položka " + ("z ČR" if need_cz else "ze světa"))
    for b in backup:
        if len(out) >= 3:
            break
        if b.item_id not in seen:
            out.append(b)
            seen.add(b.item_id)
    # CZ první
    return sorted(out[:3], key=lambda f: f.geo != "CZ")


def _default_thread(findings: list[DigestFindingOut], headline: str) -> list[str]:
    posts = [f"🧵 Co tento týden přinesly analýzy a průzkumy o dekarbonizaci? {headline}"]
    for n, f in enumerate(findings, 1):
        posts.append(f"{n}/ {f.title_cs}: {f.text_cs} (zdroj: {f.source_name})")
    posts.append("Celý týdenní přehled se SWOT a zdroji: {ODKAZ} #klima #energetika")
    return posts


def build_digest(report: WeeklyReport, settings: Settings, client: ClaudeClient | None) -> Digest:
    notes: list[str] = []
    url = report_url(settings, report.week)
    findings: list[DigestFinding] = []
    thread: list[str] = []
    model = "deterministicky (bez AI)"
    if client is not None and report.top_items:
        # do max_tokens se počítá i „přemýšlení“ modelu – limit s rezervou, při uříznutí jeden pokus s vyšším
        for max_tokens in (12000, 24000):
            try:
                draft, _ = client.structured(
                    model=settings.llm.model_synthesis, system=prompt_path("digest.md").read_text(encoding="utf-8"),
                    user=_input_text(report), schema=DigestDraft, max_tokens=max_tokens, effort="low",
                    cache_system=False)
                findings, thread, model = draft.findings, draft.x_thread, settings.llm.model_synthesis
                break
            except LLMMaxTokens:
                log.warning("Výběr zjištění uříznut na max_tokens=%d", max_tokens)
            except LLMError as exc:
                log.error("Výběr zjištění pro e-mail selhal: %s – použiji zálohu", exc)
                notes.append(f"AI výběr selhal ({exc}); použita záloha")
                break
        else:
            notes.append("AI výběr byl uříznut na limitu délky; použita záloha")
    if not findings:
        findings = _deterministic(report)
    findings = _enforce_mix(findings, report, notes)
    items = {i.id: i for i in report.items}
    out = [DigestFindingOut(**f.model_dump(), title=items[f.item_id].title, url=items[f.item_id].url,
                            source_name=items[f.item_id].source_name) for f in findings]
    if not thread:
        thread = _default_thread(out, report.headline_cs.replace("**", ""))
    thread = [fit_x(p.replace("**", "").replace("{ODKAZ}", url)) for p in thread][:6]
    if not any(url in p for p in thread):
        thread.append(fit_x(f"Celý přehled: {url}"))
    digest = Digest(week=report.week, generated_at=datetime.now(UTC), model=model,
                    headline_cs=report.headline_cs.replace("**", ""), report_url=url, findings=out,
                    x_thread=thread, notes=notes)
    if client is not None:
        digest.usage = client.tracker.info
    return digest
