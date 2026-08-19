"""row level security and the restricted application role

Isolation is enforced by the database, not by remembering to write WHERE
clauses. Every tenant-scoped table gets a policy keyed on a session variable
that the application sets per transaction; a query that forgets its filter
returns zero foreign rows instead of leaking them.

Two details make this real rather than decorative:

*The application connects as a non-superuser.* Superusers bypass RLS entirely
— even with FORCE ROW LEVEL SECURITY — so running the app as the database
owner would silently disable every policy here. This migration creates
docfactory_app and grants it DML only; DDL, roles and policies stay with the
owner, which is also who runs migrations.

*FORCE ROW LEVEL SECURITY* is set so the table owner obeys its own policies
too. Without it, anything connecting as the owner sees everything.

Creating a role is cluster-level rather than schema-level, which is unusual in
a migration; it lives here so local development is reproducible from
`make migrate` alone. In AWS this belongs in Terraform (see docs/backlog.md).

Revision ID: a1b2c3d4e5f6
Revises: 5c7d75127da4
Create Date: 2026-08-18 03:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from docfactory_core.config import get_settings

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "5c7d75127da4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every table carrying tenant_id. Adding a tenant-scoped table means adding it
# here; the isolation suite asserts this list covers the schema.
TENANT_TABLES = (
    "documents",
    "extractions",
    "extraction_fields",
    "review_tasks",
    "eval_cases",
)

APP_ROLE = "docfactory_app"


def upgrade() -> None:
    password = get_settings().app_db_password

    # Role creation is idempotent: the cluster outlives any one database, so a
    # rebuild of this database must not fail on an existing role.
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                    CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{password}' NOSUPERUSER
                        NOCREATEDB NOCREATEROLE NOBYPASSRLS;
                END IF;
            END
            $$;
            """
        )
    )
    op.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
    op.execute(
        sa.text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
        )
    )
    op.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))
    # Tables created by later migrations must be reachable too.
    op.execute(
        sa.text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
        )
    )

    for table in TENANT_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        # current_setting(..., true) returns NULL rather than raising when the
        # variable is unset, so an unscoped connection sees nothing instead of
        # erroring — fail closed, not open.
        op.execute(
            sa.text(
                f"""
                CREATE POLICY tenant_isolation ON {table}
                    USING (tenant_id = current_setting('app.tenant_id', true))
                    WITH CHECK (tenant_id = current_setting('app.tenant_id', true))
                """
            )
        )

    # Tenants and api_keys are not tenant-scoped rows in the same sense: the
    # auth path must read them *before* a tenant context exists, so neither can
    # carry a tenant-scoped policy — a policy would compare against an unset
    # app.tenant_id and every login would fail. They are protected by grant
    # instead.
    #
    # CORRECTION (4d): this REVOKE covers `tenants` and nothing else. The
    # original comment here claimed it covered api_keys too, which it never
    # did — the app role kept INSERT/UPDATE/DELETE on api_keys and could
    # therefore mint a valid key for any tenant. Fixed in revision
    # e5b2c1d8a3f7; see docs/tenant_isolation_audit.md. Left as a comment
    # rather than a change to this migration's SQL, because rewriting applied
    # history would leave every existing database in a state no migration
    # describes.
    op.execute(sa.text(f"REVOKE INSERT, UPDATE, DELETE ON tenants FROM {APP_ROLE}"))


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"GRANT INSERT, UPDATE, DELETE ON tenants TO {APP_ROLE}"))
    # The role is deliberately NOT dropped: other databases in the cluster may
    # use it, and dropping a role that owns nothing is still a cluster-wide
    # side effect from a per-database migration.
