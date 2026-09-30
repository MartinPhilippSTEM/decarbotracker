"""Tenká vrstva nad Claude API: strukturovaný výstup, stop_reason, fallbacky, tokeny a cena."""

from __future__ import annotations

import logging
from typing import Any, TypeVar

import anthropic
from anthropic import transform_schema
from pydantic import BaseModel, ValidationError

from decarbotracker.config import LLMSettings, Settings
from decarbotracker.models import UsageInfo

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """Obecná chyba volání LLM."""


class LLMRefusal(LLMError):
    pass


class LLMMaxTokens(LLMError):
    pass


class LLMValidationError(LLMError):
    def __init__(self, message: str, raw_text: str):
        super().__init__(message)
        self.raw_text = raw_text


class LLMModelUnavailable(LLMError):
    pass


class LLMAuthError(LLMError):
    """Neplatný klíč / chybějící oprávnění / došel kredit – další volání nemají smysl."""


def describe_key(key: str | None) -> str:
    """Bezpečný popis klíče (bez jeho hodnoty) pro diagnostiku."""
    if not key:
        return "klíč chybí (prázdná hodnota)"
    problems = []
    if key != key.strip():
        problems.append("na začátku/konci je mezera nebo nový řádek")
    if any(c in key for c in "\"'"):
        problems.append("obsahuje uvozovky")
    if "=" in key:
        problems.append("obsahuje '=' (nevložil se i název proměnné?)")
    if "..." in key or "…" in key:
        problems.append("obsahuje '...' (zkopírována zkrácená verze ze seznamu klíčů?)")
    ok_prefix = key.strip().startswith("sk-ant-api")
    return (f"délka {len(key)} znaků, začíná 'sk-ant-api': {'ano' if ok_prefix else 'NE'}"
            + (f"; problémy: {', '.join(problems)}" if problems else "; formát vypadá v pořádku"))


class UsageTracker:
    """Sčítá tokeny a odhadovanou cenu za běh."""

    def __init__(self, llm: LLMSettings):
        self.llm = llm
        self.info = UsageInfo()

    def price(self, model: str):
        if model in self.llm.pricing:
            return self.llm.pricing[model]
        for name, price in self.llm.pricing.items():  # např. "claude-sonnet-5-5-2026…" → prefix
            if model.startswith(name):
                return price
        # neznámý model: počítej konzervativně cenou nejdražšího známého
        return max(self.llm.pricing.values(), key=lambda p: p.output) if self.llm.pricing else None

    def cost(self, model: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0) -> float:
        p = self.price(model)
        if p is None:
            return 0.0
        cw = p.cache_write if p.cache_write is not None else p.input * 1.25
        cr = p.cache_read if p.cache_read is not None else p.input * 0.1
        return (input_tokens * p.input + output_tokens * p.output + cache_write * cw + cache_read * cr) / 1_000_000

    def add(self, model: str, usage: Any) -> float:
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        c = self.cost(model, inp, out, cw, cr)
        i = self.info
        i.calls += 1
        i.input_tokens += inp
        i.output_tokens += out
        i.cache_creation_input_tokens += cw
        i.cache_read_input_tokens += cr
        i.cost_usd = round(i.cost_usd + c, 6)
        m = i.by_model.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        m["calls"] += 1
        m["input_tokens"] += inp + cw + cr
        m["output_tokens"] += out
        m["cost_usd"] = round(m["cost_usd"] + c, 6)
        log.info("LLM %s: vstup %d (+cache zápis %d, čtení %d), výstup %d tokenů, ≈ $%.4f (celkem $%.4f)",
                 model, inp, cw, cr, out, c, i.cost_usd)
        return c

    @property
    def spent(self) -> float:
        return self.info.cost_usd


def _text_of(message: Any) -> str:
    return "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text")


class ClaudeClient:
    """Volání Claude API se strukturovaným výstupem (output_config.format) přes streaming."""

    def __init__(self, settings: Settings, tracker: UsageTracker | None = None):
        if not settings.anthropic_api_key:
            raise LLMError("Chybí ANTHROPIC_API_KEY (nastavte ho v .env nebo v GitHub Secrets)")
        self.settings = settings
        self.llm = settings.llm
        self.tracker = tracker or UsageTracker(settings.llm)
        self.client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            max_retries=self.llm.max_retries,
            timeout=self.llm.timeout_s,
        )

    def _kwargs(self, model: str, system: str, user: str, schema: type[BaseModel], max_tokens: int,
                effort: str | None, cache_system: bool) -> dict[str, Any]:
        system_block: dict[str, Any] = {"type": "text", "text": system}
        if cache_system:
            system_block["cache_control"] = {"type": "ephemeral"}
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": transform_schema(schema)}}
        if effort and model in self.llm.effort_models:
            output_config["effort"] = effort
        # Pozn.: temperature/top_p/top_k se nepoužívají (novější modely vrací 400, SDK 1.x je odstranilo).
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": [system_block],
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }

    def structured(self, *, model: str, system: str, user: str, schema: type[T], max_tokens: int,
                   effort: str | None = None, cache_system: bool = True,
                   server_fallback: bool = False) -> tuple[T, Any]:
        kwargs = self._kwargs(model, system, user, schema, max_tokens, effort, cache_system)
        try:
            if server_fallback:
                try:
                    with self.client.beta.messages.stream(
                        **kwargs, betas=[SERVER_FALLBACK_BETA], fallbacks="default"
                    ) as stream:
                        message = stream.get_final_message()
                except anthropic.BadRequestError as exc:
                    if "fallback" not in str(exc).lower():
                        raise
                    log.warning("Serverový fallback není pro %s dostupný, volám bez něj", model)
                    with self.client.messages.stream(**kwargs) as stream:
                        message = stream.get_final_message()
            else:
                with self.client.messages.stream(**kwargs) as stream:
                    message = stream.get_final_message()
        except anthropic.NotFoundError as exc:
            raise LLMModelUnavailable(f"Model {model} není dostupný: {exc.message}") from exc
        except anthropic.AuthenticationError as exc:
            raise LLMAuthError(
                f"Neplatný ANTHROPIC_API_KEY (401: {exc.message}). Klíč: {describe_key(self.settings.anthropic_api_key)}. "
                "Zkontrolujte klíč v .env / GitHub Secrets."
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMAuthError(f"Klíč nemá oprávnění (403): {exc.message}") from exc
        except anthropic.BadRequestError as exc:
            msg = exc.message
            if "credit balance" in msg.lower():
                raise LLMAuthError("Na účtu Anthropic došel kredit – dobijte ho v Console → Billing.") from exc
            raise LLMError(f"Chybný požadavek (400): {msg}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Síťová chyba při volání Claude API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Chyba Claude API ({exc.status_code}): {exc.message}") from exc

        used_model = getattr(message, "model", model) or model
        self.tracker.add(used_model, message.usage)
        stop = getattr(message, "stop_reason", None)
        if stop == "refusal":
            details = getattr(message, "stop_details", None)
            cat = getattr(details, "category", None) if details else None
            raise LLMRefusal(f"Model odmítl odpovědět (kategorie: {cat})")
        if stop == "max_tokens":
            raise LLMMaxTokens(f"Odpověď byla uříznuta na limitu max_tokens={max_tokens}")
        text = _text_of(message)
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError as exc:
            raise LLMValidationError(f"Výstup neodpovídá schématu: {exc.error_count()} chyb; {str(exc)[:600]}", text) from exc
        return parsed, message

    def count_tokens(self, *, model: str, system: str, user: str, schema: type[BaseModel]) -> int:
        """Přesný počet vstupních tokenů (count_tokens API); při chybě hrubý odhad."""
        try:
            res = self.client.messages.count_tokens(
                model=model,
                system=[{"type": "text", "text": system}],
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": transform_schema(schema)}},
            )
            return int(res.input_tokens)
        except anthropic.APIError as exc:
            log.warning("count_tokens selhal (%s), používám odhad podle délky textu", exc)
            return estimate_tokens(system + user) + 1500


def estimate_tokens(text: str) -> int:
    """Hrubý odhad: ~3,2 znaku na token (čeština + angličtina)."""
    return int(len(text) / 3.2) + 1
