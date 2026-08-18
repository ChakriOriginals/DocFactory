"""Routing tests — written before the implementation.

Routing consumes the calibrated threshold produced by 2.2c/2.3a. Two things
matter beyond "does it split correctly":

*The threshold is configuration, never a constant.* Proven by loading a
modified config and observing the decision change — a hardcoded threshold
would silently ignore every future refit.

*The pipeline and the study must compute identical features.* The feature
extractor is shared code for that reason; if they drift, the threshold is
measured against one feature space and applied to another.
"""

import json
from pathlib import Path

import pytest
from docfactory_core.confidence_model import (
    FEATURES,
    ConfidenceModel,
    RoutingDecision,
    features_for,
    route_extraction,
)

BASE_MODEL = {
    "schema_version": 2,
    "model": "logistic",
    "features": list(FEATURES),
    # groundedness dominates: easy to reason about in tests
    "weights": [0.0] * (len(FEATURES) - 1) + [0.0],
    "bias": 0.0,
    "standardization": {"mean": [0.0] * len(FEATURES), "std": [1.0] * len(FEATURES)},
    "operating_point": {"threshold": 0.5, "target_precision": 0.99},
    "provenance": {},
}


def write_model(tmp_path, **overrides) -> ConfidenceModel:
    payload = {**BASE_MODEL, **overrides}
    path = tmp_path / "model.json"
    path.write_text(json.dumps(payload))
    return ConfidenceModel.load(path)


CLEAN_SIGNALS = {
    "attempts": 1,
    "rule.line_items_sum_to_subtotal": True,
    "rule.subtotal_plus_tax_equals_total": True,
    "rule.tax_matches_rate": True,
    "rule.invoice_date_parses": True,
    "rule.due_date_not_before_invoice_date": True,
    "groundedness.vendor": 1.0,
    "groundedness.invoice_number": 1.0,
    "groundedness.line_items_min": 1.0,
    "date_corroboration.invoice_date": 1.0,
    "date_corroboration.due_date": 1.0,
    "date_corroboration.payment_term": None,
    "total": 1000.0,
}


class TestFeatureExtraction:
    def test_every_feature_is_produced_for_every_field(self):
        for field in ("vendor", "total", "invoice_date", "line_items"):
            assert len(features_for(field, CLEAN_SIGNALS)) == len(FEATURES)

    def test_clean_signals_produce_a_benign_vector(self):
        vector = features_for("vendor", CLEAN_SIGNALS)
        named = dict(zip(FEATURES, vector, strict=True))
        assert named["implicating_rules_failed"] == 0.0
        assert named["groundedness"] == 1.0
        assert named["date_corroboration"] == 1.0

    def test_a_failed_rule_only_implicates_its_own_fields(self):
        signals = {**CLEAN_SIGNALS, "rule.subtotal_plus_tax_equals_total": False}
        assert (
            dict(zip(FEATURES, features_for("total", signals), strict=True))[
                "implicating_rules_failed"
            ]
            == 1.0
        )
        assert (
            dict(zip(FEATURES, features_for("vendor", signals), strict=True))[
                "implicating_rules_failed"
            ]
            == 0.0
        )

    def test_date_corroboration_only_applies_to_date_fields(self):
        signals = {**CLEAN_SIGNALS, "date_corroboration.invoice_date": 0.05}
        by_name = lambda f: dict(zip(FEATURES, features_for(f, signals), strict=True))  # noqa: E731
        assert by_name("invoice_date")["date_corroboration"] == 0.05
        assert by_name("vendor")["date_corroboration"] == 1.0


class TestThresholdComesFromConfig:
    def test_a_config_change_changes_the_decision(self, tmp_path):
        # Same signals, two configs: the only difference is the threshold.
        signals = {**CLEAN_SIGNALS, "groundedness.vendor": 0.1}
        weights = [0.0] * len(FEATURES)
        weights[FEATURES.index("groundedness")] = 4.0

        (tmp_path / "a").mkdir(exist_ok=True)
        (tmp_path / "b").mkdir(exist_ok=True)
        permissive = write_model(
            tmp_path / "a",
            weights=weights,
            bias=-2.0,
            operating_point={"threshold": 0.1, "target_precision": 0.99},
        )
        strict = write_model(
            tmp_path / "b",
            weights=weights,
            bias=-2.0,
            operating_point={"threshold": 0.95, "target_precision": 0.99},
        )
        assert route_extraction(signals, permissive).decision == "approved"
        assert route_extraction(signals, strict).decision == "needs_review"

    def test_threshold_is_read_from_the_file(self, tmp_path):
        model = write_model(tmp_path, operating_point={"threshold": 0.77, "target_precision": 0.99})
        assert model.threshold == 0.77

    def test_model_version_is_carried_for_provenance(self, tmp_path):
        assert write_model(tmp_path, schema_version=7).version == 7


class TestRoutingRule:
    """Document routing: any field below threshold sends the document to review."""

    def _model(self, tmp_path):
        # weight 4 with bias -2 puts a grounded field at p=0.88 and an
        # ungrounded one at p=0.12, so a 0.5 threshold separates them.
        weights = [0.0] * len(FEATURES)
        weights[FEATURES.index("groundedness")] = 4.0
        return write_model(
            tmp_path,
            weights=weights,
            bias=-2.0,
            operating_point={"threshold": 0.5, "target_precision": 0.99},
        )

    def test_all_clean_fields_auto_approve(self, tmp_path):
        result = route_extraction(CLEAN_SIGNALS, self._model(tmp_path))
        assert result.decision == "approved"
        assert result.flagged_fields == ()
        assert result.doc_confidence == pytest.approx(max(result.field_confidence.values()))

    def test_one_bad_field_sends_the_whole_document_to_review(self, tmp_path):
        signals = {**CLEAN_SIGNALS, "groundedness.vendor": 0.0}
        result = route_extraction(signals, self._model(tmp_path))
        assert result.decision == "needs_review"
        assert result.flagged_fields == ("vendor",)

    def test_flagged_fields_name_every_offender(self, tmp_path):
        signals = {
            **CLEAN_SIGNALS,
            "groundedness.vendor": 0.0,
            "groundedness.invoice_number": 0.0,
        }
        result = route_extraction(signals, self._model(tmp_path))
        assert set(result.flagged_fields) == {"vendor", "invoice_number"}

    def test_doc_confidence_is_the_weakest_field(self, tmp_path):
        signals = {**CLEAN_SIGNALS, "groundedness.vendor": 0.0}
        result = route_extraction(signals, self._model(tmp_path))
        assert result.doc_confidence == min(result.field_confidence.values())

    def test_decision_is_deterministic(self, tmp_path):
        model = self._model(tmp_path)
        assert route_extraction(CLEAN_SIGNALS, model) == route_extraction(CLEAN_SIGNALS, model)

    def test_routing_is_a_pure_function_of_signals_and_model(self, tmp_path):
        result = route_extraction(CLEAN_SIGNALS, self._model(tmp_path))
        assert isinstance(result, RoutingDecision)
        assert set(result.field_confidence) >= {"vendor", "total", "invoice_date"}


class TestPerPipelineCalibration:
    """A borrowed model is legitimate; a borrowed model served silently is not.

    The features are kind-driven, so an invoice-fitted model produces
    meaningful vectors for a purchase order — but the *weights* were measured
    against invoice errors, and nothing about a PO's error distribution has
    been observed. Routing therefore records which of the two it did.
    """

    def test_the_invoice_pipeline_serves_its_own_model(self):
        from docfactory_core.confidence_model import confidence_model_for
        from docfactory_core.pipeline_registry import default_pipeline

        definition = default_pipeline()
        model = confidence_model_for(definition)
        assert definition.confidence_model_path == "config/confidence_model_v2.json"
        assert model.calibrated_for == "invoice"
        assert model.calibration_for(definition.slug) == "invoice"

    def test_the_purchase_order_pipeline_is_told_it_is_borrowing(self):
        from docfactory_core.confidence_model import confidence_model_for
        from docfactory_core.pipeline_registry import load_from_file

        definition = load_from_file("purchase_order")
        assert definition.confidence_model_path is None  # falls back to the default
        model = confidence_model_for(definition)
        assert model.calibration_for(definition.slug) == "borrowed:invoice"

    def test_the_decision_carries_the_calibration_it_was_made_under(self):
        from docfactory_core.confidence_model import (
            confidence_model_for,
            get_confidence_model,
            route_extraction,
        )
        from docfactory_core.pipeline_registry import default_pipeline, load_from_file

        invoice, po = default_pipeline(), load_from_file("purchase_order")
        assert route_extraction(CLEAN_SIGNALS, get_confidence_model(), invoice).calibration == (
            "invoice"
        )
        assert (
            route_extraction(CLEAN_SIGNALS, confidence_model_for(po), po).calibration
            == "borrowed:invoice"
        )

    def test_a_named_model_file_is_loaded_rather_than_the_settings_default(self, tmp_path):
        import json

        from docfactory_core.confidence_model import get_confidence_model
        from docfactory_core.pipeline import parse_definition
        from docfactory_core.pipeline_registry import CONFIG_DIR

        payload = json.loads(Path("config/confidence_model_v2.json").read_text())
        payload["operating_point"]["threshold"] = 0.1234
        payload["calibrated_for"] = {"pipeline_slug": "purchase_order", "pipeline_version": 1}
        other = tmp_path / "confidence_model_test.json"
        other.write_text(json.dumps(payload))

        config = json.loads((CONFIG_DIR / "purchase_order_v1.json").read_text())
        config["confidence"] = {"model_path": str(other)}
        definition = parse_definition("dev-tenant", "purchase_order", 1, config)

        from docfactory_core.confidence_model import confidence_model_for

        model = confidence_model_for(definition)
        assert model.threshold == 0.1234
        assert model.calibration_for("purchase_order") == "purchase_order"
        # the deployment default is untouched
        assert get_confidence_model().threshold == 0.675
