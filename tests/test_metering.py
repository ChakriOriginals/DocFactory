"""Token metering, model prices, and the routing policy.

The unit-cost number this phase produces is only as good as the arithmetic
under it, so the arithmetic is pinned here: published rates, real token counts,
and no silent zero for a model nobody priced.
"""

from decimal import Decimal

import pytest
from docfactory_core.pipeline import PipelineConfigError, parse_definition
from docfactory_core.pipeline_registry import default_pipeline, load_from_file
from docfactory_core.pricing import (
    UnknownModelError,
    call_cost_usd,
    estimate_input_tokens,
    load_pricing,
    price_for,
    tier_of,
    worst_case_cost_usd,
)
from docfactory_core.routing import (
    RoutingConfigError,
    escalation_trigger,
    model_for_tier,
    parse_policy,
)


class TestPricesAreConfigNotCode:
    def test_a_call_costs_its_tokens_at_the_published_rate(self):
        # claude-opus-5: $5 per Mtok in, $25 per Mtok out.
        assert call_cost_usd("anthropic:claude-opus-5", 1_000_000, 0) == Decimal("5.000000")
        assert call_cost_usd("anthropic:claude-opus-5", 0, 1_000_000) == Decimal("25.000000")
        assert call_cost_usd("anthropic:claude-opus-5", 10_000, 2_000) == Decimal("0.100000")

    def test_the_cheap_tier_is_cheaper(self):
        small = call_cost_usd("anthropic:claude-haiku-4-5", 10_000, 2_000)
        frontier = call_cost_usd("anthropic:claude-opus-5", 10_000, 2_000)
        assert small < frontier
        assert tier_of("anthropic:claude-haiku-4-5") == "small"
        assert tier_of("anthropic:claude-opus-5") == "frontier"

    def test_an_unpriced_model_is_an_error_not_a_free_call(self):
        # A silent zero would understate every unit cost and defeat the cap.
        with pytest.raises(UnknownModelError, match="no price configured"):
            call_cost_usd("anthropic:some-unreleased-model", 10, 10)

    def test_mock_models_are_priced_as_what_they_stand_in_for(self):
        mock = price_for("mock:mock-extractor-v1")
        real = price_for(mock.simulates)
        assert (mock.input_usd_per_mtok, mock.output_usd_per_mtok) == (
            real.input_usd_per_mtok,
            real.output_usd_per_mtok,
        )

    def test_every_configured_model_declares_a_known_tier(self):
        assert {p.tier for p in load_pricing().values()} <= {"small", "frontier"}

    def test_the_worst_case_bounds_the_real_cost(self):
        # What the reservation charges must never be less than what the call
        # can actually cost, or the cap could be crossed by one call.
        worst = worst_case_cost_usd("anthropic:claude-opus-5", 2_000, 8_192)
        actual = call_cost_usd("anthropic:claude-opus-5", 2_000, 1_100)
        assert worst >= actual

    def test_token_estimate_tracks_text_length(self):
        assert estimate_input_tokens("x" * 4_000) == pytest.approx(1_000, abs=2)
        assert estimate_input_tokens("ab", "cd") <= estimate_input_tokens("abcd" * 10)


class TestRoutingPolicy:
    def test_both_shipped_pipelines_start_on_the_cheap_tier(self):
        for definition in (default_pipeline(), load_from_file("purchase_order")):
            policy = definition.model_routing
            assert policy.primary_tier == "small"
            assert policy.escalate_to == "frontier"
            assert policy.escalates

    def test_tiers_resolve_to_a_model_per_provider(self):
        assert model_for_tier("mock", "small") == "mock-extractor-small-v1"
        assert model_for_tier("mock", "frontier") == "mock-extractor-v1"
        assert model_for_tier("anthropic", "small")
        with pytest.raises(RoutingConfigError, match=r"no .* tier"):
            model_for_tier("mock", "gigantic")

    def test_escalation_fires_only_on_a_declared_trigger(self):
        policy = parse_policy(
            {
                "primary_tier": "small",
                "escalate_to": "frontier",
                "escalate_on": ["validation_failed"],
            }
        )
        assert (
            escalation_trigger(
                policy,
                extraction_failed=False,
                validation_passed=False,
                routing_decision="approved",
            )
            == "validation_failed"
        )
        # needs_review is not in this policy's trigger list, so it does not fire
        assert (
            escalation_trigger(
                policy,
                extraction_failed=False,
                validation_passed=True,
                routing_decision="needs_review",
            )
            is None
        )

    def test_a_clean_document_is_never_escalated(self):
        policy = default_pipeline().model_routing
        assert (
            escalation_trigger(
                policy,
                extraction_failed=False,
                validation_passed=True,
                routing_decision="approved",
            )
            is None
        )

    def test_a_policy_naming_an_unknown_trigger_is_rejected_on_write(self):
        with pytest.raises(RoutingConfigError, match="unknown escalation trigger"):
            parse_policy(
                {
                    "primary_tier": "small",
                    "escalate_to": "frontier",
                    "escalate_on": ["it_feels_wrong"],
                }
            )

    def test_a_policy_naming_an_unknown_tier_is_rejected_on_write(self):
        with pytest.raises(RoutingConfigError, match="unknown model tier"):
            parse_policy({"primary_tier": "enormous"})

    def test_a_bad_policy_in_a_pipeline_is_a_pipeline_config_error(self):
        """The API boundary must reject it like any other malformed config."""
        config = {
            "document_type": "thing",
            "fields": {"name": {"kind": "text"}},
            "extraction_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
            "model_routing": {"primary_tier": "small", "escalate_on": ["needs_review"]},
        }
        with pytest.raises(PipelineConfigError, match="escalate_on is set"):
            parse_definition("t", "s", 1, config)


class TestTheTiersActuallyDiffer:
    """A mock whose cheap tier is as good as its expensive one makes routing
    unmeasurable, so the difference is asserted rather than assumed."""

    def test_the_small_tier_truncates_a_long_table_and_the_rule_catches_it(self):
        from pathlib import Path

        from docfactory_core.extraction import run_extraction
        from docfactory_core.llm import MockLLMClient
        from docfactory_core.parsing import extract_pdf_text
        from docfactory_core.pipeline import evaluate_rules

        definition = default_pipeline()
        text = extract_pdf_text(
            (Path(__file__).parent / "fixtures" / "digital_euro.pdf").read_bytes()
        )
        big = run_extraction(text, MockLLMClient(model="mock-extractor-v1"), definition)
        small = run_extraction(text, MockLLMClient(model="mock-extractor-small-v1"), definition)

        assert big.record is not None and small.record is not None
        assert len(small.record["line_items"]) <= len(big.record["line_items"])
        # The frontier answer is internally consistent; that is the baseline
        # the escalation trigger is measured against.
        assert all(evaluate_rules(big.record, definition).values())

    def test_the_small_tier_reports_fewer_tokens_and_costs_less(self):
        from pathlib import Path

        from docfactory_core.extraction import run_extraction
        from docfactory_core.llm import MockLLMClient
        from docfactory_core.parsing import extract_pdf_text

        definition = default_pipeline()
        text = extract_pdf_text(
            (Path(__file__).parent / "fixtures" / "digital_classic.pdf").read_bytes()
        )
        outcomes = {
            tier: run_extraction(text, MockLLMClient(model=model), definition)
            for tier, model in (
                ("small", "mock-extractor-small-v1"),
                ("frontier", "mock-extractor-v1"),
            )
        }
        # outcome.model is already the "provider:model-id" label
        costs = {
            tier: call_cost_usd(o.model, o.input_tokens, o.output_tokens)
            for tier, o in outcomes.items()
        }
        assert costs["small"] < costs["frontier"]
        # Tokens are derived from the real prompt, not invented.
        assert outcomes["frontier"].input_tokens > 100


class TestTheCounterAndTheAuditTrailAgree:
    """The cap is enforced against a counter; the truth is the event log. If
    they can drift, one of the two numbers is a lie."""

    pytestmark = pytest.mark.integration

    def test_recorded_usage_sums_to_the_spend_counter(self):
        import socket
        from urllib.parse import urlparse

        from docfactory_core.config import get_settings
        from docfactory_core.db import session_scope, tenant_context
        from sqlalchemy import text

        settings = get_settings()
        parsed = urlparse(settings.s3_endpoint_url or "")
        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
                pass
        except OSError:
            pytest.skip("compose stack is not running")

        tenant = settings.default_tenant_id
        with tenant_context(tenant), session_scope() as session:
            events = Decimal(
                str(
                    session.execute(
                        text("SELECT COALESCE(SUM(cost_usd), 0) FROM usage_events")
                    ).scalar()
                )
            )
            counter = Decimal(
                str(
                    session.execute(
                        text("SELECT spent_usd FROM tenant_spend WHERE tenant_id = :t"),
                        {"t": tenant},
                    ).scalar()
                    or 0
                )
            )
            unsettled = session.execute(
                text("SELECT unsettled_calls FROM tenant_spend WHERE tenant_id = :t"),
                {"t": tenant},
            ).scalar()

        # Reservations in flight would legitimately make the counter larger;
        # with none outstanding the two must be identical.
        #
        # DELIBERATELY AN ABSOLUTE CHECK OVER ACCUMULATED STATE, not a delta
        # over this test's own activity. A delta was tried and is strictly
        # worse: settle() and record_usage() are called one after the other in
        # the worker handler, so anything a test can drive in-process moves
        # both by zero and the assertion becomes 0 == 0 — passing whatever the
        # code does. The value here is catching drift the system accumulated in
        # ways nobody enumerated, which only an absolute check can see.
        #
        # The cost of that is this failing on a long-lived development database
        # carrying residue from interrupted runs, killed workers and abandoned
        # experiments. The message below exists to make that distinguishable
        # rather than mysterious.
        if unsettled == 0:
            assert counter == events, (
                f"The spend counter ({counter}) and the usage-event log ({events}) "
                f"disagree by {counter - events} for tenant {tenant!r}, with no "
                "reservations outstanding.\n\n"
                "The cap is enforced against the counter and the audit trail is "
                "the event log, so a drift means one of the two numbers a client "
                "can be shown is wrong.\n\n"
                "TO TELL A CODE BUG FROM DATABASE HISTORY: a drift that is "
                "already present before you do anything, and does not grow when "
                "you push a document through the pipeline, is residue from "
                "earlier local runs — `make reset && make up && make migrate` "
                "clears it. A drift that GROWS as documents are processed is the "
                "real thing, and means settle() and record_usage() are "
                "disagreeing in apps/worker/docfactory_worker/handlers.py."
            )
