"""tenants and api keys

Tenants become real rows rather than a hardcoded string, with per-tenant SLA
and budget. API keys are stored as hashes only: the plaintext is shown once at
issue time, so a database leak cannot be replayed as valid credentials.

Seeds the existing dev-tenant so every row already carrying tenant_id =
'dev-tenant' keeps a valid owner.

Revision ID: 5c7d75127da4
Revises: f2a52e41d85c
Create Date: 2026-08-17 22:14:43.486391

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5c7d75127da4"
down_revision: str | Sequence[str] | None = "f2a52e41d85c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column(
            "review_sla_hours",
            sa.Numeric(precision=8, scale=2),
            server_default="24",
            nullable=False,
        ),
        sa.Column(
            "budget_usd", sa.Numeric(precision=12, scale=4), server_default="100", nullable=False
        ),
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
        sa.CheckConstraint("status IN ('active', 'paused')", name="ck_tenants_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "api_keys",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=12), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("key_hash", name="uq_api_keys_hash"),
    )
    op.create_index("ix_api_keys_tenant", "api_keys", ["tenant_id"], unique=False)

    # Backfill: every existing row already carries tenant_id = 'dev-tenant',
    # so the tenant must exist before anything can reference it.
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, name, status, review_sla_hours, budget_usd) "
            "VALUES ('dev-tenant', 'Development Tenant', 'active', 24, 100) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_api_keys_tenant", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("tenants")
