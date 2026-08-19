"""4d: pipelines was tenant-scoped and unprotected

`pipelines` was added in Phase 3b, after the Phase 3a RLS migration, and
inherited full read/write grants for the application role from
ALTER DEFAULT PRIVILEGES without ever getting the policy that confines them.
Verified against the live schema in docs/tenant_isolation_audit.md: the app
role could read every tenant's pipeline definitions and disable them.

A pipeline row is a tenant's extraction schema, validation rules and model
routing policy. Nothing reads this table today — definitions load from
config/pipelines/*.json — so this closes the hole before the DB-backed read the
registry's docstring anticipates makes it live.

Same shape as every other tenant table: ENABLE, then FORCE so the owner obeys
its own policy, then one ALL policy with a matching WITH CHECK so a row you
could not read is a row you cannot write. Hand-written because Alembic cannot
diff policies.

Revision ID: d4a1f0c9b7e2
Revises: 089e1b235c11
Create Date: 2026-08-18 19:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a1f0c9b7e2"
down_revision: str | Sequence[str] | None = "089e1b235c11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "pipelines"


def upgrade() -> None:
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
    op.execute(sa.text(f"ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY"))
