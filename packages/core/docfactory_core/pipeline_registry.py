"""Loading pipeline definitions.

A definition normally comes from the tenant's `pipelines` row. Each definition
also ships as a file so the system boots with a working pipeline and so the
Phase 1-2 behaviour has a single, inspectable source of truth rather than
being spread across Python constants.

The slug is caller-supplied — it arrives as a document's `doc_type` from the
API — so it is validated against the definitions that actually exist before it
is ever used to build a path. An unknown or malformed slug is a clean error at
the boundary, never a filesystem read.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

from docfactory_core.pipeline import PipelineConfigError, PipelineDefinition, parse_definition

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "pipelines"

_SLUG = re.compile(r"[a-z][a-z0-9_]*")
_FILENAME = re.compile(r"(?P<slug>[a-z][a-z0-9_]*)_v(?P<version>\d+)\.json")


@lru_cache
def load_from_file(
    slug: str, version: int = 1, tenant_id: str = "dev-tenant"
) -> PipelineDefinition:
    if not _SLUG.fullmatch(slug):
        raise PipelineConfigError(f"invalid pipeline slug: {slug!r}")
    path = CONFIG_DIR / f"{slug}_v{version}.json"
    if not path.is_file():
        raise PipelineConfigError(f"no pipeline definition for {slug!r} v{version}")
    return parse_definition(tenant_id, slug, version, json.loads(path.read_text()))


@lru_cache
def available_slugs() -> tuple[str, ...]:
    """Every document type this deployment can process, newest version first."""
    matches = (_FILENAME.fullmatch(path.name) for path in sorted(CONFIG_DIR.glob("*.json")))
    return tuple(sorted({match["slug"] for match in matches if match}))


@lru_cache
def default_pipeline() -> PipelineDefinition:
    """The invoice pipeline, used when a caller does not name one."""
    return load_from_file("invoice", 1)
