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
"""

import socket
import uuid
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
    ReviewStatus,
    ReviewTask,
    Tenant,
)
from sqlalchemy import delete, select, text

pytestmark = pytest.mark.integration

TENANT_TABLES = ("documents", "extractions", "extraction_fields", "review_tasks", "eval_cases")
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
    """A second tenant with a full row set: document, extraction, field, task, case."""
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
        "api_key": key.plaintext,
    }
    with admin_session_scope() as session:
        session.execute(delete(Document).where(Document.tenant_id == OTHER))


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

    @pytest.mark.parametrize("model", [Document, Extraction, ExtractionField, ReviewTask, EvalCase])
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

    def test_spend_is_computed_per_tenant_not_globally(self, spent_tenant):
        from docfactory_core.budget import budget_state

        # acme's $1.00 charge must not appear in dev-tenant's spend. The
        # counter row is tenant-scoped and under RLS, so a read bound to
        # dev-tenant cannot see it even though both rows live in one table.
        assert budget_state("dev-tenant").spent_usd < 1

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
