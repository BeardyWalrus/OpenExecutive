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
https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-09-09;
that page lists Opus 5 / 4.8 / 4.7 at one shared rate, which is why the older
two appear below). Cache writes are the 5-minute rate (1.25x base input);
cache reads are 0.1x base input on every model here. Re-check when adding a
model: the multipliers are not universal (Claude Fable 5.1 reads at 0.025x).
"""

from __future__ import annotations

import re
from typing import NamedTuple


class ModelPrices(NamedTuple):
    """USD per million tokens, by token class."""

    input: float
    output: float
    cache_write_5m: float
    cache_read: float


_PER_MILLION = 1_000_000.0

# Keyed on the canonical (dateless) model id. Lookups go through _normalise()
# below, so a dated pin resolves here too.
MODEL_PRICES: dict[str, ModelPrices] = {
    # Current trio.
    "claude-opus-5": ModelPrices(5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5": ModelPrices(2.00, 10.00, 2.50, 0.20),
    "claude-haiku-4-5": ModelPrices(1.00, 5.00, 1.25, 0.10),
    # Previous generation, priced identically to their successors except
    # Sonnet 4.6, which was $3/$15 before the Sonnet 5 price.
    "claude-opus-4-8": ModelPrices(5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-7": ModelPrices(5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-4-6": ModelPrices(3.00, 15.00, 3.75, 0.30),
}


# Mirrors providers.registry._CLAUDE_ID_RE. Duplicated rather than imported
# because registry imports agent_sdk_provider, which imports this module — the
# import would be a cycle. A test asserts the two agree, so they cannot drift.
_CLAUDE_ID_RE = re.compile(
    r"^claude-(?P<family>[a-z]+)-(?P<major>\d{1,3})(?:-(?P<minor>\d{1,3}))?(?:-\d{8})?$"
)


def _normalise(model: str) -> str:
    """Canonical dateless id for a Claude model id, or the input unchanged.

    Load-bearing, not cosmetic: the price lookup runs against the model id the
    API reported over the wire, and that is frequently a dated pin
    (``claude-opus-5-20260315``) rather than the canonical slug this app sends.
    An exact-match table would miss every one of those, price them as unknown,
    and hand back exactly the $0 total this module exists to eliminate — the
    same shape of bug as a stale hand-written id map, and just as silent.
    """
    m = _CLAUDE_ID_RE.match(model)
    if m is None:
        return model
    minor = m.group("minor")
    base = f"claude-{m.group('family')}-{m.group('major')}"
    return f"{base}-{minor}" if minor else base


def _as_int(value: object) -> int:
    """Token counts, coerced. ``None`` is a real wire value, not a bug.

    ``input_tokens`` / ``cache_*_input_tokens`` are Optional in the Anthropic
    ``message_delta`` schema, and the assembler merges a delta's usage over the
    ``message_start`` values — so a JSON ``null`` lands here as ``None`` and
    ``None * 6.25`` raises. That raise would surface inside the stream's
    teardown, replacing whatever the turn was actually doing, so this coerces
    at the boundary rather than trusting callers.
    """
    if isinstance(value, int):  # bool is an int subclass; harmless as a count
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0  # None, and anything else the wire might hand us


def estimate_cost_usd(
    model: str,
    *,
    input_tokens: object = 0,
    output_tokens: object = 0,
    cache_creation_input_tokens: object = 0,
    cache_read_input_tokens: object = 0,
) -> float | None:
    """Dollar-equivalent of one call's tokens at list prices.

    ``None`` for a model with no published price. Note this is only honest at
    the row level: ``AuditLogger.usage_summary`` coalesces a null cost to 0 when
    it aggregates, so an unpriced model still reads as a cheap one in the
    totals. Keeping every model priced is what actually prevents under-reporting
    — hence the test that every offered model resolves here.

    The four token classes are priced separately because they differ by up to
    50x: on Opus 5 a cache read is $0.50/MTok against $25.00/MTok for output.
    Summing them as one number would misprice a cache-heavy workload badly,
    and this app is built around prompt caching.
    """
    prices = MODEL_PRICES.get(_normalise(model))
    if prices is None:
        return None
    return (
        _as_int(input_tokens) * prices.input
        + _as_int(output_tokens) * prices.output
        + _as_int(cache_creation_input_tokens) * prices.cache_write_5m
        + _as_int(cache_read_input_tokens) * prices.cache_read
    ) / _PER_MILLION
