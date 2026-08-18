"""4a: pipelines declare their model routing policy

Both shipped definitions now name a *tier* for the first attempt and the
conditions under which a document is re-run on a stronger one. Tiers map to
concrete models in config/model_routing.json, so changing which model serves
the cheap tier never touches a pipeline.

The triggers are the deterministic checks the system already computes — a
failed validation rule, a calibrated router flag, an outright extraction
failure — so escalation happens exactly where the cheap answer is suspect.

Revision ID: b2699ca3acba
Revises: 7f3caa4f346c
Create Date: 2026-08-18

"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2699ca3acba"
down_revision: str | Sequence[str] | None = "7f3caa4f346c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "pipelines"

PREVIOUS = {
    "invoice": '{"document_type": "invoice", "prompt": {"intro": "Invoices may be in English or German (Rechnung); layouts vary and text extraction may interleave columns or split words.", "field_notes": {"tax_rate": "decimal fraction as a string (a printed \\"19%\\" becomes \\"0.19\\")", "line_items": "one entry per row of the invoice\'s item table, in document order", "vendor": "the issuing company\'s name; repair obvious text-extraction artifacts (split or spaced-out letters)"}}, "fields": {"vendor": {"kind": "text"}, "invoice_number": {"kind": "text"}, "invoice_date": {"kind": "date"}, "due_date": {"kind": "date"}, "currency": {"kind": "enum", "values": ["USD", "EUR"]}, "subtotal": {"kind": "money"}, "tax_rate": {"kind": "rate"}, "tax": {"kind": "money"}, "total": {"kind": "money", "positive": true}, "line_items": {"kind": "table", "item_fields": {"description": {"kind": "text"}, "quantity": {"kind": "quantity"}, "unit_price": {"kind": "money"}, "amount": {"kind": "money"}}}}, "validation_rules": [{"name": "line_items_sum_to_subtotal", "type": "sum_equals", "fields": ["subtotal", "line_items"], "items": "line_items", "item_field": "amount", "target": "subtotal"}, {"name": "subtotal_plus_tax_equals_total", "type": "terms_equal", "fields": ["subtotal", "tax", "total"], "terms": ["subtotal", "tax"], "target": "total"}, {"name": "tax_matches_rate", "type": "product_equals", "fields": ["subtotal", "tax_rate", "tax"], "factors": ["subtotal", "tax_rate"], "result": "tax", "tolerance": "0.02"}, {"name": "invoice_date_parses", "type": "required", "fields": ["invoice_date"]}, {"name": "due_date_not_before_invoice_date", "type": "date_order", "fields": ["due_date", "invoice_date"], "earlier": "invoice_date", "later": "due_date"}, {"name": "row_arithmetic", "type": "product_equals", "fields": ["line_items"], "items": "line_items", "factors": ["quantity", "unit_price"], "result": "amount"}], "extraction_schema": {"type": "object", "additionalProperties": false, "required": ["vendor", "invoice_number", "invoice_date", "due_date", "currency", "subtotal", "tax_rate", "tax", "total", "line_items"], "properties": {"vendor": {"type": "string", "minLength": 1, "description": "The issuing company\'s name as printed"}, "invoice_number": {"type": "string", "minLength": 1, "description": "Exactly as printed on the document"}, "invoice_date": {"type": "string", "format": "date", "description": "ISO-8601 YYYY-MM-DD"}, "due_date": {"type": "string", "format": "date", "description": "ISO-8601 YYYY-MM-DD"}, "currency": {"type": "string", "enum": ["USD", "EUR"], "description": "ISO 4217 code"}, "subtotal": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}, "tax_rate": {"type": "string", "description": "Decimal fraction as a string: 19% -> \\"0.19\\", 6.25% -> \\"0.0625\\""}, "tax": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}, "total": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}, "line_items": {"type": "array", "description": "One entry per row of the invoice\'s item table, in document order", "items": {"type": "object", "additionalProperties": false, "required": ["description", "quantity", "unit_price", "amount"], "properties": {"description": {"type": "string"}, "quantity": {"type": "string", "description": "e.g. \\"3\\""}, "unit_price": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}, "amount": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}}}}}}, "confidence": {"model_path": "config/confidence_model_v2.json"}, "sla_hours": 24}',
    "purchase_order": '{"document_type": "purchase order", "prompt": {"intro": "Purchase orders may be in English or German (Bestellung); the buyer issues the order and the vendor is the supplier it is sent to.", "field_notes": {"buyer": "the ordering organization, whose letterhead the order carries", "vendor": "the supplier the order is addressed to", "shipping": "the freight or delivery charge as a separate amount, \\"0\\" if the order is free of it", "line_items": "one entry per row of the order\'s item table, in document order"}}, "fields": {"buyer": {"kind": "text"}, "vendor": {"kind": "text"}, "po_number": {"kind": "text"}, "order_date": {"kind": "date"}, "delivery_date": {"kind": "date"}, "currency": {"kind": "enum", "values": ["USD", "EUR"]}, "subtotal": {"kind": "money"}, "shipping": {"kind": "money"}, "total": {"kind": "money", "positive": true}, "line_items": {"kind": "table", "item_fields": {"description": {"kind": "text"}, "quantity": {"kind": "quantity"}, "unit_price": {"kind": "money"}, "amount": {"kind": "money"}}}}, "validation_rules": [{"name": "line_items_sum_to_subtotal", "type": "sum_equals", "fields": ["subtotal", "line_items"], "items": "line_items", "item_field": "amount", "target": "subtotal"}, {"name": "subtotal_plus_shipping_equals_total", "type": "terms_equal", "fields": ["subtotal", "shipping", "total"], "terms": ["subtotal", "shipping"], "target": "total"}, {"name": "order_date_parses", "type": "required", "fields": ["order_date"]}, {"name": "delivery_not_before_order_date", "type": "date_order", "fields": ["delivery_date", "order_date"], "earlier": "order_date", "later": "delivery_date"}, {"name": "po_number_format", "type": "regex", "fields": ["po_number"], "field": "po_number", "pattern": "PO-\\\\d{4}-\\\\d{5}|BA-\\\\d{4}/\\\\d{4}"}, {"name": "row_arithmetic", "type": "product_equals", "fields": ["line_items"], "items": "line_items", "factors": ["quantity", "unit_price"], "result": "amount"}], "extraction_schema": {"type": "object", "additionalProperties": false, "required": ["buyer", "vendor", "po_number", "order_date", "delivery_date", "currency", "subtotal", "shipping", "total", "line_items"], "properties": {"buyer": {"type": "string", "minLength": 1, "description": "The ordering organization as printed"}, "vendor": {"type": "string", "minLength": 1, "description": "The supplier the order is addressed to"}, "po_number": {"type": "string", "minLength": 1, "description": "Exactly as printed on the document"}, "order_date": {"type": "string", "format": "date", "description": "ISO-8601 YYYY-MM-DD"}, "delivery_date": {"type": "string", "format": "date", "description": "ISO-8601 YYYY-MM-DD"}, "currency": {"type": "string", "enum": ["USD", "EUR"], "description": "ISO 4217 code"}, "subtotal": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}, "shipping": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}, "total": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}, "line_items": {"type": "array", "description": "One entry per row of the order\'s item table, in document order", "items": {"type": "object", "additionalProperties": false, "required": ["description", "quantity", "unit_price", "amount"], "properties": {"description": {"type": "string"}, "quantity": {"type": "string", "description": "e.g. \\"3\\""}, "unit_price": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}, "amount": {"type": "string", "description": "Plain decimal string: \'.\' as decimal separator, no thousands separators, no currency symbols. e.g. \\"25832.09\\""}}}}}}, "sla_hours": 48}',
}

FILES = {"invoice": "invoice_v1.json", "purchase_order": "purchase_order_v1.json"}

_UPDATE = (
    "UPDATE pipelines SET config = CAST(:config AS jsonb), updated_at = now() "
    "WHERE slug = :slug AND version = 1"
)


def upgrade() -> None:
    for slug, filename in FILES.items():
        config = json.dumps(json.loads((CONFIG_DIR / filename).read_text()))
        op.execute(sa.text(_UPDATE).bindparams(slug=slug, config=config))


def downgrade() -> None:
    for slug, config in PREVIOUS.items():
        op.execute(sa.text(_UPDATE).bindparams(slug=slug, config=config))
