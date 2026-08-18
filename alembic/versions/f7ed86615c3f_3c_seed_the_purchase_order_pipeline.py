"""3c: seed the purchase order pipeline

The second document type, added the way the abstraction promised: a definition
row, seeded from the same file the runtime loads. No extraction, scoring,
routing or worker code changes — the fields, kinds, rules and JSON Schema in
config/pipelines/purchase_order_v1.json are the whole of it.

Revision ID: f7ed86615c3f
Revises: 4d2da7cbc871
Create Date: 2026-08-18

"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7ed86615c3f"
down_revision: str | Sequence[str] | None = "4d2da7cbc871"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "pipelines" / "purchase_order_v1.json"
)


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO pipelines (tenant_id, slug, version, document_type, config, is_active) "
            "VALUES ('dev-tenant', 'purchase_order', 1, 'purchase order', "
            "CAST(:config AS jsonb), true) "
            "ON CONFLICT (tenant_id, slug, version) DO UPDATE SET config = EXCLUDED.config"
        ).bindparams(config=json.dumps(json.loads(CONFIG_PATH.read_text())))
    )


def downgrade() -> None:
    # Documents already processed under this definition keep their extractions;
    # only the definition goes, and only if nothing is mid-flight against it.
    op.execute(
        sa.text(
            "DELETE FROM pipelines WHERE tenant_id = 'dev-tenant' "
            "AND slug = 'purchase_order' AND version = 1"
        )
    )
