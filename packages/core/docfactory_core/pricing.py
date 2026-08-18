"""Model prices, and the cost of one call.

Prices are configuration (`config/model_pricing.json`), not constants: adding a
model or reacting to a price change is a config edit. A model with no price is
an error rather than a silent zero — an unpriced call would quietly understate
every unit-cost number downstream and defeat the budget cap.

Mock models are priced at the rates of the real model they stand in for. That
makes a mock-mode cost rollup an honest arithmetic estimate over *real* token
counts (the mock's token counts are derived from the actual prompt and
response) rather than an invented figure — what it is not is a measurement of
the real model's token efficiency, which needs an `anthropic`-mode run.
"""

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "model_pricing.json"

# extractions.cost_usd is NUMERIC(12, 6); quantize to match so the stored value
# and the computed value can never disagree.
_CENT_MILLIONTH = Decimal("0.000001")
_PER_MILLION = Decimal("1000000")


class UnknownModelError(KeyError):
    """A call was made with a model that has no price."""


@dataclass(frozen=True)
class ModelPrice:
    model: str
    tier: str
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    simulates: str | None = None

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        total = (
            Decimal(input_tokens) * self.input_usd_per_mtok
            + Decimal(output_tokens) * self.output_usd_per_mtok
        ) / _PER_MILLION
        return total.quantize(_CENT_MILLIONTH, rounding=ROUND_HALF_UP)


@lru_cache
def load_pricing(path: str | Path | None = None) -> dict[str, ModelPrice]:
    payload = json.loads(Path(path or CONFIG_PATH).read_text())
    return {
        model: ModelPrice(
            model=model,
            tier=str(spec["tier"]),
            input_usd_per_mtok=Decimal(str(spec["input"])),
            output_usd_per_mtok=Decimal(str(spec["output"])),
            simulates=spec.get("simulates"),
        )
        for model, spec in payload["models"].items()
    }


def price_for(model: str) -> ModelPrice:
    """The price of a "provider:model-id" label."""
    try:
        return load_pricing()[model]
    except KeyError as exc:
        raise UnknownModelError(
            f"no price configured for model {model!r}; add it to {CONFIG_PATH.name} "
            f"(known: {sorted(load_pricing())})"
        ) from exc


def call_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """What one model call cost, from its actual token usage."""
    return price_for(model).cost(input_tokens, output_tokens)


def tier_of(model: str) -> str:
    return price_for(model).tier


def worst_case_cost_usd(model: str, input_tokens: int, max_output_tokens: int) -> Decimal:
    """The most one call can possibly cost.

    The budget reservation charges this before the call and settles down to the
    truth after, so a tenant can never be pushed past its cap by a call whose
    size was not yet known. `max_output_tokens` is the hard ceiling the request
    itself carries, which is what makes this a real bound rather than a guess.
    """
    return price_for(model).cost(input_tokens, max_output_tokens)


def estimate_input_tokens(*texts: str) -> int:
    """Token estimate for text about to be sent.

    Four characters per token is the standard rule of thumb and is what the
    mock backend also reports, so mock-mode reservations and mock-mode actuals
    are computed the same way. A real tokenizer (or the count_tokens endpoint)
    would sharpen this; it is used only to size a reservation that gets settled
    against the provider's own usage report moments later.
    """
    return sum(len(text) for text in texts) // 4 + 1
