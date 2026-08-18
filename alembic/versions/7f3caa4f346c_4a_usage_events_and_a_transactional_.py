"""4a: usage events and a transactional tenant spend counter

Two tables, both tenant-scoped and both under RLS.

`usage_events` is the metering audit trail: one row per model call, with the
provider's own token counts priced by config/model_pricing.json. Its foreign
keys are ON DELETE SET NULL, not CASCADE: deleting a document must not delete
the record that money was spent on it, or the audit trail and the spend counter
would drift apart the first time anything was cleaned up. It is separate
from `extractions` because one extraction can involve several calls (routing
escalates) and because a call that produced nothing still cost money.

`tenant_spend` is the enforcement point for the budget cap. Summing usage
events gives the same number, but a cap enforced that way races: two workers
both read a spend under the cap and both charge. A single conditional UPDATE
against this row serializes them — the second worker re-evaluates the cap
while holding the row lock and loses cleanly. Every existing tenant gets a row
back-filled from its extractions so no spend is forgotten at the cutover.

Revision ID: 7f3caa4f346c
Revises: 717db8a4aa65
Create Date: 2026-08-18

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f3caa4f346c"
down_revision: str | Sequence[str] | None = "717db8a4aa65"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "docfactory_app"
NEW_TENANT_TABLES = ("usage_events", "tenant_spend")


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("extraction_id", sa.Uuid(), nullable=True),
        sa.Column("pipeline_slug", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("model_tier", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["extraction_id"], ["extractions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_usage_events_tenant_created", "usage_events", ["tenant_id", "created_at"], unique=False
    )
    op.create_index("ix_usage_events_document", "usage_events", ["document_id"], unique=False)

    op.create_table(
        "tenant_spend",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("spent_usd", sa.Numeric(14, 6), server_default="0", nullable=False),
        sa.Column("unsettled_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    # Same isolation guarantees as every other tenant-scoped table: enabled AND
    # forced, so the table owner obeys its own policy too.
    for table in NEW_TENANT_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"""
                CREATE POLICY tenant_isolation ON {table}
                    USING (tenant_id = current_setting('app.tenant_id', true))
                    WITH CHECK (tenant_id = current_setting('app.tenant_id', true))
                """
            )
        )
        op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"))

    # Carry existing spend across the cutover: the counter starts where the
    # per-call charges left off, so raising a cap does not silently forgive
    # money already spent.
    op.execute(
        sa.text(
            "INSERT INTO tenant_spend (tenant_id, spent_usd) "
            "SELECT t.id, COALESCE(SUM(e.cost_usd), 0) "
            "FROM tenants t LEFT JOIN extractions e ON e.tenant_id = t.id "
            "GROUP BY t.id "
            "ON CONFLICT (tenant_id) DO NOTHING"
        )
    )


def downgrade() -> None:
    for table in NEW_TENANT_TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    op.drop_table("tenant_spend")
    op.drop_index("ix_usage_events_document", table_name="usage_events")
    op.drop_index("ix_usage_events_tenant_created", table_name="usage_events")
    op.drop_table("usage_events")
