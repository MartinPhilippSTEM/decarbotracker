"""Příležitosti: vypsané výzvy a granty vhodné pro STEM (sekce v části pro analytiky).

Od 20. 11. 2026:
- výzvy s uzávěrkou později než 6 měsíců od konce týdne (nebo už po uzávěrce) se nezobrazují,
- už zobrazená výzva se může znovu objevit jako připomínka, když se blíží uzávěrka,
- z jednoho programu nejvýš 3 výzvy (dřív to byl skoro jen Horizon Europe),
- AI posuzuje výzvy podle profilu STEM (config/stem_profile.md) a navrhne, jak by se STEM mohl zapojit.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from decarbotracker.config import CODE_ROOT, Settings, prompt_path
from decarbotracker.dedup import SeenState
from decarbotracker.llm import ClaudeClient, LLMError, LLMMaxTokens
from decarbotracker.models import Item, Opportunity, OpportunityBatch
from decarbotracker.normalize import strip_diacritics

log = logging.getLogger(__name__)

# titulky zpráv, které nejspíš ohlašují výzvu (pro zdroje, které nejsou čistě grantové)
CALL_RE = re.compile(
    r"\b(vyzv|soutez|grant|dotac|call for (proposals|applications|projects)|funding (call|opportunit)|open call|"
    r"joint call|calls? (is |are )?(now )?open|tender)", re.I)

MONTHS = {"ledna": 1, "unora": 2, "brezna": 3, "dubna": 4, "kvetna": 5, "cervna": 6, "cervence": 7, "srpna": 8,
          "zari": 9, "rijna": 10, "listopadu": 11, "prosince": 12,
          "leden": 1, "unor": 2, "brezen": 3, "duben": 4, "kveten": 5, "cerven": 6, "cervenec": 7, "srpen": 8,
          "rijen": 10, "listopad": 11, "prosinec": 12,
          "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
          "september": 9, "october": 10, "november": 11, "december": 12}
DEADLINE_IN_TEXT = re.compile(r"Uzávěrka:\s*([^|]+)")
MAX_PER_PROGRAMME = 3


def parse_deadline(text: str) -> date | None:
    """Datum uzávěrky z textu („31. 3. 2027“, „2027-03-31“, „31. března 2027“, „March 2027“); jinak None.
    U data jen s měsícem se bere poslední den měsíce."""
    t = strip_diacritics(str(text or "")).lower()
    if m := re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):
        y, mo, d = (int(x) for x in m.groups())
    elif m := re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", t):
        d, mo, y = (int(x) for x in m.groups())
    elif m := re.search(r"(\d{1,2})\.?\s+([a-z]+)\s+(\d{4})", t):
        if m.group(2) not in MONTHS:
            return None
        d, mo, y = int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))
    elif m := re.search(r"\b([a-z]+)\s+(\d{4})", t):
        if m.group(1) not in MONTHS:
            return None
        mo, y = MONTHS[m.group(1)], int(m.group(2))
        nxt = date(y + (mo == 12), mo % 12 + 1, 1)
        return nxt - timedelta(days=1)
    else:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


# klimatická a energeticko-transformační témata (bez diakritiky, česky i anglicky) – mají v poolu přednost
CLIMATE_RE = re.compile(
    r"(klima|energ|dekarboni|decarboni|transformac|transition|emis|uhl|coal|obnoviteln|renewable|"
    r"vytapen|heating|climate|carbon|uhlik|fosil|fossil|ets ?2|zelen|green|udrzitel|sustainab|adaptac|adaptation)",
    re.I)


def is_climate(it: Item) -> bool:
    return bool(CLIMATE_RE.search(strip_diacritics(f"{it.title} {(it.summary_raw or '')[:600]}")))


def deadline_window(ref: datetime, months: int) -> tuple[date, date]:
    """Okno uzávěrek: od data týdne do + `months` měsíců (přibližně po 30,5 dne)."""
    start = ref.date()
    return start, start + timedelta(days=round(months * 30.5))


def item_deadline(it: Item) -> date | None:
    m = DEADLINE_IN_TEXT.search(it.summary_raw or "")
    return parse_deadline(m.group(1)) if m else None


def build_pool(items: list[Item], funding_source_ids: set[str], seen: SeenState, now: datetime,
               lookback_days: int, pool_max: int, week: str = "", ref: datetime | None = None,
               max_months: int = 6, reminder_days: int = 45) -> list[Item]:
    """Kandidáti: položky z grantových zdrojů a zprávy s výzvou v titulku.
    Vyřadí výzvy s uzávěrkou mimo okno; už zobrazené jen jako připomínku blížící se uzávěrky."""
    since = now - timedelta(days=lookback_days)
    lo, hi = deadline_window(ref or now, max_months)
    pool: dict[str, Item] = {}
    for it in items:
        is_funding_source = it.source_id in funding_source_ids or it.source_type == "funding"
        if not is_funding_source and not CALL_RE.search(strip_diacritics(it.title)):
            continue
        dl = item_deadline(it)
        if dl is not None and not (lo <= dl <= hi):
            continue  # po uzávěrce, nebo uzávěrka za víc než `max_months` měsíců
        shown = seen.opportunities.get(it.id)
        if shown not in (None, week) and not (dl is not None and dl <= lo + timedelta(days=reminder_days)):
            continue  # už zobrazeno v jiném týdnu a uzávěrka není blízko
        # zprávy z feedů jen čerstvé; výzvy z EU portálu jsou vždy otevřené/ohlášené
        if it.source_type != "funding" and it.effective_date < since:
            continue
        pool.setdefault(it.id, it)

    def rank(i: Item) -> tuple:
        dl = item_deadline(i)
        ssh = "Štítek SSH" in (i.summary_raw or "")
        # klimatické a energetické výzvy první; pak české zdroje, EU výzvy se štítkem SSH, ostatní;
        # uvnitř nejbližší uzávěrka / nejnovější
        return (not is_climate(i), i.region != "CZ", i.source_type == "funding" and not ssh, i.source_type == "funding",
                dl or date.max, -i.effective_date.timestamp(), i.id)

    return sorted(pool.values(), key=rank)[:pool_max]


def _format(it: Item) -> str:
    return (f"[ID: {it.id}]\nNázev: {it.title}\nZdroj: {it.source_name} ({it.region})\n"
            f"Datum: {it.effective_date.date().isoformat()}\nText: {it.summary_raw[:1200] or '(bez textu)'}\n")


def programme(title: str, source_name: str, funder: str = "") -> str:
    """Program výzvy (pro strop na program): prefix identifikátoru EU výzvy, jinak zdroj/poskytovatel."""
    m = re.search(r"\(([A-Z0-9]+)-[A-Z0-9-]+\)\s*$", title or "")
    return m.group(1) if m else (source_name or funder or "?")


def _finalize(drafts, pool: list[Item], limit: int, lo: date | None = None,
              hi: date | None = None) -> tuple[list[Opportunity], list[str]]:
    by_id = {i.id: i for i in pool}
    out: list[Opportunity] = []
    logs: list[str] = []
    per_prog: dict[str, int] = {}
    for d in drafts:
        it = by_id.get(d.item_id.strip())
        if it is None:
            logs.append(f"příležitosti: vyřazeno neexistující ID {d.item_id}")
            continue
        if any(o.item_id == it.id for o in out):
            continue
        dl = parse_deadline(d.deadline) or item_deadline(it)
        if lo and hi and dl is not None and not (lo <= dl <= hi):
            logs.append(f"příležitosti: „{it.title[:60]}“ – uzávěrka {dl.isoformat()} mimo okno 6 měsíců, vyřazeno")
            continue
        prog = programme(it.title, it.source_name, d.funder)
        if per_prog.get(prog, 0) >= MAX_PER_PROGRAMME:
            logs.append(f"příležitosti: z programu {prog} už jsou {MAX_PER_PROGRAMME} výzvy – „{it.title[:50]}“ vynechána")
            continue
        per_prog[prog] = per_prog.get(prog, 0) + 1
        out.append(Opportunity(**d.model_dump(), title=it.title, url=it.url, source_name=it.source_name))
    return out[:limit], logs


def stem_profile() -> str:
    path = CODE_ROOT / "config" / "stem_profile.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def find_opportunities(items: list[Item], settings: Settings, client: ClaudeClient | None, seen: SeenState,
                       funding_source_ids: set[str], now: datetime,
                       week: str = "") -> tuple[list[Opportunity], list[str]]:
    """Vrátí (příležitosti, log). Nikdy nevyhazuje výjimku – při chybě vrátí prázdný seznam."""
    cfg = settings.opportunities
    if not cfg.enabled:
        return [], []
    ref = now
    if week:
        from decarbotracker.weeks import week_bounds

        ref = week_bounds(week)[1]  # okno 6 měsíců se počítá od konce daného týdne
    lo, hi = deadline_window(ref, cfg.max_deadline_months)
    pool = build_pool(items, funding_source_ids, seen, now, cfg.lookback_days, cfg.pool_max, week, ref,
                      cfg.max_deadline_months, cfg.reminder_days)
    log.info("Příležitosti: %d kandidátů (uzávěrka %s – %s)", len(pool), lo, hi)
    if not pool:
        return [], []
    if client is None:  # dry-run: ukázka bez AI
        from decarbotracker.models import OpportunityDraft

        drafts = [OpportunityDraft(item_id=i.id, title_cs=f"[Ukázka – dry-run] {i.title}", funder=i.source_name,
                                   amount_min="neuvedeno", amount_max="neuvedeno", open_from="neuvedeno",
                                   deadline="neuvedeno", summary_cs="Ukázkový výstup bez volání AI.",
                                   stem_role_cs="[Ukázka] Návrh zapojení STEM bez volání AI.")
                  for i in pool[:2]]
        return _finalize(drafts, pool, cfg.max_items, lo, hi)
    system = prompt_path("opportunities.md").read_text(encoding="utf-8") + "\n\n" + stem_profile()
    user = (f"Dnešní datum: {now.date().isoformat()}\n"
            f"Zobrazují se jen výzvy s uzávěrkou od {lo.isoformat()} do {hi.isoformat()} (nejvýš {cfg.max_deadline_months} "
            "měsíců od konce týdne); pozdější nebo prošlé nevybírej.\n\n## Položky\n\n"
            + "\n".join(_format(i) for i in pool))
    parsed = None
    # do max_tokens se počítá i „přemýšlení“ modelu – při uříznutí jeden pokus s dvojnásobným limitem
    for max_tokens in (cfg.max_tokens, cfg.max_tokens * 2):
        try:
            parsed, _ = client.structured(model=settings.llm.model_synthesis, system=system, user=user,
                                          schema=OpportunityBatch, max_tokens=max_tokens, effort=cfg.effort,
                                          cache_system=False)
            break
        except LLMMaxTokens:
            log.warning("Výběr příležitostí uříznut na max_tokens=%d", max_tokens)
        except LLMError as exc:
            log.error("Výběr příležitostí selhal: %s", exc)
            return [], [f"příležitosti: výběr selhal ({exc})"]
    if parsed is None:
        return [], ["příležitosti: odpověď byla uříznuta na limitu délky"]
    opps, logs = _finalize(parsed.opportunities, pool, cfg.max_items, lo, hi)
    log.info("Příležitosti: vybráno %d z %d kandidátů", len(opps), len(pool))
    return opps, logs
