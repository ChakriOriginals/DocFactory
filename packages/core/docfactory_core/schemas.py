"""Extraction schema for the invoice pipeline.

The Pydantic model runs every incoming value through the shared normalizer
(R2), so even a model response with localized formatting is canonicalized at
the validation boundary. INVOICE_JSON_SCHEMA is the hand-written strict schema
sent to the LLM via structured outputs — hand-written rather than generated
from Pydantic so it stays within the API's supported JSON-Schema subset
(additionalProperties: false everywhere, no array-length constraints).
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_serializer

from docfactory_core.normalize import normalize_amount, normalize_date, normalize_rate

Money = Annotated[Decimal, BeforeValidator(normalize_amount)]
Rate = Annotated[Decimal, BeforeValidator(normalize_rate)]
IsoDate = Annotated[date, BeforeValidator(normalize_date)]


class InvoiceLineItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    description: str = Field(min_length=1)
    quantity: Money
    unit_price: Money
    amount: Money

    @field_serializer("quantity", "unit_price", "amount")
    def _decimal_as_str(self, value: Decimal) -> str:
        return str(value)


class Invoice(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vendor: str = Field(min_length=1)
    invoice_number: str = Field(min_length=1)
    invoice_date: IsoDate
    due_date: IsoDate
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    subtotal: Money
    tax_rate: Rate
    tax: Money
    total: Money
    line_items: list[InvoiceLineItem] = Field(min_length=1)

    @field_serializer("subtotal", "tax_rate", "tax", "total")
    def _decimal_as_str(self, value: Decimal) -> str:
        return str(value)


# The canonical scalar field names, in report order. Shared by the worker's
# extraction_fields flattening and the eval harness so the two never drift.
SCALAR_FIELD_NAMES = (
    "vendor",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "subtotal",
    "tax_rate",
    "tax",
    "total",
)

_MONEY_SCHEMA = {
    "type": "string",
    "description": (
        "Plain decimal string: '.' as decimal separator, no thousands "
        'separators, no currency symbols. e.g. "25832.09"'
    ),
}

INVOICE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [*SCALAR_FIELD_NAMES, "line_items"],
    "properties": {
        "vendor": {"type": "string", "description": "The issuing company's name as printed"},
        "invoice_number": {"type": "string", "description": "Exactly as printed on the document"},
        "invoice_date": {"type": "string", "format": "date", "description": "ISO-8601 YYYY-MM-DD"},
        "due_date": {"type": "string", "format": "date", "description": "ISO-8601 YYYY-MM-DD"},
        "currency": {"type": "string", "enum": ["USD", "EUR"], "description": "ISO 4217 code"},
        "subtotal": _MONEY_SCHEMA,
        "tax_rate": {
            "type": "string",
            "description": 'Decimal fraction as a string: 19% -> "0.19", 6.25% -> "0.0625"',
        },
        "tax": _MONEY_SCHEMA,
        "total": _MONEY_SCHEMA,
        "line_items": {
            "type": "array",
            "description": "One entry per row of the invoice's item table, in document order",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["description", "quantity", "unit_price", "amount"],
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": "string", "description": 'e.g. "3"'},
                    "unit_price": _MONEY_SCHEMA,
                    "amount": _MONEY_SCHEMA,
                },
            },
        },
    },
}
