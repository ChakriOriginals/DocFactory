"""Schema-guided extraction runner.

One implementation shared by the worker's extract stage and the eval harness,
so the eval measures exactly the code path production runs. Flow: LLM call
(structured outputs) -> Pydantic validation (which canonicalizes via the R2
normalizer) -> on failure, ONE retry with the validation error appended ->
give up with the raw output preserved for debugging.

Spans use OpenInference attribute names so Phoenix renders LLM calls with
prompt/response/token panels. Until setup_tracing() runs, spans are no-ops.
"""

import json
import time
from dataclasses import dataclass

from opentelemetry import trace
from pydantic import ValidationError

from docfactory_core.llm import LLMClient, LLMRefusalError
from docfactory_core.schemas import INVOICE_JSON_SCHEMA, Invoice

tracer = trace.get_tracer("docfactory")

SYSTEM_PROMPT = """\
You extract structured data from invoice documents. Invoices may be in English or German \
(Rechnung); layouts vary and text extraction may interleave columns or split words.

Return a single JSON object matching the provided schema. Canonical value rules:
- dates: ISO-8601 (YYYY-MM-DD), regardless of how they are printed
- monetary amounts: plain decimal strings with '.' as decimal separator, no thousands \
separators, no currency symbols (e.g. "25832.09" for a printed "25.832,09 €")
- tax_rate: decimal fraction as a string (a printed "19%" becomes "0.19")
- currency: ISO 4217 code
- line_items: one entry per row of the invoice's item table, in document order
- vendor: the issuing company's name; repair obvious text-extraction artifacts \
(split or spaced-out letters)"""


@dataclass
class ExtractionOutcome:
    invoice: Invoice | None
    raw_output: str
    model: str  # "provider:model-id"
    attempts: int
    input_tokens: int
    output_tokens: int
    latency_ms: int
    error: str | None = None


def run_extraction(text: str, client: LLMClient) -> ExtractionOutcome:
    messages: list[dict] = [
        {
            "role": "user",
            "content": f"Extract the invoice fields from this document text:\n\n{text}",
        }
    ]
    model_label = f"{client.provider}:{client.model}"
    input_tokens = output_tokens = 0
    raw_output = ""
    error: str | None = None
    started = time.monotonic()

    for attempt in (1, 2):
        with tracer.start_as_current_span("llm.extract") as span:
            # OpenInference semantic conventions — Phoenix renders these natively.
            span.set_attribute("openinference.span.kind", "LLM")
            span.set_attribute("llm.model_name", client.model)
            span.set_attribute("llm.provider", client.provider)
            span.set_attribute("input.value", messages[-1]["content"])
            span.set_attribute("retry.attempt", attempt)
            try:
                response = client.complete(
                    system=SYSTEM_PROMPT, messages=messages, output_schema=INVOICE_JSON_SCHEMA
                )
            except LLMRefusalError as exc:
                error = str(exc)
                span.record_exception(exc)
                break
            raw_output = response.content
            input_tokens += response.input_tokens or 0
            output_tokens += response.output_tokens or 0
            span.set_attribute("output.value", raw_output)
            if response.input_tokens is not None:
                span.set_attribute("llm.token_count.prompt", response.input_tokens)
            if response.output_tokens is not None:
                span.set_attribute("llm.token_count.completion", response.output_tokens)

            try:
                invoice = Invoice.model_validate(json.loads(_strip_fences(raw_output)))
                return ExtractionOutcome(
                    invoice=invoice,
                    raw_output=raw_output,
                    model=model_label,
                    attempts=attempt,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            except (json.JSONDecodeError, ValidationError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                span.set_attribute("error.validation", error)
                messages.extend(
                    (
                        {"role": "assistant", "content": raw_output},
                        {
                            "role": "user",
                            "content": (
                                "That JSON failed validation with these errors:\n"
                                f"{exc}\n\nRespond with only the corrected JSON object."
                            ),
                        },
                    )
                )

    return ExtractionOutcome(
        invoice=None,
        raw_output=raw_output,
        model=model_label,
        attempts=2,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=int((time.monotonic() - started) * 1000),
        error=error,
    )


def _strip_fences(content: str) -> str:
    s = content.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.removesuffix("```").removeprefix("json").strip()
    return s
