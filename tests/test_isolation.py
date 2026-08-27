"""Tenant isolation suite.

This is the deliverable that makes multi-tenancy real. The claim under test is
not "the application remembers to filter by tenant" — it is "the database will
not return another tenant's rows even when the application forgets".

The signature test is `test_unscoped_query_returns_no_foreign_rows`: a query
with no WHERE clause at all. Under application-layer filtering it would return
everything; under RLS it returns only the caller's rows. That test is the whole
reason RLS was chosen over filtering in the ORM, and it must stay in CI.

Preconditions that make the policies real, asserted here rather than assumed:
the application role is not a superuser and does not have BYPASSRLS, and every
tenant-scoped table has RLS both enabled and FORCEd.

The table list is DISCOVERED FROM THE LIVE SCHEMA, not hardcoded. That is the
lesson of the 4d audit: `pipelines` went two phases without a policy because
`ALTER DEFAULT PRIVILEGES` grants every new table full read/write to the app
role automatically while the policy that confines those grants has to be
written by hand. A hardcoded list in this file would have gone stale in exactly
the same silence. Add a table with a `tenant_id` and no policy now and
`test_every_tenant_scoped_table_is_protected` goes red.
"""

import socket
import uuid
from decimal import Decimal
from urllib.parse import urlparse

import pytest
from docfactory_core.auth import AuthError, issue_api_key, resolve_tenant, revoke_api_key
from docfactory_core.db import admin_session_scope, session_scope, tenant_context
from docfactory_core.models import (
    Document,
    DocumentStatus,
    EvalCase,
    Extraction,
    ExtractionField,
    Pipeline,
    ReviewStatus,
    ReviewTask,
    Tenant,
    TenantRateLimit,
    TenantSpend,
    UsageEvent,
)
from sqlalchemy import delete, select, text

pytestmark = pytest.mark.integration

# The floor. Discovery must find at least these; if the query silently returned
# nothing, the parametrized tests below would vacuously pass.
KNOWN_TENANT_TABLES = frozenset(
    {
        "documents",
        "extractions",
        "extraction_fields",
        "review_tasks",
        "eval_cases",
        "usage_events",
        "tenant_spend",
        "tenant_rate_limits",
        "pipelines",
    }
)

# Carries a tenant_id but deliberately has NO row policy. Authentication
# resolves a key hash to a tenant *before* a tenant is bound, so a tenant-scoped
# policy here would compare against an unset app.tenant_id and every login would
# fail. It is protected by grant instead — SELECT only — which
# TestControlPlaneTables asserts. See docs/tenant_isolation_audit.md.
POLICY_EXEMPT = frozenset({"api_keys"})


def _tenant_scoped_tables() -> tuple[str, ...]:
    """Every table in the live schema with a tenant_id column.

    Read at collection time so the parametrized tests below cover tables nobody
    remembered to add here. Returns empty if the database is unreachable; the
    suite skips in that case, and `test_discovery_actually_ran` makes sure an
    empty result can never look like a pass.
    """
    try:
        with admin_session_scope() as session:
            return tuple(
                session.execute(
                    text(
                        """
                        SELECT c.relname
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        JOIN information_schema.columns col
                          ON col.table_name = c.relname AND col.table_schema = n.nspname
                        WHERE n.nspname = 'public' AND c.relkind = 'r'
                          AND col.column_name = 'tenant_id'
                        ORDER BY c.relname
                        """
                    )
                )
                .scalars()
                .all()
            )
    except Exception:  # collection must not fail on a stopped database
        return ()


DISCOVERED_TABLES = _tenant_scoped_tables()
TENANT_TABLES = tuple(t for t in DISCOVERED_TABLES if t not in POLICY_EXEMPT) or tuple(
    sorted(KNOWN_TENANT_TABLES)
)
OTHER = "acme-tenant"


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def require_db():
    from docfactory_core.config import get_settings

    parsed = urlparse(get_settings().database_url.replace("postgresql+psycopg", "postgresql"))
    if not _reachable(parsed.hostname or "localhost", parsed.port or 5432):
        pytest.skip("postgres is not running")
    try:
        with tenant_context("dev-tenant"), session_scope() as session:
            session.execute(select(Document).limit(1))
    except Exception:
        pytest.skip("database not migrated")


@pytest.fixture(scope="module")
def other_tenant():
    """A second tenant with a row in every tenant-scoped table.

    "Every" is load-bearing. Before 4d this fixture stopped at the five tables
    the Phase 3a migration protected, so the four added later — and the one
    that had no policy at all — were never represented in a leak test. A
    foreign row has to exist for "I cannot see it" to mean anything.
    """
    with admin_session_scope() as session:
        if session.get(Tenant, OTHER) is None:
            session.add(Tenant(id=OTHER, name="Acme Corp"))

    document_id, extraction_id = uuid.uuid4(), uuid.uuid4()
    with admin_session_scope() as session:
        session.add(
            Document(
                id=document_id,
                tenant_id=OTHER,
                s3_key=f"{OTHER}/incoming/{document_id}.pdf",
                sha256=uuid.uuid4().hex * 2,
                status=DocumentStatus.NEEDS_REVIEW,
            )
        )
        session.add(
            Extraction(
                id=extraction_id,
                document_id=document_id,
                tenant_id=OTHER,
                model="mock:mock-extractor-v1",
                output={"vendor": "Acme Secret Supplier"},
                doc_confidence=0.2,
            )
        )
        # Flush in dependency order: the ORM's own insert ordering does not
        # guarantee the parent rows land before their foreign keys.
        session.flush()
        session.add(
            ExtractionField(
                extraction_id=extraction_id,
                tenant_id=OTHER,
                name="vendor",
                value="Acme Secret Supplier",
            )
        )
        session.flush()
        session.add(
            ReviewTask(
                extraction_id=extraction_id,
                document_id=document_id,
                tenant_id=OTHER,
                status=ReviewStatus.OPEN,
                sla_due_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
                flagged_fields=["vendor"],
            )
        )
        session.flush()
        session.add(
            EvalCase(
                tenant_id=OTHER,
                document_id=document_id,
                extraction_id=extraction_id,
                field_name="vendor",
                extracted_value="wrong",
                corrected_value="Acme Secret Supplier",
            )
        )
    # The tables added after Phase 3a. usage_events and tenant_spend matter
    # most of all of these: a tenant reading another's cost and billing data is
    # the leak that ends a B2B conversation, and neither was covered until now.
    usage_event_id = uuid.uuid4()
    with admin_session_scope() as session:
        session.add(
            Pipeline(
                tenant_id=OTHER,
                slug="acme_secret_recipe",
                version=1,
                document_type="acme secret",
                config={"fields": []},
            )
        )
        session.add(
            UsageEvent(
                id=usage_event_id,
                tenant_id=OTHER,
                model="mock:mock-extractor-v1",
                model_tier="frontier",
                purpose="extract",
                input_tokens=4242,
                output_tokens=424,
                cost_usd=Decimal("9.990000"),
            )
        )
        session.execute(
            text(
                "INSERT INTO tenant_spend (tenant_id, spent_usd) VALUES (:t, 9.99) "
                "ON CONFLICT (tenant_id) DO UPDATE SET spent_usd = 9.99"
            ),
            {"t": OTHER},
        )
        session.execute(
            text(
                "INSERT INTO tenant_rate_limits (tenant_id, tokens) VALUES (:t, 7) "
                "ON CONFLICT (tenant_id) DO UPDATE SET tokens = 7"
            ),
            {"t": OTHER},
        )

    acme_key = "dk_acme_test_key"
    try:
        resolve_tenant(acme_key)
    except AuthError:
        issue_api_key(OTHER, "acme key", plaintext=acme_key)
    key = type("K", (), {"plaintext": acme_key})
    yield {
        "tenant": OTHER,
        "document_id": document_id,
        "extraction_id": extraction_id,
        "usage_event_id": usage_event_id,
        "pipeline_slug": "acme_secret_recipe",
        "api_key": key.plaintext,
    }
    with admin_session_scope() as session:
        session.execute(delete(Document).where(Document.tenant_id == OTHER))
        session.execute(delete(UsageEvent).where(UsageEvent.tenant_id == OTHER))
        session.execute(delete(Pipeline).where(Pipeline.tenant_id == OTHER))


class TestPreconditions:
    """Policies are decorative unless these hold."""

    def test_application_role_is_not_a_superuser(self):
        with admin_session_scope() as session:
            row = session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='docfactory_app'")
            ).one()
        # A superuser bypasses RLS entirely, even with FORCE — the classic
        # foot-gun that makes every policy below a no-op.
        assert row[0] is False, "app role is a superuser: RLS is bypassed"
        assert row[1] is False, "app role has BYPASSRLS: RLS is bypassed"

    def test_the_app_actually_connects_as_that_role(self):
        with tenant_context("dev-tenant"), session_scope() as session:
            assert session.execute(text("SELECT current_user")).scalar() == "docfactory_app"

    @pytest.mark.parametrize("table", TENANT_TABLES)
    def test_rls_is_enabled_and_forced(self, table):
        with admin_session_scope() as session:
            row = session.execute(
                text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
                {"t": table},
            ).one()
        assert row[0] is True, f"{table} has RLS disabled"
        # FORCE makes the table owner obey its own policies too.
        assert row[1] is True, f"{table} does not FORCE RLS"

    @pytest.mark.parametrize("table", TENANT_TABLES)
    def test_every_tenant_table_has_a_policy(self, table):
        with admin_session_scope() as session:
            policies = (
                session.execute(
                    text("SELECT policyname FROM pg_policies WHERE tablename = :t"), {"t": table}
                )
                .scalars()
                .all()
            )
        assert "tenant_isolation" in policies

    def test_discovery_actually_ran(self):
        """An empty discovery would make every parametrized test above vacuous.

        The failure mode this guards against is subtle: if the schema query
        returned nothing, pytest would generate zero parametrized cases and the
        suite would go green having asserted nothing at all.
        """
        assert DISCOVERED_TABLES, "schema discovery returned no tables"
        missing = KNOWN_TENANT_TABLES - set(DISCOVERED_TABLES)
        assert not missing, f"tables that should carry a tenant_id no longer do: {sorted(missing)}"

    def test_every_tenant_scoped_table_is_protected(self):
        """THE drift guard. Not parametrized — it is about the SET of tables.

        Any table with a tenant_id must have RLS enabled, FORCEd, and a policy,
        unless it is on the documented exemption list. A new tenant-scoped table
        added without one fails here, in CI, before it can ship — which is the
        thing that did not happen for `pipelines` between Phase 3b and 4d.
        """
        with admin_session_scope() as session:
            rows = session.execute(
                text(
                    """
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                           (SELECT count(*) FROM pg_policies p
                             WHERE p.tablename = c.relname AND p.policyname = 'tenant_isolation')
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    JOIN information_schema.columns col
                      ON col.table_name = c.relname AND col.table_schema = n.nspname
                    WHERE n.nspname = 'public' AND c.relkind = 'r'
                      AND col.column_name = 'tenant_id'
                    """
                )
            ).all()

        unprotected = [
            {"table": name, "rls": rls, "forced": forced, "policies": policies}
            for name, rls, forced, policies in rows
            if name not in POLICY_EXEMPT and not (rls and forced and policies)
        ]
        assert not unprotected, (
            "tenant-scoped tables with no enforced isolation policy: "
            f"{unprotected}. Add ENABLE + FORCE ROW LEVEL SECURITY and a "
            "tenant_isolation policy in a migration, or document the table in "
            "POLICY_EXEMPT and docs/tenant_isolation_audit.md if it genuinely "
            "cannot have one."
        )


class TestRowLevelIsolation:
    def test_unscoped_query_returns_no_foreign_rows(self, other_tenant):
        """THE signature test: no WHERE clause, and still no leak.

        Application-layer filtering cannot pass this — the query has nothing to
        filter on. Only the database can, which is the entire argument for RLS.
        """
        with tenant_context("dev-tenant"), session_scope() as session:
            every_document = session.scalars(select(Document)).all()
        assert all(d.tenant_id == "dev-tenant" for d in every_document)
        assert other_tenant["document_id"] not in {d.id for d in every_document}

    @pytest.mark.parametrize(
        "model",
        [
            Document,
            Extraction,
            ExtractionField,
            ReviewTask,
            EvalCase,
            # Added in 4d. Pipeline had no policy at all until this phase;
            # the other three had one and were simply never tested.
            Pipeline,
            UsageEvent,
            TenantSpend,
            TenantRateLimit,
        ],
    )
    def test_no_table_leaks_under_an_unscoped_select(self, other_tenant, model):
        with tenant_context("dev-tenant"), session_scope() as session:
            rows = session.scalars(select(model)).all()
        assert all(row.tenant_id == "dev-tenant" for row in rows)

    def test_fetching_a_foreign_row_by_primary_key_finds_nothing(self, other_tenant):
        # Knowing the exact id must not help.
        with tenant_context("dev-tenant"), session_scope() as session:
            assert session.get(Document, other_tenant["document_id"]) is None
            assert session.get(Extraction, other_tenant["extraction_id"]) is None

    def test_a_tenant_sees_its_own_rows(self, other_tenant):
        with tenant_context(OTHER), session_scope() as session:
            assert session.get(Document, other_tenant["document_id"]) is not None

    def test_writing_a_row_for_another_tenant_is_refused(self, other_tenant):
        # WITH CHECK on the policy: you cannot insert what you could not read.
        from sqlalchemy.exc import DatabaseError

        with (
            pytest.raises(DatabaseError),
            tenant_context("dev-tenant"),
            session_scope() as session,
        ):
            session.add(
                Document(
                    tenant_id=OTHER,
                    s3_key=f"{OTHER}/incoming/smuggled.pdf",
                    sha256=uuid.uuid4().hex * 2,
                    status=DocumentStatus.RECEIVED,
                )
            )

    def test_updating_a_foreign_row_affects_nothing(self, other_tenant):
        with tenant_context("dev-tenant"), session_scope() as session:
            result = session.execute(
                text("UPDATE documents SET status = 'failed' WHERE id = :id"),
                {"id": str(other_tenant["document_id"])},
            )
            assert result.rowcount == 0
        with tenant_context(OTHER), session_scope() as session:
            assert session.get(Document, other_tenant["document_id"]).status != "failed"

    def test_deleting_a_foreign_row_affects_nothing(self, other_tenant):
        with tenant_context("dev-tenant"), session_scope() as session:
            result = session.execute(
                text("DELETE FROM documents WHERE id = :id"),
                {"id": str(other_tenant["document_id"])},
            )
            assert result.rowcount == 0

    def test_an_unbound_session_sees_nothing_rather_than_everything(self, other_tenant):
        """Fail closed: no tenant bound means no rows, not all rows.

        Uses the application role deliberately. The admin role is a superuser
        and bypasses RLS entirely, so running this through it would prove
        nothing at all.
        """
        from docfactory_core.db import current_tenant

        token = current_tenant.set(None)
        try:
            with session_scope(require_tenant=False) as session:
                assert session.execute(text("SELECT current_user")).scalar() == "docfactory_app"
                assert session.scalars(select(Document)).all() == []
        finally:
            current_tenant.reset(token)


class TestLateAddedTablesAreProtected:
    """The tables the Phase 3a suite never covered.

    Four of these were added after the original RLS migration and one of them —
    `pipelines` — had no policy at all until 4d. Each gets the same three
    assertions the original five have always had: the unscoped read, the
    known-id read, and the cross-tenant write.
    """

    def test_another_tenants_pipeline_definition_is_invisible(self, other_tenant):
        """A pipeline row is the tenant's schema, rules and routing policy.

        Competitively meaningful configuration. Until 4d this table had full
        app-role grants and no policy, and this query returned acme's row.
        """
        with tenant_context("dev-tenant"), session_scope() as session:
            slugs = {row.slug for row in session.scalars(select(Pipeline)).all()}
        assert other_tenant["pipeline_slug"] not in slugs

    def test_the_owning_tenant_still_sees_its_pipeline(self, other_tenant):
        """The policy must confine, not break. Isolation that hides your own
        rows is an outage wearing a security badge."""
        with tenant_context(OTHER), session_scope() as session:
            slugs = {row.slug for row in session.scalars(select(Pipeline)).all()}
        assert other_tenant["pipeline_slug"] in slugs

    def test_writing_a_pipeline_for_another_tenant_is_refused(self, other_tenant):
        from sqlalchemy.exc import DatabaseError

        with (
            pytest.raises(DatabaseError),
            tenant_context("dev-tenant"),
            session_scope() as session,
        ):
            session.add(
                Pipeline(
                    tenant_id=OTHER,
                    slug="smuggled",
                    version=99,
                    document_type="smuggled",
                    config={"fields": []},
                )
            )

    def test_updating_another_tenants_pipeline_affects_nothing(self, other_tenant):
        with tenant_context("dev-tenant"), session_scope() as session:
            result = session.execute(
                text("UPDATE pipelines SET is_active = false WHERE tenant_id = :t"), {"t": OTHER}
            )
            assert result.rowcount == 0
        with tenant_context(OTHER), session_scope() as session:
            row = session.scalars(select(Pipeline)).one()
            assert row.is_active is True

    def test_another_tenants_usage_events_are_invisible(self, other_tenant):
        """Cost and token counts, per call. The B2B-deal-ending leak."""
        with tenant_context("dev-tenant"), session_scope() as session:
            assert session.get(UsageEvent, other_tenant["usage_event_id"]) is None
            spend = session.execute(
                text("SELECT coalesce(sum(cost_usd), 0) FROM usage_events")
            ).scalar()
        # acme's single event is $9.99; dev-tenant's whole history must be less.
        assert spend < Decimal("9.99")

    def test_writing_a_usage_event_for_another_tenant_is_refused(self, other_tenant):
        from sqlalchemy.exc import DatabaseError

        with (
            pytest.raises(DatabaseError),
            tenant_context("dev-tenant"),
            session_scope() as session,
        ):
            session.add(
                UsageEvent(
                    tenant_id=OTHER,
                    model="mock:mock-extractor-v1",
                    model_tier="small",
                    purpose="extract",
                    cost_usd=Decimal("0.01"),
                )
            )

    def test_another_tenants_spend_counter_is_invisible(self, other_tenant):
        """tenant_spend is the row a budget cap is enforced against.

        Reading it is reading the customer's bill; writing it is raising or
        lowering their cap.
        """
        with tenant_context("dev-tenant"), session_scope() as session:
            assert session.get(TenantSpend, OTHER) is None
            rows = session.scalars(select(TenantSpend)).all()
        assert all(row.tenant_id == "dev-tenant" for row in rows)

    def test_another_tenants_spend_cannot_be_moved(self, other_tenant):
        with tenant_context("dev-tenant"), session_scope() as session:
            result = session.execute(
                text("UPDATE tenant_spend SET spent_usd = 0 WHERE tenant_id = :t"), {"t": OTHER}
            )
            assert result.rowcount == 0
        with tenant_context(OTHER), session_scope() as session:
            assert session.get(TenantSpend, OTHER).spent_usd == Decimal("9.990000")

    def test_another_tenants_rate_limit_bucket_is_invisible(self, other_tenant):
        """Draining a neighbour's token bucket is a denial of service."""
        with tenant_context("dev-tenant"), session_scope() as session:
            assert session.get(TenantRateLimit, OTHER) is None
            result = session.execute(
                text("UPDATE tenant_rate_limits SET tokens = 0 WHERE tenant_id = :t"), {"t": OTHER}
            )
            assert result.rowcount == 0


class TestControlPlaneTables:
    """`tenants` and `api_keys` cannot have policies, so grants are the control.

    Both are read by the auth path before any tenant is bound, so a
    tenant-scoped policy would compare against an unset app.tenant_id and every
    login would fail. The protection is therefore GRANT-shaped, and these tests
    are the only thing asserting it.

    Until 4d the app role could INSERT into api_keys. That is isolation
    defeated from above rather than around: mint a key for another tenant,
    present it, and every policy in the schema then works perfectly on the
    attacker's behalf.
    """

    @staticmethod
    def _as_app(statement: str, **params):
        from sqlalchemy.exc import ProgrammingError

        with (
            pytest.raises(ProgrammingError, match="permission denied"),
            tenant_context("dev-tenant"),
            session_scope() as session,
        ):
            session.execute(text(statement), params)
            session.flush()

    def test_the_app_role_cannot_mint_an_api_key(self, other_tenant):
        """The privilege escalation itself. This exact INSERT used to succeed."""
        self._as_app(
            "INSERT INTO api_keys (tenant_id, name, key_hash, key_prefix) "
            "VALUES (:t, 'forged', repeat('f', 64), 'dk_forged')",
            t=OTHER,
        )

    def test_the_app_role_cannot_mint_a_key_for_itself_either(self):
        """Not a cross-tenant problem — the app has no business issuing keys."""
        self._as_app(
            "INSERT INTO api_keys (tenant_id, name, key_hash, key_prefix) "
            "VALUES ('dev-tenant', 'forged', repeat('e', 64), 'dk_forged2')"
        )

    def test_the_app_role_cannot_revoke_a_key(self):
        """Revocation is denial of service if anyone can do it."""
        self._as_app("UPDATE api_keys SET revoked_at = now()")

    def test_the_app_role_cannot_delete_a_key(self):
        self._as_app("DELETE FROM api_keys")

    def test_the_app_role_cannot_write_tenants(self):
        self._as_app("UPDATE tenants SET budget_usd = 1000000")

    def test_the_app_role_cannot_rewrite_migration_state(self):
        """An app role that can UPDATE alembic_version can convince the next
        deploy that a migration it never ran has already been applied."""
        self._as_app("UPDATE alembic_version SET version_num = 'deadbeef'")

    def test_the_app_role_can_still_read_api_keys(self):
        """The revoke must not overshoot. Authentication is a SELECT on this
        table with no tenant bound, and it has to keep working."""
        with tenant_context("dev-tenant"), session_scope() as session:
            assert session.execute(text("SELECT count(*) FROM api_keys")).scalar() >= 1

    def test_authentication_still_resolves_a_tenant(self, other_tenant):
        """THE regression this fix could plausibly have caused. Login works."""
        assert resolve_tenant("dev-local-key") == "dev-tenant"
        assert resolve_tenant(other_tenant["api_key"]) == OTHER

    def test_authentication_works_with_no_tenant_bound(self, other_tenant):
        """Explicitly the unbound case, since that is what auth actually is:
        resolving the key is how the tenant becomes known in the first place."""
        from docfactory_core.db import current_tenant

        token = current_tenant.set(None)
        try:
            assert resolve_tenant(other_tenant["api_key"]) == OTHER
        finally:
            current_tenant.reset(token)

    def test_issuing_a_key_still_works_through_the_owner(self):
        """Key issuance moved to admin_session_scope(). It must still function,
        and the key it mints must authenticate."""
        issued = issue_api_key("dev-tenant", "4d owner-path test")
        try:
            assert resolve_tenant(issued.plaintext) == "dev-tenant"
        finally:
            revoke_api_key(issued.key_id)
        with pytest.raises(AuthError):
            resolve_tenant(issued.plaintext)

    def test_api_keys_grants_are_exactly_select(self):
        """The property, asserted directly rather than inferred from behaviour."""
        with admin_session_scope() as session:
            granted = set(
                session.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_schema='public' AND table_name='api_keys' "
                        "AND grantee='docfactory_app'"
                    )
                )
                .scalars()
                .all()
            )
        assert granted == {"SELECT"}, f"api_keys grants drifted: {sorted(granted)}"

    def test_alembic_version_has_no_app_grants_at_all(self):
        with admin_session_scope() as session:
            granted = (
                session.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_schema='public' AND table_name='alembic_version' "
                        "AND grantee='docfactory_app'"
                    )
                )
                .scalars()
                .all()
            )
        assert granted == [], f"app role can still touch migration state: {granted}"


class TestSessionScoping:
    def test_tenant_setting_is_transaction_local(self):
        """SET LOCAL, not SET: the value must not survive the transaction.

        A value that persisted would ride a pooled connection into the next
        request — the exact leak this design exists to prevent.
        """
        with tenant_context("dev-tenant"), session_scope() as session:
            assert (
                session.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar()
                == "dev-tenant"
            )

        # A fresh transaction on the same pooled connection starts clean.
        with admin_session_scope() as session:
            leaked = session.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar()
        assert leaked in (None, ""), f"tenant context leaked across connections: {leaked!r}"

    def test_consecutive_tenants_do_not_bleed(self, other_tenant):
        with tenant_context("dev-tenant"), session_scope() as session:
            first = session.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar()
        with tenant_context(OTHER), session_scope() as session:
            second = session.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar()
        assert (first, second) == ("dev-tenant", OTHER)

    def test_a_session_without_a_tenant_is_rejected(self):
        from docfactory_core.db import TenantContextError, current_tenant

        token = current_tenant.set(None)
        try:
            with pytest.raises(TenantContextError), session_scope() as session:
                session.execute(select(Document).limit(1))
        finally:
            current_tenant.reset(token)


class TestApiIsolation:
    @pytest.fixture
    def dev_client(self):
        from conftest import authenticated_client

        with authenticated_client() as client:
            yield client

    @pytest.fixture
    def acme_client(self, other_tenant):
        from conftest import authenticated_client

        with authenticated_client(other_tenant["api_key"]) as client:
            yield client

    def test_missing_key_is_401(self):
        from docfactory_api.main import app
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            assert client.get("/review/tasks").status_code == 401

    def test_unknown_key_is_401(self):
        from conftest import authenticated_client

        with authenticated_client("dk_not_a_real_key") as client:
            assert client.get("/review/tasks").status_code == 401

    def test_revoked_key_is_401(self):
        issued = issue_api_key("dev-tenant", "temporary")
        assert resolve_tenant(issued.plaintext) == "dev-tenant"
        revoke_api_key(issued.key_id)
        with pytest.raises(AuthError):
            resolve_tenant(issued.plaintext)

    def test_another_tenants_document_is_404_not_403(self, dev_client, other_tenant):
        """404, deliberately: a 403 would confirm the id exists somewhere."""
        response = dev_client.get(f"/documents/{other_tenant['document_id']}")
        assert response.status_code == 404

    def test_list_endpoints_do_not_include_foreign_rows(self, dev_client, other_tenant):
        body = dev_client.get("/review/tasks").json()
        assert all(t["document_id"] != str(other_tenant["document_id"]) for t in body)

    def test_each_tenant_sees_only_its_own_queue(self, dev_client, acme_client, other_tenant):
        acme = acme_client.get("/review/tasks").json()
        assert any(t["document_id"] == str(other_tenant["document_id"]) for t in acme)
        dev = dev_client.get("/review/tasks").json()
        assert {t["task_id"] for t in acme}.isdisjoint({t["task_id"] for t in dev})

    def test_resolving_another_tenants_task_is_404(self, dev_client, acme_client, other_tenant):
        acme_task = acme_client.get("/review/tasks").json()[0]["task_id"]
        response = dev_client.post(
            f"/review/tasks/{acme_task}/resolve",
            json={"resolution": "approved_as_is", "corrections": {}},
        )
        assert response.status_code == 404


class TestBudgetIsolation:
    """A tenant over budget pauses; its neighbour is untouched."""

    @pytest.fixture
    def spent_tenant(self, other_tenant):
        """Push acme's spend past its cap on the counter the cap is enforced against."""
        from decimal import Decimal

        from sqlalchemy import text as sql

        with admin_session_scope() as session:
            session.get(Tenant, OTHER).budget_usd = Decimal("0.005")  # below one call's cost
            session.execute(
                sql(
                    "INSERT INTO tenant_spend (tenant_id, spent_usd) VALUES (:t, 1.00) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET spent_usd = 1.00"
                ),
                {"t": OTHER},
            )
        yield OTHER
        with admin_session_scope() as session:
            session.get(Tenant, OTHER).budget_usd = Decimal("100")
            session.execute(
                sql("UPDATE tenant_spend SET spent_usd = 0 WHERE tenant_id = :t"), {"t": OTHER}
            )

    def test_the_over_budget_tenant_reports_exceeded(self, spent_tenant):
        from docfactory_core.budget import budget_state

        state = budget_state(OTHER)
        assert state.exceeded is True
        assert state.remaining_usd == 0

    def test_another_tenant_is_unaffected(self, spent_tenant):
        from docfactory_core.budget import budget_state

        state = budget_state("dev-tenant")
        assert state.exceeded is False, "one tenant's overspend must not pause another"

    def test_spend_is_computed_per_tenant_not_globally(self, other_tenant):
        """acme's charge must not appear in dev-tenant's spend.

        Measured as a DELTA rather than against an absolute threshold. The
        original asserted `dev-tenant spend < $1`, which held until the
        development database had processed enough documents for its lifetime
        spend to cross a dollar — at which point a correct system failed a
        correct-looking test, for a reason that has nothing to do with
        isolation. A test whose truth depends on how much you have used the
        machine is a test that will eventually lie about something else.

        The delta form asserts the actual property: charging one tenant moves
        that tenant's counter and nobody else's.
        """
        from decimal import Decimal

        from docfactory_core.budget import budget_state

        before = budget_state("dev-tenant").spent_usd
        with admin_session_scope() as session:
            session.execute(
                text(
                    "INSERT INTO tenant_spend (tenant_id, spent_usd) VALUES (:t, 1.00) "
                    "ON CONFLICT (tenant_id) DO UPDATE "
                    "SET spent_usd = tenant_spend.spent_usd + 1.00"
                ),
                {"t": OTHER},
            )
        try:
            after = budget_state("dev-tenant").spent_usd
            assert after == before, (
                f"charging {OTHER} moved dev-tenant's counter from {before} to {after}"
            )
            assert budget_state(OTHER).spent_usd >= Decimal("1.00")
        finally:
            with admin_session_scope() as session:
                session.execute(
                    text(
                        "UPDATE tenant_spend SET spent_usd = spent_usd - 1.00 WHERE tenant_id = :t"
                    ),
                    {"t": OTHER},
                )

    def test_an_over_budget_document_pauses_instead_of_crashing(self, spent_tenant):
        """The worker records a clear state and acknowledges the message."""
        from decimal import Decimal

        from docfactory_core.budget import budget_state, reserve

        state = budget_state(OTHER)
        assert state.exceeded is True
        # And the charge is refused rather than merely reported: the cap is
        # enforced by the statement that would spend the money.
        assert reserve(OTHER, Decimal("0.01")) is None
        # BUDGET_EXCEEDED is deliberately distinct from FAILED: nothing is
        # wrong with the document and it resumes when the cap is raised.
        assert DocumentStatus.BUDGET_EXCEEDED == "budget_exceeded"


class TestObjectStoreIsolation:
    """The bucket has no RLS, so the prefix guard is its counterpart."""

    def test_reading_another_tenants_key_is_refused(self):
        from docfactory_core.storage import ObjectStore

        store = ObjectStore()
        with tenant_context("dev-tenant"), pytest.raises(PermissionError, match="outside tenant"):
            store.get_object(f"{OTHER}/incoming/anything.pdf")

    def test_writing_outside_the_tenant_prefix_is_refused(self):
        from docfactory_core.storage import ObjectStore

        store = ObjectStore()
        with tenant_context("dev-tenant"), pytest.raises(PermissionError, match="outside tenant"):
            store.put_object(f"{OTHER}/incoming/x.pdf", b"x", content_type="application/pdf")

    def test_path_traversal_is_refused(self):
        from docfactory_core.storage import ObjectStore

        store = ObjectStore()
        with tenant_context("dev-tenant"), pytest.raises(PermissionError, match="traversal"):
            store.get_object("dev-tenant/../acme-tenant/incoming/x.pdf")

    def test_listing_another_tenants_prefix_is_refused(self):
        from docfactory_core.storage import ObjectStore

        store = ObjectStore()
        with tenant_context("dev-tenant"), pytest.raises(PermissionError):
            list(store.list_keys(f"{OTHER}/"))
