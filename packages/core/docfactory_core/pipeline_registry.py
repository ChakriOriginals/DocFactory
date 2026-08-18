"""Loading pipeline definitions.

A definition normally comes from the tenant's `pipelines` row. The invoice
definition also ships as a file so the system boots with a working pipeline
and so the Phase 1-2 behaviour has a single, inspectable source of truth
rather than being spread across Python constants.
"""

import json
from functools import lru_cache
from pathlib import Path

from docfactory_core.pipeline import PipelineDefinition, parse_definition

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "pipelines"


@lru_cache
def load_from_file(
    slug: str, version: int = 1, tenant_id: str = "dev-tenant"
) -> PipelineDefinition:
    path = CONFIG_DIR / f"{slug}_v{version}.json"
    return parse_definition(tenant_id, slug, version, json.loads(path.read_text()))


@lru_cache
def default_pipeline() -> PipelineDefinition:
    """The invoice pipeline, used when a caller does not name one."""
    return load_from_file("invoice", 1)
