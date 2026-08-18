"""pipelines as config

Document types become tenant-owned, versioned definitions instead of Python
constants. The existing invoice behaviour is seeded from
config/pipelines/invoice_v1.json — the same file the runtime loads — so the
migration cannot drift from what the code actually runs.

Revision ID: 3b69ec4f8a95
Revises: a1b2c3d4e5f6
Create Date: 2026-08-17 22:35:50.927465

"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b69ec4f8a95"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipelines",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "slug", "version", name="uq_pipelines_tenant_slug_version"
        ),
    )
    op.create_index(
        "ix_pipelines_tenant_active", "pipelines", ["tenant_id", "slug", "is_active"], unique=False
    )
    op.add_column("extractions", sa.Column("pipeline_slug", sa.Text(), nullable=True))
    op.add_column("extractions", sa.Column("pipeline_version", sa.Integer(), nullable=True))

    # Seed the invoice pipeline from the file the runtime loads, so there is
    # one source of truth rather than a copy pasted into a migration.
    config_path = Path(__file__).resolve().parents[2] / "config" / "pipelines" / "invoice_v1.json"
    op.execute(
        sa.text(
            "INSERT INTO pipelines (tenant_id, slug, version, document_type, config, is_active) "
            "VALUES ('dev-tenant', 'invoice', 1, 'invoice', CAST(:config AS jsonb), true) "
            "ON CONFLICT (tenant_id, slug, version) DO NOTHING"
        ).bindparams(config=json.dumps(json.loads(config_path.read_text())))
    )


def downgrade() -> None:
    op.drop_column("extractions", "pipeline_version")
    op.drop_column("extractions", "pipeline_slug")
    op.drop_index("ix_pipelines_tenant_active", table_name="pipelines")
    op.drop_table("pipelines")
