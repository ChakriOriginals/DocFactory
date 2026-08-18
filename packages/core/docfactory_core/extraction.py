"""Schema-guided extraction runner.

One implementation shared by the worker's extract stage and the eval harness,
so the eval measures exactly the code path production runs. Flow: LLM call
(structured outputs against the *pipeline's* JSON Schema) -> validation of the
returned object against that same schema -> kind-driven canonicalization via
the R2 normalizer -> on failure, ONE retry with the validation error appended
-> give up with the raw output preserved for debugging.

Nothing here knows what an invoice is. The schema, the prompt and the
canonical forms all come from the pipeline definition, so a second document
type is a definition, not a code path.

Spans use OpenInference attribute names so Phoenix renders LLM calls with
prompt/response/token panels. Until setup_tracing() runs, spans are no-ops.
"""

import json
import time
from dataclasses import dataclass

import jsonschema
from opentelemetry import trace

from docfactory_core.llm import LLMClient, LLMRefusalError
from docfactory_core.pipeline import FieldKind, PipelineDefinition

tracer = trace.get_tracer("docfactory")

# Canonical value rules per field kind. These are properties of the kind, not
# of any document type, so they are stated once here rather than repeated in
# every definition's prompt block.
_KIND_RULES: dict[FieldKind, str] = {
    FieldKind.DATE: "dates: ISO-8601 (YYYY-MM-DD), regardless of how they are printed",
    FieldKind.MONEY: (
        "monetary amounts: plain decimal strings with '.' as decimal separator, no "
        'thousands separators, no currency symbols (e.g. "25832.09" for a printed '
        '"25.832,09 €")'
    ),
    FieldKind.QUANTITY: 'quantities: plain decimal strings (e.g. "3")',
    FieldKind.RATE: 'rates: decimal fractions as strings (a printed "19%" becomes "0.19")',
}


def build_system_prompt(definition: PipelineDefinition) -> str:
    """The extraction prompt for a document type, assembled from its definition."""
    kinds = {spec.kind for spec in definition.fields.values()}
    rules = [text for kind, text in _KIND_RULES.items() if kind in kinds]
    for name, spec in definition.fields.items():
        if spec.kind is FieldKind.ENUM and name not in definition.prompt_field_notes:
            rules.append(f"{name}: one of {', '.join(spec.values)}")
    rules.extend(f"{name}: {note}" for name, note in definition.prompt_field_notes.items())

    head = f"You extract structured data from {definition.document_type} documents."
    if definition.prompt_intro:
        head = f"{head} {definition.prompt_intro}"
    body = "\n".join(f"- {rule}" for rule in rules)
    return (
        f"{head}\n\nReturn a single JSON object matching the provided schema. "
        f"Canonical value rules:\n{body}"
    )


@dataclass
class ExtractionOutcome:
    # The extracted record, canonicalized per the definition's field kinds:
    # Decimals for money, `date` for dates. None when extraction failed.
    record: dict | None
    raw_output: str
    model: str  # "provider:model-id"
    attempts: int
    input_tokens: int
    output_tokens: int
    latency_ms: int
    error: str | None = None


def build_user_message(text: str, definition: PipelineDefinition) -> str:
    """The extraction request for one document.

    The "document text:" marker is load-bearing for the mock backend, which
    recovers the raw document from it — see MockLLMClient._document_text.
    """
    return f"Extract the {definition.document_type} fields from this document text:\n\n{text}"


def run_extraction(
    text: str, client: LLMClient, definition: PipelineDefinition
) -> ExtractionOutcome:
    messages: list[dict] = [{"role": "user", "content": build_user_message(text, definition)}]
    system = build_system_prompt(definition)
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
            span.set_attribute("pipeline.slug", definition.slug)
            try:
                response = client.complete(
                    system=system,
                    messages=messages,
                    output_schema=definition.json_schema,
                    definition=definition,
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
                record = validate_record(json.loads(_strip_fences(raw_output)), definition)
                return ExtractionOutcome(
                    record=record,
                    raw_output=raw_output,
                    model=model_label,
                    attempts=attempt,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            except (jsonschema.ValidationError, ValueError) as exc:
                error = _describe(exc)
                span.set_attribute("error.validation", error)
                messages.extend(
                    (
                        {"role": "assistant", "content": raw_output},
                        {
                            "role": "user",
                            "content": (
                                "That JSON failed validation with these errors:\n"
                                f"{error}\n\nRespond with only the corrected JSON object."
                            ),
                        },
                    )
                )

    return ExtractionOutcome(
        record=None,
        raw_output=raw_output,
        model=model_label,
        attempts=2,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=int((time.monotonic() - started) * 1000),
        error=error,
    )


def validate_record(payload: object, definition: PipelineDefinition) -> dict:
    """Check the model's object against the pipeline schema, then canonicalize.

    Two failure modes, both retryable and both reported to the model in its
    own terms: the shape is wrong (JSON Schema), or a value cannot be read as
    the kind its field declares ("31.02.2026" is not a date).
    """
    jsonschema.validate(payload, definition.json_schema)
    return definition.normalize(payload)


def _describe(exc: Exception) -> str:
    if isinstance(exc, jsonschema.ValidationError):
        path = "/".join(str(part) for part in exc.absolute_path) or "<root>"
        return f"ValidationError: {path}: {exc.message}"
    return f"{type(exc).__name__}: {exc}"


def _strip_fences(content: str) -> str:
    s = content.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.removesuffix("```").removeprefix("json").strip()
    return s
