"""3c: invoice definition owns its extraction schema

Phase 3b moved the invoice's fields, kinds and rules into config but left the
JSON Schema sent to the model — and the Pydantic model validating the reply —
in Python. 3c makes `run_extraction` schema-driven, so the definition now
carries the full extraction schema, the prompt guidance, and the two rules the
hardcoded validator had that the config did not (`invoice_date_parses`), plus
the `positive` constraint on the total.

The stored config is refreshed from the file the runtime loads, which is what
keeps the seed and the code from drifting. The rule *results* are unchanged —
the frozen expectations in tests/test_pipeline.py are the guard.

Revision ID: 4d2da7cbc871
Revises: 3b69ec4f8a95
Create Date: 2026-08-18

"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d2da7cbc871"
down_revision: str | Sequence[str] | None = "3b69ec4f8a95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "pipelines" / "invoice_v1.json"

# The definition as 3b seeded it, so the downgrade restores what it replaced
# rather than leaving a newer config behind an older schema.
PREVIOUS_CONFIG = '{"document_type": "invoice", "fields": {"vendor": {"kind": "text"}, "invoice_number": {"kind": "text"}, "invoice_date": {"kind": "date"}, "due_date": {"kind": "date"}, "currency": {"kind": "enum", "values": ["USD", "EUR"]}, "subtotal": {"kind": "money"}, "tax_rate": {"kind": "rate"}, "tax": {"kind": "money"}, "total": {"kind": "money"}, "line_items": {"kind": "table", "item_fields": {"description": {"kind": "text"}, "quantity": {"kind": "quantity"}, "unit_price": {"kind": "money"}, "amount": {"kind": "money"}}}}, "validation_rules": [{"name": "line_items_sum_to_subtotal", "type": "sum_equals", "fields": ["subtotal", "line_items"], "items": "line_items", "item_field": "amount", "target": "subtotal"}, {"name": "subtotal_plus_tax_equals_total", "type": "terms_equal", "fields": ["subtotal", "tax", "total"], "terms": ["subtotal", "tax"], "target": "total"}, {"name": "tax_matches_rate", "type": "product_equals", "fields": ["subtotal", "tax_rate", "tax"], "factors": ["subtotal", "tax_rate"], "result": "tax", "tolerance": "0.02"}, {"name": "due_date_not_before_invoice_date", "type": "date_order", "fields": ["due_date", "invoice_date"], "earlier": "invoice_date", "later": "due_date"}, {"name": "row_arithmetic", "type": "product_equals", "fields": ["line_items"], "items": "line_items", "factors": ["quantity", "unit_price"], "result": "amount"}], "extraction_schema": {"type": "object", "additionalProperties": false, "required": ["vendor", "invoice_number", "invoice_date", "due_date", "currency", "subtotal", "tax_rate", "tax", "total", "line_items"], "properties": {"vendor": {"type": "string"}, "invoice_number": {"type": "string"}, "invoice_date": {"type": "string"}, "due_date": {"type": "string"}, "currency": {"type": "string", "enum": ["USD", "EUR"]}, "subtotal": {"type": "string"}, "tax_rate": {"type": "string"}, "tax": {"type": "string"}, "total": {"type": "string"}, "line_items": {"type": "array", "items": {"type": "object"}}}}, "sla_hours": 24}'

_UPDATE = (
    "UPDATE pipelines SET config = CAST(:config AS jsonb), updated_at = now() "
    "WHERE slug = 'invoice' AND version = 1"
)


def upgrade() -> None:
    op.execute(sa.text(_UPDATE).bindparams(config=json.dumps(json.loads(CONFIG_PATH.read_text()))))


def downgrade() -> None:
    op.execute(sa.text(_UPDATE).bindparams(config=PREVIOUS_CONFIG))
