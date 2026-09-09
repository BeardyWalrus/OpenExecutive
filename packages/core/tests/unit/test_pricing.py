"""Token counts must turn into a dollar-equivalent on the subscription path.

The Agent SDK path is not billed per call, so nothing comes back over the
wire. Reporting 0 for it made `/audit/usage` claim a subscription install
spends nothing, which is the one number an operator budgets against.
"""
from __future__ import annotations

import pytest

from openexecutive.providers import registry as registry_mod
from openexecutive.providers.pricing import MODEL_PRICES, estimate_cost_usd


def test_every_offered_claude_model_has_a_price() -> None:
    """An unpriced model contributes 0 to the totals -- indistinguishable from
    a quiet day. This is the same rename trap that broke _CLAUDE_CLI_MODELS."""
    missing = set(registry_mod.ANTHROPIC_DIRECT_MODELS) - set(MODEL_PRICES)
    assert not missing, f"models with no published price: {sorted(missing)}"


def test_unknown_model_is_none_not_zero() -> None:
    """None reads as 'unknown'; 0.0 would read as 'free' and drag totals down."""
    assert estimate_cost_usd("not-a-model", input_tokens=1_000_000) is None


def test_each_token_class_is_priced_separately() -> None:
    """Opus 5: $5 in / $25 out / $6.25 cache write / $0.50 cache read per MTok."""
    assert estimate_cost_usd("claude-opus-5", input_tokens=1_000_000) == pytest.approx(5.00)
    assert estimate_cost_usd("claude-opus-5", output_tokens=1_000_000) == pytest.approx(25.00)
    assert estimate_cost_usd(
        "claude-opus-5", cache_creation_input_tokens=1_000_000
    ) == pytest.approx(6.25)
    assert estimate_cost_usd(
        "claude-opus-5", cache_read_input_tokens=1_000_000
    ) == pytest.approx(0.50)


def test_classes_sum() -> None:
    """A realistic cache-heavy turn: the classes differ by 50x, so a single
    blended rate would misprice this app badly."""
    got = estimate_cost_usd(
        "claude-opus-5",
        input_tokens=2_000,
        output_tokens=1_000,
        cache_creation_input_tokens=10_000,
        cache_read_input_tokens=100_000,
    )
    expected = (2_000 * 5.00 + 1_000 * 25.00 + 10_000 * 6.25 + 100_000 * 0.50) / 1_000_000
    assert got == pytest.approx(expected)


def test_no_tokens_is_zero_not_none() -> None:
    """A priced model with an empty turn really did cost nothing."""
    assert estimate_cost_usd("claude-sonnet-5") == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("model", "field", "per_mtok"),
    [
        ("claude-sonnet-5", "input_tokens", 2.00),
        ("claude-sonnet-5", "output_tokens", 10.00),
        ("claude-haiku-4-5", "input_tokens", 1.00),
        ("claude-haiku-4-5", "output_tokens", 5.00),
        ("claude-haiku-4-5-20251001", "output_tokens", 5.00),
    ],
)
def test_published_rates(model: str, field: str, per_mtok: float) -> None:
    """Guards the table against a typo; figures are the published list prices."""
    assert estimate_cost_usd(model, **{field: 1_000_000}) == pytest.approx(per_mtok)


def test_cache_multipliers_hold() -> None:
    """Cache write is 1.25x input and cache read 0.1x input on every model here.
    Not universal across Anthropic's range, so it is asserted rather than assumed."""
    for model, prices in MODEL_PRICES.items():
        assert prices.cache_write_5m == pytest.approx(prices.input * 1.25), model
        assert prices.cache_read == pytest.approx(prices.input * 0.1), model
