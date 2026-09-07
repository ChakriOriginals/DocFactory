"""Review queue and SLA tests (need the compose stack; auto-skip otherwise).

Three claims worth pinning:

*A below-threshold document produces exactly one task*, carrying the specific
flagged fields — the fault localization from 2.1 is what makes review cheap,
so losing it would quietly make the queue much more expensive to work.

*Breach is detectable* from a stored absolute deadline. Alerting on breach is
Phase 5; this phase only has to make it queryable.

*A correction closes the loop* by writing an eval_case, so human effort becomes
labelled data for future evals and refits rather than a one-off repair.
"""

import socket
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from docfactory_core.config import get_settings
from docfactory_core.db import session_scope
from docfactory_core.models import (
    Document,
    DocumentStatus,
    EvalCase,
    Extraction,
    ExtractionField,
    ReviewResolution,
    ReviewStatus,
    ReviewTask,
)
from docfactory_core.review import (
    breached_tasks,
    ensure_review_task,
    open_tasks,
    queue_depth,
    resolve_task,
)
from sqlalchemy import delete, select

pytestmark = pytest.mark.integration


def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def require_db():
    settings = get_settings()
    if not settings.database_url or "localhost" not in settings.database_url:
        pytest.skip("no local database configured")
    if not _reachable("http://localhost:5432"):
        pytest.skip("postgres is not running")
    try:
        with session_scope() as session:
            session.execute(select(ReviewTask).limit(1))
    except Exception:
        pytest.skip("database not migrated")


@pytest.fixture
def extraction():
    """A stored extraction with two flagged fields, cleaned up afterwards."""
    settings = get_settings()
    document_id, extraction_id = uuid.uuid4(), uuid.uuid4()
    with session_scope() as session:
        session.add(
            Document(
                id=document_id,
                tenant_id=settings.default_tenant_id,
                s3_key=f"{settings.default_tenant_id}/incoming/{document_id}.pdf",
                sha256=uuid.uuid4().hex * 2,
                status=DocumentStatus.NEEDS_REVIEW,
            )
        )
        session.add(
            Extraction(
                id=extraction_id,
                document_id=document_id,
                tenant_id=settings.default_tenant_id,
                model="mock:mock-extractor-v1",
                output={"vendor": "B B AG", "total": "1075.00"},
                doc_confidence=0.11,
                routing_decision="needs_review",
                confidence_model_version=2,
            )
        )
        for name, value in (("vendor", "B B AG"), ("total", "1075.00")):
            session.add(
                ExtractionField(
                    extraction_id=extraction_id,
                    tenant_id=settings.default_tenant_id,
                    name=name,
                    value=value,
                    confidence=0.11 if name == "vendor" else 0.99,
                )
            )
    yield document_id, extraction_id
    with session_scope() as session:
        session.execute(delete(Document).where(Document.id == document_id))


class TestTaskCreation:
    def test_a_flagged_extraction_creates_a_task_with_its_fields(self, extraction):
        _, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        with session_scope() as session:
            task = session.get(ReviewTask, task_id)
            assert task.status == ReviewStatus.OPEN
            assert task.flagged_fields == ["vendor"]

    def test_sla_deadline_comes_from_config(self, extraction):
        _, extraction_id = extraction
        before = datetime.now(UTC)
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        with session_scope() as session:
            task = session.get(ReviewTask, task_id)
            expected = before + timedelta(hours=get_settings().review_sla_hours)
            assert abs((task.sla_due_at - expected).total_seconds()) < 60

    def test_creation_is_idempotent_under_redelivery(self, extraction):
        _, extraction_id = extraction
        first = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        second = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        assert first == second
        with session_scope() as session:
            tasks = session.scalars(
                select(ReviewTask).where(ReviewTask.extraction_id == extraction_id)
            ).all()
            assert len(tasks) == 1


class TestSlaBreach:
    def test_a_task_past_its_deadline_reads_as_breached(self, extraction):
        _, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        with session_scope() as session:
            session.get(ReviewTask, task_id).sla_due_at = datetime.now(UTC) - timedelta(hours=1)
        assert task_id in {task.id for task in breached_tasks()}

    def test_a_task_within_its_deadline_is_not_breached(self, extraction):
        _, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        assert task_id not in {task.id for task in breached_tasks()}

    def test_a_resolved_task_is_never_breached(self, extraction):
        _, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        with session_scope() as session:
            session.get(ReviewTask, task_id).sla_due_at = datetime.now(UTC) - timedelta(hours=1)
        resolve_task(task_id, resolution=ReviewResolution.APPROVED_AS_IS)
        assert task_id not in {task.id for task in breached_tasks()}

    def test_queue_depth_and_age_are_exposed(self, extraction):
        _, extraction_id = extraction
        ensure_review_task(extraction_id, flagged_fields=("vendor",))
        depth = queue_depth()
        assert depth["open"] >= 1
        assert depth["oldest_age_seconds"] >= 0


class TestResolution:
    def test_open_tasks_are_listed_oldest_first(self, extraction):
        _, extraction_id = extraction
        ensure_review_task(extraction_id, flagged_fields=("vendor",))
        created = [task.created_at for task in open_tasks()]
        assert created == sorted(created)

    def test_approving_as_is_closes_the_task_and_approves_the_document(self, extraction):
        document_id, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        resolve_task(task_id, resolution=ReviewResolution.APPROVED_AS_IS)
        with session_scope() as session:
            task = session.get(ReviewTask, task_id)
            assert task.status == ReviewStatus.RESOLVED
            assert task.resolved_at is not None
            assert session.get(Document, document_id).status == DocumentStatus.APPROVED

    def test_a_correction_updates_the_stored_values(self, extraction):
        document_id, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        resolve_task(
            task_id,
            resolution=ReviewResolution.CORRECTED,
            corrections={"vendor": "Bloch Bloch AG"},
        )
        with session_scope() as session:
            extraction_row = session.get(Extraction, extraction_id)
            assert extraction_row.output["vendor"] == "Bloch Bloch AG"
            field = session.scalars(
                select(ExtractionField)
                .where(ExtractionField.extraction_id == extraction_id)
                .where(ExtractionField.name == "vendor")
            ).one()
            assert field.value == "Bloch Bloch AG"
            assert session.get(Document, document_id).status == DocumentStatus.APPROVED

    def test_a_correction_appends_an_eval_case(self, extraction):
        document_id, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        resolve_task(
            task_id,
            resolution=ReviewResolution.CORRECTED,
            corrections={"vendor": "Bloch Bloch AG"},
        )
        with session_scope() as session:
            cases = session.scalars(
                select(EvalCase).where(EvalCase.document_id == document_id)
            ).all()
            assert len(cases) == 1
            case = cases[0]
            assert case.field_name == "vendor"
            assert case.extracted_value == "B B AG"  # what the model produced
            assert case.corrected_value == "Bloch Bloch AG"
            assert case.source == "human_review"

    def test_approving_as_is_creates_no_eval_case(self, extraction):
        document_id, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        resolve_task(task_id, resolution=ReviewResolution.APPROVED_AS_IS)
        with session_scope() as session:
            assert not session.scalars(
                select(EvalCase).where(EvalCase.document_id == document_id)
            ).all()

    def test_resolving_twice_is_rejected(self, extraction):
        _, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        resolve_task(task_id, resolution=ReviewResolution.APPROVED_AS_IS)
        with pytest.raises(ValueError, match="already resolved"):
            resolve_task(task_id, resolution=ReviewResolution.APPROVED_AS_IS)

    def test_a_correction_needs_values(self, extraction):
        _, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        with pytest.raises(ValueError, match="corrections"):
            resolve_task(task_id, resolution=ReviewResolution.CORRECTED, corrections={})


class TestReviewApi:
    """The API surface a reviewer (or a future UI) drives.

    Entering a TestClient runs the app lifespan and ensure_infra(), so this
    class needs the object store even though its assertions are about Postgres.
    Without the guard it fails after thirty retries instead of skipping.
    """

    @pytest.fixture(autouse=True)
    def _needs_object_store(self, requires_object_store):
        pass

    @pytest.fixture
    def client(self):
        from conftest import authenticated_client

        with authenticated_client() as test_client:
            yield test_client

    def test_open_tasks_are_listed_with_breach_state(self, client, extraction):
        _, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        body = client.get("/review/tasks").json()
        mine = next(t for t in body if t["task_id"] == str(task_id))
        assert mine["status"] == "open"
        assert mine["flagged_fields"] == ["vendor"]
        assert mine["breached"] is False

    def test_task_detail_points_at_the_flagged_cells(self, client, extraction):
        _, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        body = client.get(f"/review/tasks/{task_id}").json()
        assert body["confidence_model_version"] == 2
        assert [f["name"] for f in body["flagged"]] == ["vendor"]
        assert body["flagged"][0]["value"] == "B B AG"
        assert body["flagged"][0]["confidence"] == pytest.approx(0.11)

    def test_unknown_task_is_404(self, client):
        assert client.get(f"/review/tasks/{uuid.uuid4()}").status_code == 404

    def test_resolving_with_a_correction_closes_the_loop(self, client, extraction):
        document_id, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        response = client.post(
            f"/review/tasks/{task_id}/resolve",
            json={"resolution": "corrected", "corrections": {"vendor": "Bloch Bloch AG"}},
        )
        assert response.status_code == 200
        with session_scope() as session:
            assert session.get(Document, document_id).status == DocumentStatus.APPROVED
            case = session.scalars(
                select(EvalCase).where(EvalCase.document_id == document_id)
            ).one()
            assert case.corrected_value == "Bloch Bloch AG"

    def test_resolving_twice_is_a_conflict(self, client, extraction):
        _, extraction_id = extraction
        task_id = ensure_review_task(extraction_id, flagged_fields=("vendor",))
        payload = {"resolution": "approved_as_is", "corrections": {}}
        assert client.post(f"/review/tasks/{task_id}/resolve", json=payload).status_code == 200
        assert client.post(f"/review/tasks/{task_id}/resolve", json=payload).status_code == 409

    def test_queue_stats_expose_depth_and_breaches(self, client, extraction):
        _, extraction_id = extraction
        ensure_review_task(extraction_id, flagged_fields=("vendor",))
        body = client.get("/review/queue").json()
        assert body["open"] >= 1
        assert "breached" in body and "oldest_age_seconds" in body
