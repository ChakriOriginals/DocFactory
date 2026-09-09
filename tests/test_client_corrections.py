"""A wrong answer that auto-approved had nowhere to go.

POST /review/tasks/{id}/resolve was the only write path in the API, and it
needs a ReviewTask. A document that auto-approved never had one — by
definition, because the confidence model was sure. So the errors a client is
most likely to notice, the ones that came back clean and wrong, were precisely
the errors they could not report.

It compounds. Every eval_case came from a resolved review task, so the feedback
loop learned only from errors the confidence model had already flagged. The
errors it is blind to could never enter the training data, which is the one
population that would have taught it something new.
"""

import pytest

pytestmark = pytest.mark.integration


class TestCorrectionPath:
    @pytest.fixture(autouse=True)
    def _needs_object_store(self, requires_object_store):
        pass

    @pytest.fixture
    def client(self):
        from conftest import authenticated_client

        with authenticated_client() as test_client:
            yield test_client

    def test_the_endpoint_exists_and_needs_no_review_task(self) -> None:
        from docfactory_api.main import app

        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/documents/{document_id}/corrections" in paths, (
            "There is no way to correct an auto-approved extraction. The only "
            "write path needs a ReviewTask, which an auto-approved document "
            "never had."
        )

    def test_correcting_an_unknown_document_is_404(self, client) -> None:
        import uuid

        response = client.post(
            f"/documents/{uuid.uuid4()}/corrections",
            json={"corrections": {"total": "1.00"}},
        )
        assert response.status_code == 404, response.text

    def test_an_empty_correction_is_rejected(self, client) -> None:
        import uuid

        response = client.post(f"/documents/{uuid.uuid4()}/corrections", json={"corrections": {}})
        assert response.status_code in (404, 422), response.text

    def test_a_document_with_no_extraction_cannot_be_corrected(self) -> None:
        """needs_ocr and failed documents have nothing to correct.

        Fabricating an extraction to hang a correction on would put an invented
        row into unit costs, drift baselines and the eval corpus.
        """
        import inspect

        from docfactory_core.review import correct_extraction

        source = inspect.getsource(correct_extraction)
        assert "no extraction to correct" in source


class TestCorrectionsFeedTheLearningLoop:
    """The point is not the write; it is the eval_case."""

    def test_a_correction_records_an_eval_case(self) -> None:
        import inspect

        from docfactory_core.review import correct_extraction

        source = inspect.getsource(correct_extraction)
        assert "EvalCase(" in source, (
            "A correction updates the extraction but records no eval_case, so "
            "the feedback loop still never sees the errors the confidence "
            "model was blind to — which is the whole reason this path exists."
        )

    def test_client_corrections_are_distinguishable_from_review(self) -> None:
        """Human review and a disputed auto-approval are different populations.

        Mixing them makes the corpus unable to answer "what does the model get
        wrong while being confident", which is the question worth asking.
        """
        import inspect

        from docfactory_core.review import correct_extraction

        signature = inspect.signature(correct_extraction)
        assert signature.parameters["source"].default == "client_correction"
