"""Schémata pro strukturovaný výstup nesmí mít volitelná pole.

Volitelné vlastnosti násobí velikost gramatiky, kterou API kompiluje; při překročení limitu vrací
400 „The compiled grammar is too large“ (stalo se 30. 9. 2026 po přidání volitelného short_cs).
"""

import pytest
from anthropic import transform_schema

from decarbotracker.models import BriefDraft, OpportunityBatch, ReportDraft, ScoreBatch


def _objects(schema: dict):
    yield schema
    for d in (schema.get("$defs") or {}).values():
        yield from _objects(d)
    for p in (schema.get("properties") or {}).values():
        yield from _objects(p)
    if isinstance(schema.get("items"), dict):
        yield from _objects(schema["items"])


@pytest.mark.parametrize("model", [ReportDraft, BriefDraft, ScoreBatch, OpportunityBatch], ids=lambda m: m.__name__)
def test_structured_output_schema_has_no_optional_properties(model):
    schema = transform_schema(model)
    for obj in _objects(schema):
        props = set((obj.get("properties") or {}).keys())
        required = set(obj.get("required") or [])
        assert props <= required, f"{model.__name__}: volitelná pole {sorted(props - required)}"


def test_report_schema_size_budget():
    # hrubá pojistka proti dalšímu nafukování schématu (počet vlastností napříč objekty)
    schema = transform_schema(ReportDraft)
    n_props = sum(len(o.get("properties") or {}) for o in _objects(schema))
    assert n_props <= 60, f"schéma ReportDraft má {n_props} vlastností – hrozí 'compiled grammar is too large'"
