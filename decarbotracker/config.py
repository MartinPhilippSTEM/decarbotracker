"""Načtení konfigurace z config/*.yaml a proměnných prostředí."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from decarbotracker.models import Source


def project_root() -> Path:
    """Kořen repozitáře (lze přepsat proměnnou DECARBO_ROOT, např. v testech)."""
    env = os.environ.get("DECARBO_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


CODE_ROOT = Path(__file__).resolve().parent.parent  # šablony, static, prompty jsou vždy u kódu


class SiteSettings(BaseModel):
    title: str = "decarbotracker"
    tagline: str = "Týdenní přehled analýz, studií a průzkumů o dekarbonizaci"
    base_url: str = "/"
    site_url: str = "http://localhost:8000/"
    repo_url: str = "https://github.com/"
    output_dir: str = "site"
    archive_weeks_on_home: int = 8


class ModelPrice(BaseModel):
    input: float
    output: float
    cache_write: float | None = None
    cache_read: float | None = None


class LLMSettings(BaseModel):
    model_scoring: str = "claude-haiku-4-5"
    model_scoring_fallback: str = "claude-sonnet-5-5"
    model_synthesis: str = "claude-sonnet-5-5"
    model_synthesis_fallback: str = "claude-opus-5-5"
    server_side_fallback: bool = True
    # modely, které přijímají output_config.effort (Haiku 4.5 ne)
    effort_models: list[str] = Field(default_factory=lambda: ["claude-sonnet-5-5", "claude-opus-5-5"])
    synthesis_effort: str = "medium"
    scoring_effort: str = "low"
    synthesis_max_tokens: int = 16000
    synthesis_max_tokens_retry: int = 32000
    scoring_max_tokens: int = 8000
    scoring_batch_size: int = 25
    scoring_summary_chars: int = 450
    synthesis_summary_chars: int = 900
    max_items_scoring: int = 320
    max_cost_usd: float = 1.0
    scoring_budget_share: float = 0.45
    expected_synthesis_output_tokens: int = 14000
    timeout_s: float = 600.0
    max_retries: int = 4
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)


class SelectionSettings(BaseModel):
    max_items_synthesis: int = 40
    min_cz: int = 5
    min_attitudes: int = 5
    geo_weights: dict[str, float] = Field(
        default_factory=lambda: {"CZ": 1.0, "EU": 0.7, "US": 0.4, "GLOBAL": 0.5}
    )
    global_relevance_bonus: float = 0.2
    source_type_weights: dict[str, float] = Field(default_factory=dict)
    bonus_attitudes: float = 0.30
    bonus_original_research: float = 0.15
    opinion_penalty: float = 0.10
    fuzzy_title_threshold: int = 90
    undated_first_run_cap: int = 3
    seen_retention_weeks: int = 30


class FetchSettings(BaseModel):
    connect_timeout: float = 10.0
    read_timeout: float = 20.0
    max_workers: int = 8
    stale_days: int = 60
    max_retries: int = 2
    max_items_per_source: int = 60


class JournalSettings(BaseModel):
    name: str
    issn: list[str]
    topic_filter: bool = False


class AcademicSettings(BaseModel):
    enabled: bool = True
    max_requests: int = 50
    openalex_no_key_max_requests: int = 4
    openalex_per_page: int = 50
    crossref_rows: int = 100
    lookback_days: int = 7
    journals: list[JournalSettings] = Field(default_factory=list)
    openalex_queries: list[dict[str, str]] = Field(default_factory=list)


class AskSettings(BaseModel):
    default_days: int = 14
    max_items: int = 30


class Settings(BaseModel):
    site: SiteSettings = Field(default_factory=SiteSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    selection: SelectionSettings = Field(default_factory=SelectionSettings)
    fetch: FetchSettings = Field(default_factory=FetchSettings)
    academic: AcademicSettings = Field(default_factory=AcademicSettings)
    ask: AskSettings = Field(default_factory=AskSettings)
    contact_email: str = ""
    user_agent: str = "decarbotracker/1.0 (+{repo_url}; vyzkumny agregator)"

    # tajné hodnoty – jen z prostředí, nikdy se neukládají ani nelogují
    anthropic_api_key: str | None = Field(default=None, exclude=True, repr=False)
    openalex_api_key: str | None = Field(default=None, exclude=True, repr=False)
    # ID pracovního prostoru (wrkspc_…) – nutné u klíčů, které nejsou přiřazené k workspace
    anthropic_workspace_id: str | None = Field(default=None, exclude=True, repr=False)

    @property
    def ua(self) -> str:
        ua = self.user_agent.format(repo_url=self.site.repo_url)
        return ua.encode("ascii", "ignore").decode("ascii")


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def normalize_base_url(base: str) -> str:
    base = (base or "/").strip()
    if not base.startswith("/"):
        base = "/" + base
    if not base.endswith("/"):
        base += "/"
    return base


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    load_dotenv(project_root() / ".env")
    data = _read_yaml(CODE_ROOT / "config" / "settings.yaml")
    settings = Settings.model_validate(data)
    if os.environ.get("DECARBO_BASE_URL"):
        settings.site.base_url = os.environ["DECARBO_BASE_URL"]
    if os.environ.get("DECARBO_SITE_URL"):
        settings.site.site_url = os.environ["DECARBO_SITE_URL"]
    if os.environ.get("DECARBO_REPO_URL"):
        settings.site.repo_url = os.environ["DECARBO_REPO_URL"]
    settings.site.base_url = normalize_base_url(settings.site.base_url)
    if not settings.site.site_url.endswith("/"):
        settings.site.site_url += "/"
    settings.contact_email = os.environ.get("CONTACT_EMAIL", settings.contact_email) or ""
    # odstraň mezery, nové řádky a uvozovky, které se často přidají při vkládání klíče
    settings.anthropic_api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip().strip("\"'").strip() or None
    settings.openalex_api_key = os.environ.get("OPENALEX_API_KEY") or None
    settings.anthropic_workspace_id = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip() or None
    return settings


def load_sources(include_disabled: bool = True) -> list[Source]:
    data = _read_yaml(CODE_ROOT / "config" / "sources.yaml")
    sources = [Source.model_validate(s) for s in data.get("sources", [])]
    ids = [s.id for s in sources]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"Duplicitní id zdrojů v sources.yaml: {sorted(dupes)}")
    return sources if include_disabled else [s for s in sources if s.enabled]


@lru_cache(maxsize=1)
def load_keywords() -> dict[str, Any]:
    return _read_yaml(CODE_ROOT / "config" / "keywords.yaml")


def data_dir() -> Path:
    return project_root() / "data"


def prompt_path(name: str) -> Path:
    return CODE_ROOT / "prompts" / name
