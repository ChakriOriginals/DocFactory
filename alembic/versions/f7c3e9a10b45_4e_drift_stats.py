"""4e: drift_stats — rolling baselines and drift state per tenant x doc type

One row per (tenant, doc_type, window). Holds the baseline the detector
compares against and the running state of that comparison together, because a
single event — a document finishing extraction — updates both, and splitting
them would be two writes that can disagree.

RLS FROM CREATION, not as a follow-up. That is the whole lesson of 4d:
`pipelines` was added in 3b, inherited full app-role read/write from
ALTER DEFAULT PRIVILEGES, and went two phases with no policy because the policy
has to be written by hand and nothing failed when it was not. This table is
tenant-scoped, so it gets ENABLE + FORCE + the policy in the same migration
that creates it. `test_every_tenant_scoped_table_is_protected` (4d) fails if
that is ever untrue — verified by deliberately omitting the policy and watching
it go red before writing this version.

Drift data is not especially sensitive on its own, but the baselines encode a
tenant's document mix and their extraction quality over time, and "not
especially sensitive" is exactly the reasoning that leaves a table unprotected.

Revision ID: f7c3e9a10b45
Revises: e5b2c1d8a3f7
Create Date: 2026-08-19 09:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7c3e9a10b45"
down_revision: str | Sequence[str] | None = "e5b2c1d8a3f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "drift_stats"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("doc_type", sa.Text(), nullable=False),
        sa.Column("window_key", sa.Text(), server_default="baseline-1", nullable=False),
        sa.Column("status", sa.Text(), server_default="baseline", nullable=False),
        sa.Column("n_observed", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "stats", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("centroid", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("flagged_signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_flagged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flagged_document_id", sa.Uuid(), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["flagged_document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "doc_type", "window_key", name="uq_drift_stats_window"),
        sa.CheckConstraint(
            "status IN ('baseline', 'stable', 'drifting')", name="ck_drift_stats_status"
        ),
    )

    op.execute(sa.text(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"""
            CREATE POLICY tenant_isolation ON {TABLE}
                USING (tenant_id = current_setting('app.tenant_id', true))
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true))
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}"))
    op.drop_table(TABLE)
