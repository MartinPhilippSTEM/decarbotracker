"""Příležitosti: vypsané výzvy a granty vhodné pro společenskovědní tým (sekce pod Událostmi)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from decarbotracker.config import Settings, prompt_path
from decarbotracker.dedup import SeenState
from decarbotracker.llm import ClaudeClient, LLMError, LLMMaxTokens
from decarbotracker.models import Item, Opportunity, OpportunityBatch
from decarbotracker.normalize import strip_diacritics

log = logging.getLogger(__name__)

# titulky zpráv, které nejspíš ohlašují výzvu (pro zdroje, které nejsou čistě grantové)
CALL_RE = re.compile(
    r"\b(vyzv|soutez|grant|dotac|call for (proposals|applications)|funding (call|opportunit)|open call|tender)", re.I)


def build_pool(items: list[Item], funding_source_ids: set[str], seen: SeenState, now: datetime,
               lookback_days: int, pool_max: int, week: str = "") -> list[Item]:
    """Kandidáti: položky z grantových zdrojů a zprávy s výzvou v titulku; bez už zobrazených."""
    since = now - timedelta(days=lookback_days)
    pool: dict[str, Item] = {}
    for it in items:
        is_funding_source = it.source_id in funding_source_ids or it.source_type == "funding"
        if not is_funding_source and not CALL_RE.search(strip_diacritics(it.title)):
            continue
        if seen.opportunities.get(it.id) not in (None, week):  # už zobrazeno v jiném týdnu
            continue
        # zprávy z feedů jen čerstvé; výzvy z EU portálu jsou vždy otevřené/ohlášené
        if it.source_type != "funding" and it.effective_date < since:
            continue
        pool.setdefault(it.id, it)
    # české zdroje první, pak ostatní; uvnitř nejnovější
    ordered = sorted(pool.values(), key=lambda i: (i.region != "CZ", i.source_type == "funding",
                                                    -i.effective_date.timestamp(), i.id))
    return ordered[:pool_max]


def _format(it: Item) -> str:
    return (f"[ID: {it.id}]\nNázev: {it.title}\nZdroj: {it.source_name} ({it.region})\n"
            f"Datum: {it.effective_date.date().isoformat()}\nText: {it.summary_raw[:1200] or '(bez textu)'}\n")


def _finalize(drafts, pool: list[Item], limit: int) -> tuple[list[Opportunity], list[str]]:
    by_id = {i.id: i for i in pool}
    out: list[Opportunity] = []
    logs: list[str] = []
    for d in drafts:
        it = by_id.get(d.item_id.strip())
        if it is None:
            logs.append(f"příležitosti: vyřazeno neexistující ID {d.item_id}")
            continue
        if any(o.item_id == it.id for o in out):
            continue
        out.append(Opportunity(**d.model_dump(), title=it.title, url=it.url, source_name=it.source_name))
    return out[:limit], logs


def find_opportunities(items: list[Item], settings: Settings, client: ClaudeClient | None, seen: SeenState,
                       funding_source_ids: set[str], now: datetime,
                       week: str = "") -> tuple[list[Opportunity], list[str]]:
    """Vrátí (příležitosti, log). Nikdy nevyhazuje výjimku – při chybě vrátí prázdný seznam."""
    cfg = settings.opportunities
    if not cfg.enabled:
        return [], []
    pool = build_pool(items, funding_source_ids, seen, now, cfg.lookback_days, cfg.pool_max, week)
    log.info("Příležitosti: %d kandidátů", len(pool))
    if not pool:
        return [], []
    if client is None:  # dry-run: ukázka bez AI
        from decarbotracker.models import OpportunityDraft

        drafts = [OpportunityDraft(item_id=i.id, title_cs=f"[Ukázka – dry-run] {i.title}", funder=i.source_name,
                                   amount_min="neuvedeno", amount_max="neuvedeno", open_from="neuvedeno",
                                   deadline="neuvedeno", summary_cs="Ukázkový výstup bez volání AI.")
                  for i in pool[:2]]
        return _finalize(drafts, pool, cfg.max_items)
    system = prompt_path("opportunities.md").read_text(encoding="utf-8")
    user = f"Dnešní datum: {now.date().isoformat()}\n\n## Položky\n\n" + "\n".join(_format(i) for i in pool)
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
    opps, logs = _finalize(parsed.opportunities, pool, cfg.max_items)
    log.info("Příležitosti: vybráno %d z %d kandidátů", len(opps), len(pool))
    return opps, logs
