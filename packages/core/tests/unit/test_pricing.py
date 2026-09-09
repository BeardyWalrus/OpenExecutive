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
    missing = [
        m for m in registry_mod.ANTHROPIC_DIRECT_MODELS
        if estimate_cost_usd(m, input_tokens=1) is None
    ]
    assert not missing, f"models with no published price: {sorted(missing)}"


def test_dated_pins_price_like_their_canonical_id() -> None:
    """The lookup runs against the id the API reported, and that is routinely a
    dated pin -- registry.py documents exactly this. Asserting the table only
    against canonical slugs (as the first version of this test did) checks a key
    space the runtime never uses, and every real call priced as unknown: $0
    totals, the very bug this module exists to fix."""
    for model in registry_mod.ANTHROPIC_DIRECT_MODELS:
        pinned = f"{model}-20260315"
        assert estimate_cost_usd(pinned, output_tokens=1_000_000) == pytest.approx(
            estimate_cost_usd(model, output_tokens=1_000_000)
        ), pinned


def test_normalisation_agrees_with_the_registry_regex() -> None:
    """pricing._CLAUDE_ID_RE is a copy (importing registry here would be an
    import cycle). Pin the copy to the original so they cannot drift."""
    from openexecutive.providers.pricing import _CLAUDE_ID_RE as ours

    assert ours.pattern == registry_mod._CLAUDE_ID_RE.pattern


@pytest.mark.parametrize(
    "counts",
    [
        {"input_tokens": None},
        {"output_tokens": None},
        {"cache_creation_input_tokens": None},
        {"cache_read_input_tokens": None},
        {"input_tokens": None, "output_tokens": None,
         "cache_creation_input_tokens": None, "cache_read_input_tokens": None},
    ],
)
def test_null_token_counts_do_not_raise(counts: dict[str, object]) -> None:
    """None is a real wire value: input_tokens and the cache_* counts are
    Optional in the Anthropic message_delta schema, and the assembler merges a
    delta's usage over message_start. Before this was coerced, `None * 6.25`
    raised a TypeError from inside the stream's teardown."""
    assert estimate_cost_usd("claude-opus-5", **counts) is not None  # type: ignore[arg-type]


def test_garbage_token_count_is_treated_as_zero() -> None:
    """A malformed count must not take the turn down with it."""
    assert estimate_cost_usd(
        "claude-opus-5", input_tokens="nonsense",  # type: ignore[arg-type]
    ) == pytest.approx(0.0)


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
