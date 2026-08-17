"""OpenTelemetry setup + trace propagation, exported to Phoenix.

Design: one trace per document. The API starts the trace at upload; the W3C
traceparent is carried inside each queue message body (SQS has no header
channel we control locally), and each worker stage attaches its spans as
children. Opening any document_id in Phoenix shows upload -> parse ->
extract -> validate with prompts, responses, and timings.

Until setup_tracing() is called, all span code in this repo is a no-op —
tests and scripts don't need Phoenix running.
"""

import logging

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from docfactory_core.config import get_settings

log = logging.getLogger(__name__)

_propagator = TraceContextTextMapPropagator()


def setup_tracing(service: str) -> None:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    endpoint = f"{get_settings().phoenix_collector_endpoint}/v1/traces"
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    log.info("tracing configured", extra={"exporter": endpoint, "otel_service": service})


def inject_trace_context(payload: dict) -> dict:
    """Stamp the current trace context into a queue message payload."""
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    if carrier:
        payload["traceparent"] = carrier.get("traceparent")
    return payload


def extract_trace_context(payload: dict) -> Context | None:
    """Recover the upload's trace context from a queue message payload."""
    traceparent = payload.get("traceparent")
    if not traceparent:
        return None
    return _propagator.extract({"traceparent": traceparent})
