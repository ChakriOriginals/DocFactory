"""4a: record which pipeline a confidence model was calibrated for

`PipelineDefinition.confidence_model_path` was parsed and ignored: routing
loaded the one model named in settings, so purchase orders were scored by
weights fitted on invoices without anything saying so. Routing now loads the
model the pipeline names, and records on every extraction whether those
weights were fitted on this document type ("invoice") or borrowed from
another ("borrowed:invoice").

The invoice definition is refreshed to name its model file explicitly.

Revision ID: 717db8a4aa65
Revises: f7ed86615c3f
Create Date: 2026-08-18

"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "717db8a4aa65"
down_revision: str | Sequence[str] | None = "f7ed86615c3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "pipelines" / "invoice_v1.json"
PREVIOUS_INVOICE_CONFIG = '{"document_type": "invoice", "prompt": {"intro": "Invoices may be in English or German (Rechnung); layouts vary and text extraction may interleave columns or split words.", "field_notes": {"tax_rate": "decimal fraction as a string (a printed \\"19%\\" becomes \\"0.19\\")", "line_items": "one entry per row of the invoice\'s item table, in document order", "vendor": "the issuing company\'s name; repair obvious text-extraction artifacts (split or spaced-out letters)"}}, "fields": {"vendor": {"kind": "text"}, "invoice_number": {"kind": "text"}, "invoice_date": {"kind": "date"}, "due_date": {"kind": "date"}, "currency": {"kind": "enum", "values": ["USD", "EUR"]}, "subtotal": {"kind": "money"}, "tax_rate": {"kind": "rate"}, "tax": {"kind": "money"}, "total": {"kind": "money", "positive": true}, "line_items": {"kind": "table", "item_fields": {"description": {"kind": "text"}, "quantity": {"kind": "quantity"}, "unit_price": {"kind": "money"}, "amount": {"kind": "money"}}}}, "validation_rules": [{"name": "line_items_sum_to_subtotal", "type": "sum_equals", "fields": ["subtotal", "line_items"], "items": "line_items", "item_field": "amount", "target": "subtotal"}, {"name": "subtotal_plus_tax_equals_total", "type": "terms_equal", "fields": ["subtotal", "tax", "total"], "terms": ["subtotal", "tax"], "target": "total"}, {"name": "tax_matches_rate", "type": "product_equals", "fields": ["subtotal", "tax_rate", "tax"], "factors": ["subtotal", "tax_rate"], "result": "tax", "tolerance": "0.02"}, {"name": "invoice_date_parses", "type": "required", "fields": ["invoice_date"]}, {"name": "due_date_not_before_invoice_date", "type": "date_order", "fields": ["due_date", "invoice_date"], "earlier": "invoice_date", "later": "due_date"}, {"name": "row_arithmetic", "type": "product_equals", "fields": ["line_items"], "items": "line_items", "factors": ["quantity", "unit_price"], "result": "amount"}], "extraction_schema": {"type": "object", "additionalProperties": false, "required": ["vendor", "invoice_number", "invoice_date", "due_date", "currency", "subtotal", "tax_rate", "tax", "total", "line_items"], "properties": {"vendor": {"type": "string", "minLength": 1, "description": "The issuing company\'s name as printed"}, "invoice_number": {"type": "string", "minLength": 1, "description": "Exactly as printed on the document"}, "invoice_date": {"type": "string", "format": "date", "description": "ISO-8601 YYYY-MM-DD"}, "due_date": {"type": "string", "format": "date", "description": "ISO-8601 YYYY-MM-DD"}, "currency": {"type": "string", "enum": ["USD", "EUR"], "description": "ISO 4217 code"}, "subtotal": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}, "tax_rate": {"type": "string", "description": "Decimal fraction as a string: 19% -> \\"0.19\\", 6.25% -> \\"0.0625\\""}, "tax": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}, "total": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}, "line_items": {"type": "array", "description": "One entry per row of the invoice\'s item table, in document order", "items": {"type": "object", "additionalProperties": false, "required": ["description", "quantity", "unit_price", "amount"], "properties": {"description": {"type": "string"}, "quantity": {"type": "string", "description": "e.g. \\"3\\""}, "unit_price": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}, "amount": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}}}}}}, "sla_hours": 24}'

_UPDATE = (
    "UPDATE pipelines SET config = CAST(:config AS jsonb), updated_at = now() "
    "WHERE slug = 'invoice' AND version = 1"
)


def upgrade() -> None:
    op.add_column("extractions", sa.Column("confidence_calibration", sa.Text(), nullable=True))
    op.execute(sa.text(_UPDATE).bindparams(config=json.dumps(json.loads(CONFIG_PATH.read_text()))))


def downgrade() -> None:
    op.execute(sa.text(_UPDATE).bindparams(config=PREVIOUS_INVOICE_CONFIG))
    op.drop_column("extractions", "confidence_calibration")
