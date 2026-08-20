"""LLM client: one interface, two backends behind MODEL_PROVIDER.

- mock: deterministic heuristic extraction from the document text, small
  simulated latency, no network. The default for tests and bulk runs — real
  API budget is never spent implicitly.
- anthropic: real calls via the official SDK using structured outputs
  (output_config.format), so the API itself constrains the response to the
  schema; adaptive thinking stays on with configurable effort.
"""

import json
import random
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from docfactory_core.config import Settings, get_settings
from docfactory_core.corruption import apply_corruption, plan_corruption
from docfactory_core.pipeline import PipelineDefinition


class LLMRefusalError(RuntimeError):
    """The model declined the request (stop_reason == refusal)."""


@dataclass(frozen=True)
class LLMResponse:
    content: str
    model: str
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        definition: "PipelineDefinition | None" = None,
    ) -> LLMResponse: ...


def get_llm_client(
    settings: Settings | None = None, model: str | None = None
) -> "MockLLMClient | AnthropicLLMClient":
    """A client for a specific model, or the deployment default.

    Routing picks the model id from the pipeline's tier; everything else in the
    system keeps talking to one interface.

    CLIENTS ARE REUSED, not rebuilt per document. This is called once per
    extraction attempt and again on every escalation, so a fresh client per
    call means a fresh httpx connection pool per document — TLS handshakes
    that did not need to happen, and file descriptors left to the garbage
    collector to reclaim. Reuse is what the Anthropic SDK documents: the client
    is thread-safe and intended to be long-lived.

    Cached on (provider, model) rather than on the Settings object, which is
    unhashable. Settings are themselves process-global via an lru_cache, so
    this adds no staleness that was not already there — with one caveat worth
    stating: changing MODEL_PROVIDER or the API key inside a live process will
    not be picked up. Nothing does that; a task definition change rolls the
    task.
    """
    settings = settings or get_settings()
    return _client_for(settings.model_provider, model)


@lru_cache(maxsize=8)
def _client_for(provider: str, model: str | None) -> "MockLLMClient | AnthropicLLMClient":
    if provider == "anthropic":
        return AnthropicLLMClient(get_settings(), model=model)
    return MockLLMClient(model=model)


class AnthropicLLMClient:
    """The real backend. It receives the pipeline definition for interface
    parity with the mock but needs nothing from it: the prompt and the output
    schema, both built from that definition, are what the model sees."""

    provider = "anthropic"

    def __init__(self, settings: Settings, model: str | None = None) -> None:
        import anthropic

        self.model = model or settings.anthropic_model
        self._settings = settings
        # Explicit key if configured; otherwise the SDK's own resolution
        # (env var / `ant auth login` profile).
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None)

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        definition: "PipelineDefinition | None" = None,
    ) -> LLMResponse:
        output_config: dict = {"effort": self._settings.llm_effort}
        if output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self._settings.llm_max_tokens,
            system=system,
            messages=messages,
            output_config=output_config,
        )
        if response.stop_reason == "refusal":
            raise LLMRefusalError(f"model refused: {response.stop_details}")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return LLMResponse(
            content=text,
            model=response.model,
            stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


# --- mock backend ---------------------------------------------------------
#
# The mock stands in for the model, so it has to be able to read a document
# type it was not written for. The *algorithms* here are generic — find a
# labelled amount, read the dates in printed order, walk an item table — while
# the vocabulary that makes them work on a particular layout (which words
# label a total, what an order number looks like) is per-type configuration in
# config/mock_extraction/. A new document type therefore needs a hints file,
# not a new branch in this module.

_US_MONEY = re.compile(r"\$\s?[\d,]+\.\d{2}")
_EU_MONEY = re.compile("[\\d.]*\\d+,\\d{2}[\\s\\u00a0\\u202f]*\u20ac")
_DATES = re.compile(r"\d{2}/\d{2}/\d{4}|\d{2}\.\d{2}\.\d{4}|[A-Z][a-z]{2,8} \d{1,2}, \d{4}")
_RATE = re.compile(r"([\d.,]+)\s*%")
_INT_TOKEN = re.compile(r"^\d+$")

HINTS_DIR = Path(__file__).resolve().parents[3] / "config" / "mock_extraction"
MODELS_PATH = Path(__file__).resolve().parents[3] / "config" / "mock_models.json"

DEFAULT_MOCK_MODEL = "mock-extractor-v1"


class MockHintsError(RuntimeError):
    """No mock reading hints for a pipeline — the mock cannot fake that type."""


@lru_cache
def load_profile(model: str) -> dict:
    """How this mock model behaves.

    The tiers differ in quality as well as price — a mock whose cheap tier is
    exactly as good as its expensive one makes routing unmeasurable. The
    degradations are simulated and declared in config/mock_models.json.
    """
    profiles = json.loads(MODELS_PATH.read_text())["models"]
    try:
        return profiles[model]
    except KeyError as exc:
        raise MockHintsError(
            f"no mock behaviour profile for {model!r}; known: {sorted(profiles)}"
        ) from exc


@lru_cache
def load_hints(slug: str, version: int = 1) -> dict:
    path = HINTS_DIR / f"{slug}_v{version}.json"
    if not path.is_file():
        raise MockHintsError(
            f"no mock extraction hints for pipeline {slug!r} v{version} ({path}). "
            "The mock backend reads documents heuristically and needs to be told "
            "how this document type prints its fields; use MODEL_PROVIDER=anthropic "
            "for a type without hints."
        )
    return json.loads(path.read_text())


class MockLLMClient:
    provider = "mock"

    def __init__(self, settings: Settings | None = None, model: str | None = None) -> None:
        settings = settings or get_settings()
        self.model = model or DEFAULT_MOCK_MODEL
        self._profile = load_profile(self.model)
        self._corruption_rate = settings.mock_corruption_rate
        self._corruption_seed = settings.mock_corruption_seed

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        definition: PipelineDefinition | None = None,
    ) -> LLMResponse:
        time.sleep(random.uniform(0.02, 0.08))
        if definition is None:
            from docfactory_core.pipeline_registry import default_pipeline

            definition = default_pipeline()
        text = self._document_text(messages)
        payload = heuristic_extract(
            text, load_hints(definition.slug, definition.version), self._profile
        )
        # Labelled error injection for the calibration study. Off unless
        # MOCK_CORRUPTION_RATE is set; deterministic in (seed, document) so a
        # study re-run reproduces the same corpus and can recompute the label
        # without it being threaded back through this interface.
        if self._corruption_rate > 0:
            plan = plan_corruption(text, rate=self._corruption_rate, seed=self._corruption_seed)
            payload = apply_corruption(payload, plan, seed=self._corruption_seed)
        content = json.dumps(payload, ensure_ascii=False)
        # Token counts cover the whole prompt — system instructions and schema
        # included — not just the document, so a mock-mode cost number reflects
        # what the pipeline actually sends. Four characters per token is the
        # standard approximation; a real run replaces it with the provider's
        # own count.
        prompt = system + "".join(str(message.get("content", "")) for message in messages)
        return LLMResponse(
            content=content,
            model=self.model,
            stop_reason="end_turn",
            input_tokens=len(prompt) // 4 + len(json.dumps(output_schema or {})) // 4,
            output_tokens=len(content) // 4,
        )

    @staticmethod
    def _document_text(messages: list[dict]) -> str:
        for message in messages:
            if message.get("role") == "user" and "document text:" in str(message.get("content")):
                return str(message["content"]).split("document text:", 1)[1]
        return str(messages[-1].get("content", ""))


def heuristic_extract(text: str, hints: dict, profile: dict | None = None) -> dict:
    """Read a document the way an extractor of this tier would.

    `hints` say how the document type prints its fields; `profile` says how
    capable this model is at reading them.
    """
    profile = profile or load_profile(DEFAULT_MOCK_MODEL)
    repairs = frozenset(profile.get("repairs", ()))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if "smallcaps" in repairs:
        lines = _rejoin_smallcaps(lines)
    currency = "EUR" if "\u20ac" in text else "USD"
    money_re = _EU_MONEY if currency == "EUR" else _US_MONEY

    record: dict = {}
    for name in hints.get("currency_fields", ()):
        record[name] = currency

    for name, pattern in hints.get("id_fields", {}).items():
        match = re.search(pattern, text)
        record[name] = match.group(0) if match else "UNKNOWN"

    for name, rules in hints.get("heading_fields", {}).items():
        record[name] = _heading_value(lines, rules, trim_logo="logo_initial" in repairs)

    for name, rules in hints.get("labeled_fields", {}).items():
        record[name] = _labeled_value(lines, rules)

    # Dates in printed order: the hints say which field each position is.
    dates = _DATES.findall(text)
    for index, name in enumerate(hints.get("date_fields", ())):
        if len(dates) > index:
            record[name] = dates[index]
        else:
            record[name] = dates[0] if dates else "1970-01-01"

    for name in hints.get("rate_fields", ()):
        match = _RATE.search(text)
        record[name] = match.group(0) if match else "0"

    table = hints.get("table")
    rows: list[dict] = []
    if table:
        rows = _table_rows(lines, money_re, currency, table)
        # A weaker model loses rows off the end of a long table. Simulated, and
        # chosen because it breaks the sum-to-subtotal rule — a failure the
        # router can see and act on.
        limit = profile.get("max_table_rows")
        if limit:
            rows = rows[:limit]
        record[table["field"]] = rows or [dict(table["empty_row"])]

    for name, keys in hints.get("labeled_amounts", {}).items():
        record[name] = _labeled_amount(lines, tuple(keys), money_re)
    if "amount_fallbacks" in repairs:
        for name, fallback in hints.get("amount_fallbacks", {}).items():
            if record.get(name) is None:
                record[name] = _fallback_amount(fallback, record, rows)
    for name in hints.get("labeled_amounts", {}):
        if record.get(name) is None:
            record[name] = "0"
    return record


def _fallback_amount(spec: str, record: dict, rows: list[dict]) -> str | None:
    """A missing amount derived from what was read: 'sum:<column>' or 'field:<name>'."""
    kind, _, argument = spec.partition(":")
    if kind == "sum":
        if not rows:
            return None
        from docfactory_core.normalize import normalize_amount

        return str(sum(normalize_amount(row[argument]) for row in rows))
    if kind == "field":
        return record.get(argument)
    raise MockHintsError(f"unknown amount fallback: {spec!r}")


def _rejoin_smallcaps(lines: list[str]) -> list[str]:
    """The euro layout's small-caps vendor renders as 'B' + 'aum' lines."""
    joined: list[str] = []
    skip_next = False
    for i, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if len(line) == 1 and line.isupper() and nxt[:1].islower():
            joined.append(line + nxt)
            skip_next = True
        else:
            joined.append(line)
    return joined


def _heading_value(lines: list[str], rules: dict, *, trim_logo: bool = True) -> str:
    """The first line that reads like a name rather than a label or a title."""
    skip_prefixes = tuple(rules.get("skip_prefixes", ()))
    title_words = {word.upper() for word in rules.get("title_words", ())}
    for line in lines:
        plain = line.replace(" ", "").upper()
        if len(line) <= 1 or plain in title_words:
            continue
        if any(line.lower().startswith(prefix) for prefix in skip_prefixes):
            continue
        # modern layout glues the logo initial on: "Fernandez-Harris F"
        tokens = line.split()
        if trim_logo and len(tokens) > 1 and len(tokens[-1]) == 1 and tokens[-1] == line[0]:
            tokens = tokens[:-1]
        return " ".join(tokens)
    return "UNKNOWN"


def _labeled_value(lines: list[str], rules: dict) -> str:
    """The text printed after a label, e.g. "Vendor: Ohlmann KG"."""
    labels = tuple(rules.get("labels", ()))
    for line in lines:
        for label in labels:
            if line.startswith(label):
                value = line[len(label) :].strip(" :\t")
                if value:
                    return value
    return "UNKNOWN"


def _labeled_amount(lines: list[str], keys: tuple[str, ...], money_re: re.Pattern) -> str | None:
    for line in lines:
        if any(key in line for key in keys):
            amounts = money_re.findall(line)
            if amounts:
                return amounts[-1]
    return None


def _table_rows(lines: list[str], money_re: re.Pattern, currency: str, table: dict) -> list[dict]:
    headers = tuple(table.get("headers", ()))
    stop_keys = tuple(table.get("stop_keys", ()))
    columns = table["columns"]
    start = next((i for i, ln in enumerate(lines) if any(h in ln for h in headers)), None)
    if start is None:
        return []
    rows = []
    for line in lines[start + 1 :]:
        if any(key in line for key in stop_keys):
            break
        amounts = money_re.findall(line)
        if len(amounts) < 2:
            continue
        remainder = line
        for amount in amounts:
            remainder = remainder.replace(amount, "")
        tokens = remainder.split()
        quantity = "1"
        if currency == "EUR":
            # euro rows: <pos> <description> <qty>
            if tokens and _INT_TOKEN.match(tokens[0]):
                tokens = tokens[1:]
            if tokens and _INT_TOKEN.match(tokens[-1]):
                quantity = tokens.pop()
        elif tokens and _INT_TOKEN.match(tokens[0]):
            # modern rows: <qty> <description>
            quantity = tokens.pop(0)
        elif tokens and _INT_TOKEN.match(tokens[-1]):
            # classic rows: <description> <qty>
            quantity = tokens.pop()
        description = " ".join(tokens).strip(" \u00b7")
        if description:
            rows.append(
                {
                    columns["description"]: description,
                    columns["quantity"]: quantity,
                    columns["unit_price"]: amounts[-2],
                    columns["amount"]: amounts[-1],
                }
            )
    return rows
