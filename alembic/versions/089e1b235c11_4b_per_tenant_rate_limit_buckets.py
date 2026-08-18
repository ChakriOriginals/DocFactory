"""4b: per-tenant rate limit buckets

A token bucket per tenant, in the database rather than in a process. Two API
replicas each holding their own bucket would each allow the full rate, so the
limit has to be shared state; and it is spent with a conditional UPDATE for the
same reason the budget counter is — check and take must not be separable.

Tenant-scoped, so RLS applies like everywhere else.

Revision ID: 089e1b235c11
Revises: b2699ca3acba
Create Date: 2026-08-18

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "089e1b235c11"
down_revision: str | Sequence[str] | None = "b2699ca3acba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "docfactory_app"


def upgrade() -> None:
    op.create_table(
        "tenant_rate_limits",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("tokens", sa.Numeric(10, 4), server_default="0", nullable=False),
        sa.Column(
            "refilled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    op.execute(sa.text("ALTER TABLE tenant_rate_limits ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE tenant_rate_limits FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """
            CREATE POLICY tenant_isolation ON tenant_rate_limits
                USING (tenant_id = current_setting('app.tenant_id', true))
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true))
            """
        )
    )
    op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_rate_limits TO {APP_ROLE}"))


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON tenant_rate_limits"))
    op.drop_table("tenant_rate_limits")
