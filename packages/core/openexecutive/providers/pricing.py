"""Published Anthropic list prices, and a dollar-equivalent for subscription calls.

Why this exists: on the Agent SDK path the app talks to Claude through a
Pro/Max subscription, so no per-request USD figure comes back over the wire —
nothing is metered and nothing is invoiced. That is NOT the same as free. The
subscription carries a usage allowance, and the number that maps to it is a
dollar-equivalent computed from the tokens actually consumed. Reporting zero
there hides the only figure an operator can budget against.

So the totals in ``/audit/usage`` mix two kinds of number:

* an ACTUAL charge, wired back by OpenRouter per call, and
* an ESTIMATE, computed here from token counts at the list prices below.

Rows carry ``cost_is_estimate`` so the two stay tellable apart. Treat an
estimate as an indicator, not an invoice: Anthropic is free to account for
subscription usage differently from list API prices, and this module cannot
know about a negotiated rate, a batch discount, or a data-residency multiplier.

Prices are USD per million tokens, from
https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-09-09).
Cache writes are the 5-minute rate (1.25x base input); cache reads are 0.1x
base input on every model here. Re-check when adding a model: the multipliers
are not universal (Claude Fable 5.1 reads at 0.025x).
"""

from __future__ import annotations

from typing import NamedTuple


class ModelPrices(NamedTuple):
    """USD per million tokens, by token class."""

    input: float
    output: float
    cache_write_5m: float
    cache_read: float


_PER_MILLION = 1_000_000.0

# Keyed on the exact model id the provider reports. Legacy ids are kept so a
# row written before a model rename still prices, rather than silently
# contributing 0 to the totals.
MODEL_PRICES: dict[str, ModelPrices] = {
    # Current trio.
    "claude-opus-5": ModelPrices(5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5": ModelPrices(2.00, 10.00, 2.50, 0.20),
    "claude-haiku-4-5": ModelPrices(1.00, 5.00, 1.25, 0.10),
    # Same model, dated id — what the Models API reports for Haiku 4.5.
    "claude-haiku-4-5-20251001": ModelPrices(1.00, 5.00, 1.25, 0.10),
    # Previous generation, priced identically to their successors except
    # Sonnet 4.6, which was $3/$15 before the Sonnet 5 price.
    "claude-opus-4-8": ModelPrices(5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-7": ModelPrices(5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-4-6": ModelPrices(3.00, 15.00, 3.75, 0.30),
}


def estimate_cost_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float | None:
    """Dollar-equivalent of one call's tokens at list prices.

    ``None`` for a model with no published price here — deliberately not 0.0,
    so an unpriced model reads as "unknown" rather than as "free" and cannot
    quietly drag a total down. Every model in ``ANTHROPIC_DIRECT_MODELS`` is
    covered, and a test enforces that.

    The four token classes are priced separately because they differ by up to
    50x: on Opus 5 a cache read is $0.50/MTok against $25.00/MTok for output.
    Summing them as one number would misprice a cache-heavy workload badly,
    and this app is built around prompt caching.
    """
    prices = MODEL_PRICES.get(model)
    if prices is None:
        return None
    return (
        input_tokens * prices.input
        + output_tokens * prices.output
        + cache_creation_input_tokens * prices.cache_write_5m
        + cache_read_input_tokens * prices.cache_read
    ) / _PER_MILLION
