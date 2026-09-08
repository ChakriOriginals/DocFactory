"""A terminal state that produces nothing has to be visible somewhere.

`needs_ocr` is correct behaviour: a scanned PDF has no text layer, and
inventing an answer would be worse than declining. It was also completely
silent. No extraction, no review task, no DLQ message, no alarm — and no way to
list documents at all, only `GET /documents/{id}`, which needs an id the client
kept. A client discovered the hole by reconciling their own totals: they sent
500, they can account for 497, and nothing said which three produced nothing or
why. Roughly a quarter of the synthetic corpus lands in this state.

Two surfaces close it: a status breakdown on the endpoint the client already
polls, and a listing they can filter.

WHY NOT A REVIEW TASK, which is the obvious suggestion. ReviewTask.extraction_id
is NOT NULL and ensure_review_task requires an existing Extraction row — a
needs_ocr document has neither, by definition. Making it fit would mean either
weakening that invariant or fabricating an empty extraction, and the fabricated
row would then flow into unit costs, drift baselines and the eval corpus. The
review queue is for correcting an extraction; there is nothing here to correct.
"""

import pytest
from docfactory_core.models import DocumentStatus

pytestmark = pytest.mark.integration


class TestStatusVisibility:
    """Needs Postgres only — no object store, so this runs in CI."""

    def test_usage_reports_a_status_breakdown(self) -> None:
        from docfactory_api.main import SpendSummary

        assert "status_counts" in SpendSummary.model_fields, (
            "/usage no longer reports status_counts. A document that finished "
            "in a state producing no output becomes invisible again."
        )

    def test_status_counts_is_tenant_scoped(self) -> None:
        """RLS, not a WHERE clause the caller has to remember."""
        from docfactory_core.backpressure import status_counts

        counts = status_counts("no-such-tenant-exists")
        assert counts == {}, (
            f"An unknown tenant saw {counts}. status_counts must be scoped by "
            "RLS; leaking another tenant's document counts is a disclosure even "
            "without the documents themselves."
        )


class TestDocumentListing:
    """Entering a TestClient runs the lifespan, which needs the object store."""

    @pytest.fixture(autouse=True)
    def _needs_object_store(self, requires_object_store):
        pass

    @pytest.fixture
    def client(self):
        from conftest import authenticated_client

        with authenticated_client() as test_client:
            yield test_client

    def test_documents_can_be_listed_at_all(self, client) -> None:
        """Before this, the only way in was an id you already had."""
        response = client.get("/documents")
        assert response.status_code == 200, response.text
        body = response.json()
        assert {"documents", "total", "limit", "offset"} <= set(body)

    def test_the_silent_state_can_be_asked_for_by_name(self, client) -> None:
        response = client.get("/documents", params={"status": DocumentStatus.NEEDS_OCR.value})
        assert response.status_code == 200, response.text
        for row in response.json()["documents"]:
            assert row["status"] == DocumentStatus.NEEDS_OCR.value

    def test_an_impossible_status_is_rejected_not_answered_with_nothing(self, client) -> None:
        """An empty page for a typo reads as "nothing is stuck", which is a lie."""
        response = client.get("/documents", params={"status": "needs-ocr"})
        assert response.status_code == 422, (
            "A status that cannot exist returned a page instead of an error. An "
            "empty result is indistinguishable from 'you have none', which is "
            "exactly the false reassurance this endpoint exists to remove."
        )
        assert "needs_ocr" in response.text, "the error should name the valid statuses"

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 500}, {"offset": -1}])
    def test_pagination_bounds_are_enforced(self, client, params) -> None:
        assert client.get("/documents", params=params).status_code == 422

    def test_total_counts_the_filter_not_the_page(self, client) -> None:
        """The number a client reconciles against is the total, not len(page)."""
        response = client.get("/documents", params={"limit": 1})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= len(body["documents"])
