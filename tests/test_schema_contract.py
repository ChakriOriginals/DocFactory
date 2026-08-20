"""The code's enums and the database's constraints must agree.

This suite exists because they did not, and nothing noticed.

`DocumentStatus.BUDGET_EXCEEDED` was added in Phase 4a and written by
`_pause_on_budget()`; the CHECK constraint on `documents.status` was last
rewritten in Phase 2.3 and never learned the value. Every budget test asserted
on the enum or on the spend counter, so all of them passed while the actual
write would have raised CheckViolation, rolled back the extraction, and sent
the document to the DLQ after three retries.

The lesson generalizes past that one bug: an enum in Python and a CHECK in
Postgres are two declarations of the same fact, kept in sync by hand, in
separate files, usually in separate commits. Asserting one against the other in
code is the only thing that keeps them honest — so these tests write every enum
value to a real database rather than comparing strings to strings.
"""

import re
import uuid

import pytest
from docfactory_core import models
from docfactory_core.db import admin_session_scope, session_scope, tenant_context
from docfactory_core.models import Document, DocumentStatus, ReviewStatus, TenantStatus
from sqlalchemy import text

pytestmark = pytest.mark.integration

TENANT = "dev-tenant"


@pytest.fixture(scope="module", autouse=True)
def require_db():
    import socket
    from urllib.parse import urlparse

    from docfactory_core.config import get_settings

    parsed = urlparse(get_settings().database_url.replace("postgresql+psycopg", "postgresql"))
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 5432), 0.5):
            pass
    except OSError:
        pytest.skip("postgres is not running")


def _check_values(table: str, column: str) -> set[str]:
    """The literals a table's CHECK constraints permit for one column."""
    with admin_session_scope() as session:
        definitions = (
            session.execute(
                text(
                    "SELECT pg_get_constraintdef(k.oid) FROM pg_constraint k "
                    "JOIN pg_class c ON c.oid = k.conrelid "
                    "WHERE k.contype = 'c' AND c.relname = :table"
                ),
                {"table": table},
            )
            .scalars()
            .all()
        )
    allowed: set[str] = set()
    for definition in definitions:
        if re.search(rf"\b{column}\b", definition):
            allowed |= set(re.findall(r"'([a-z_]+)'::text", definition))
    return allowed


class TestEnumsMatchConstraints:
    @pytest.mark.parametrize(
        ("table", "column", "enum"),
        [
            ("documents", "status", DocumentStatus),
            ("tenants", "status", TenantStatus),
            ("review_tasks", "status", ReviewStatus),
        ],
    )
    def test_every_enum_value_is_permitted_by_the_check(self, table, column, enum):
        allowed = _check_values(table, column)
        assert allowed, f"{table}.{column} has no CHECK constraint to compare against"
        missing = {member.value for member in enum} - allowed
        assert not missing, (
            f"{table}.{column} rejects {sorted(missing)}, which the code can write. "
            "Add them to the CHECK in a migration — this is the budget_exceeded bug."
        )

    @pytest.mark.parametrize(
        ("table", "column", "enum"),
        [
            ("documents", "status", DocumentStatus),
            ("tenants", "status", TenantStatus),
            ("review_tasks", "status", ReviewStatus),
        ],
    )
    def test_the_check_permits_nothing_the_code_cannot_produce(self, table, column, enum):
        """The other direction. A value the database allows and the code never
        writes is a stale constraint — harmless today, and the thing that makes
        the next reader trust the wrong list."""
        extra = _check_values(table, column) - {member.value for member in enum}
        assert not extra, f"{table}.{column} permits {sorted(extra)}, which no enum produces"


class TestEveryStatusCanActuallyBeWritten:
    """String comparisons are what let the bug through. These write.

    Each value goes into a real row, through the ordinary application session,
    as the application role, under RLS — the same path the worker takes.
    """

    @pytest.mark.parametrize("status", sorted(DocumentStatus, key=lambda s: s.value))
    def test_a_document_can_reach_this_status(self, status):
        document_id = uuid.uuid4()
        with admin_session_scope() as session:
            session.add(
                Document(
                    id=document_id,
                    tenant_id=TENANT,
                    s3_key=f"{TENANT}/incoming/{document_id}.pdf",
                    sha256=uuid.uuid4().hex * 2,
                    status=DocumentStatus.RECEIVED,
                )
            )
        try:
            with tenant_context(TENANT), session_scope() as session:
                session.get(Document, document_id).status = status
            with tenant_context(TENANT), session_scope() as session:
                assert session.get(Document, document_id).status == status
        finally:
            with admin_session_scope() as session:
                session.execute(
                    text("DELETE FROM documents WHERE id = :id"), {"id": str(document_id)}
                )

    def test_the_budget_pause_writes_the_status_it_claims(self):
        """The exact call that was broken, exercised end to end.

        `_pause_on_budget` is the only reason BUDGET_EXCEEDED exists, and it
        was the only thing that would have discovered the constraint was wrong.
        """
        from docfactory_worker.handlers import _pause_on_budget

        document_id = uuid.uuid4()
        with admin_session_scope() as session:
            session.add(
                Document(
                    id=document_id,
                    tenant_id=TENANT,
                    s3_key=f"{TENANT}/incoming/{document_id}.pdf",
                    sha256=uuid.uuid4().hex * 2,
                    status=DocumentStatus.PARSED,
                )
            )
        try:
            with tenant_context(TENANT):
                _pause_on_budget(document_id, TENANT, _NullSpan())
                with session_scope() as session:
                    document = session.get(Document, document_id)
                    assert document.status == DocumentStatus.BUDGET_EXCEEDED
                    assert "budget" in (document.last_error or "")
        finally:
            with admin_session_scope() as session:
                session.execute(
                    text("DELETE FROM documents WHERE id = :id"), {"id": str(document_id)}
                )


class _NullSpan:
    """Enough of an OTel span for the code under test."""

    def set_attribute(self, *_args, **_kwargs) -> None:
        return None


def test_every_status_like_enum_is_covered_by_this_file():
    """A new status enum must not slip past these tests unnoticed.

    The failure mode this file exists for is a value nobody remembered to check
    — so the list of things being checked is itself checked.
    """
    known = {"DocumentStatus", "TenantStatus", "ReviewStatus", "ReviewResolution"}
    found = {
        name
        for name in dir(models)
        if isinstance(getattr(models, name), type)
        and issubclass(getattr(models, name), models.enum.StrEnum)
        and getattr(models, name) is not models.enum.StrEnum
    }
    assert found == known, (
        f"status-like enums changed: {sorted(found)}. Add the new one to the "
        "parametrized tests above, or to `known` with a reason if it has no "
        "CHECK constraint (ReviewResolution does not: it is nullable until a "
        "task is resolved, and its values are validated at the API boundary)."
    )
