"""4d: the control plane is not the application's to write

GAP 2 from docs/tenant_isolation_audit.md, and the reason this phase exists.

The Phase 3a migration's comment said tenants and api_keys were "readable by
the app role but writable only by the owner". Only `tenants` was ever revoked.
`api_keys` kept INSERT/UPDATE/DELETE, and carries no RLS policy, so the
application role could mint a valid key for any tenant — reproduced as
docfactory_app before this migration was written.

That defeats isolation from ABOVE rather than around. The row policies are not
bypassed; they are correctly applied to a tenant context that was fraudulently
obtained. Every other policy in the schema works perfectly, on the attacker's
behalf.

WHY A REVOKE AND NOT A POLICY. Authentication resolves a key hash to a tenant
*before* a tenant is known — that is what authentication is. A tenant-scoped
policy on api_keys would compare against an unset app.tenant_id, match nothing,
and every login would fail. So the app keeps unscoped SELECT and loses the
writes. The rows hold a SHA-256 hash and a 12-character prefix, never the
plaintext, so a full table read cannot be replayed as credentials.

alembic_version is included for a different reason: it is not an isolation
gap, it is an over-grant with no call site. Alembic connects as the owner
(alembic/env.py takes its URL from database_admin_url), and an app role that
can UPDATE this table can convince the next deploy that a migration it never
ran has already been applied.

Revision ID: e5b2c1d8a3f7
Revises: d4a1f0c9b7e2
Create Date: 2026-08-18 19:55:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b2c1d8a3f7"
down_revision: str | Sequence[str] | None = "d4a1f0c9b7e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "docfactory_app"


def upgrade() -> None:
    # SELECT survives deliberately: resolve_tenant() reads this table on every
    # authenticated request, before any tenant context exists.
    op.execute(sa.text(f"REVOKE INSERT, UPDATE, DELETE ON api_keys FROM {APP_ROLE}"))

    # Nothing the application does touches migration bookkeeping.
    op.execute(sa.text(f"REVOKE ALL ON alembic_version FROM {APP_ROLE}"))


def downgrade() -> None:
    op.execute(sa.text(f"GRANT INSERT, UPDATE, DELETE ON api_keys TO {APP_ROLE}"))
    op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON alembic_version TO {APP_ROLE}"))
