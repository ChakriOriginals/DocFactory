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


def get_llm_client(settings: Settings | None = None) -> "MockLLMClient | AnthropicLLMClient":
    settings = settings or get_settings()
    if settings.model_provider == "anthropic":
        return AnthropicLLMClient(settings)
    return MockLLMClient()


class AnthropicLLMClient:
    """The real backend. It receives the pipeline definition for interface
    parity with the mock but needs nothing from it: the prompt and the output
    schema, both built from that definition, are what the model sees."""

    provider = "anthropic"

    def __init__(self, settings: Settings) -> None:
        import anthropic

        self.model = settings.anthropic_model
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

_US_MONEY = re.compile(r"\$\s?[\d,]+\.\d{2}")
_EU_MONEY = re.compile("[\\d.]*\\d+,\\d{2}[\\s\\u00a0\\u202f]*\u20ac")
_DATES = re.compile(r"\d{2}/\d{2}/\d{4}|\d{2}\.\d{2}\.\d{4}|[A-Z][a-z]{2,8} \d{1,2}, \d{4}")
_INVOICE_NO = re.compile(r"INV-\d{4}-\d{5}|RE-\d{4}/\d{4}|\b\d{6}-\d{4}\b")
_RATE = re.compile(r"([\d.,]+)\s*%")
_INT_TOKEN = re.compile(r"^\d+$")

_SKIP_PREFIXES = (
    "invoice", "due date", "bill to", "billed to", "tax invoice",
    "rechnung", "fällig", "ust-idnr", "pos.", "description", "qty",
    "zahlbar", "please reference",
)  # fmt: skip
_TITLE_WORDS = {"INVOICE", "TAXINVOICE", "RECHNUNG"}
_ITEM_HEADERS = ("DESCRIPTION", "QTY ITEM", "Pos. Beschreibung")
_SUBTOTAL_KEYS = ("Subtotal", "Zwischensumme")
_TAX_KEYS = ("Sales Tax", "Tax ", "MwSt")
_TOTAL_KEYS = ("Total Due", "Amount Due", "Gesamtbetrag")


class MockLLMClient:
    provider = "mock"
    model = "mock-extractor-v1"

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
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
        text = self._document_text(messages)
        payload = self._heuristic_extract(text)
        # Labelled error injection for the calibration study. Off unless
        # MOCK_CORRUPTION_RATE is set; deterministic in (seed, document) so a
        # study re-run reproduces the same corpus and can recompute the label
        # without it being threaded back through this interface.
        if self._corruption_rate > 0:
            plan = plan_corruption(text, rate=self._corruption_rate, seed=self._corruption_seed)
            payload = apply_corruption(payload, plan, seed=self._corruption_seed)
        content = json.dumps(payload, ensure_ascii=False)
        return LLMResponse(
            content=content,
            model=self.model,
            stop_reason="end_turn",
            input_tokens=len(text) // 4,
            output_tokens=len(content) // 4,
        )

    @staticmethod
    def _document_text(messages: list[dict]) -> str:
        for message in messages:
            if message.get("role") == "user" and "document text:" in str(message.get("content")):
                return str(message["content"]).split("document text:", 1)[1]
        return str(messages[-1].get("content", ""))

    def _heuristic_extract(self, text: str) -> dict:
        lines = self._rejoin_smallcaps([line.strip() for line in text.splitlines() if line.strip()])
        currency = "EUR" if "€" in text else "USD"
        money_re = _EU_MONEY if currency == "EUR" else _US_MONEY

        number_match = _INVOICE_NO.search(text)
        dates = _DATES.findall(text)
        rate_match = _RATE.search(text)

        subtotal = self._labeled_amount(lines, _SUBTOTAL_KEYS, money_re)
        tax = self._labeled_amount(lines, _TAX_KEYS, money_re)
        total = self._labeled_amount(lines, _TOTAL_KEYS, money_re)
        items = self._line_items(lines, money_re, currency)

        if subtotal is None and items:
            from docfactory_core.normalize import normalize_amount

            subtotal = str(sum(normalize_amount(i["amount"]) for i in items))
        return {
            "vendor": self._vendor(lines),
            "invoice_number": number_match.group(0) if number_match else "UNKNOWN",
            "invoice_date": dates[0] if dates else "1970-01-01",
            "due_date": dates[1] if len(dates) > 1 else (dates[0] if dates else "1970-01-01"),
            "currency": currency,
            "subtotal": subtotal or "0",
            "tax_rate": rate_match.group(0) if rate_match else "0",
            "tax": tax or "0",
            "total": total or subtotal or "0",
            "line_items": items
            or [{"description": "unknown", "quantity": "1", "unit_price": "0", "amount": "0"}],
        }

    @staticmethod
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

    @staticmethod
    def _vendor(lines: list[str]) -> str:
        for line in lines:
            plain = line.replace(" ", "").upper()
            if len(line) <= 1 or plain in _TITLE_WORDS:
                continue
            if any(line.lower().startswith(prefix) for prefix in _SKIP_PREFIXES):
                continue
            # modern layout glues the logo initial on: "Fernandez-Harris F"
            tokens = line.split()
            if len(tokens) > 1 and len(tokens[-1]) == 1 and tokens[-1] == line[0]:
                tokens = tokens[:-1]
            return " ".join(tokens)
        return "UNKNOWN"

    @staticmethod
    def _labeled_amount(
        lines: list[str], keys: tuple[str, ...], money_re: re.Pattern
    ) -> str | None:
        for line in lines:
            if any(key in line for key in keys):
                amounts = money_re.findall(line)
                if amounts:
                    return amounts[-1]
        return None

    @staticmethod
    def _line_items(lines: list[str], money_re: re.Pattern, currency: str) -> list[dict]:
        start = next((i for i, ln in enumerate(lines) if any(h in ln for h in _ITEM_HEADERS)), None)
        if start is None:
            return []
        items = []
        for line in lines[start + 1 :]:
            if any(key in line for key in _SUBTOTAL_KEYS):
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
            description = " ".join(tokens).strip(" ·")
            if description:
                items.append(
                    {
                        "description": description,
                        "quantity": quantity,
                        "unit_price": amounts[-2],
                        "amount": amounts[-1],
                    }
                )
        return items
