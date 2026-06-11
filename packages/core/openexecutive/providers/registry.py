"""Model registry + per-call provider routing.

``MODEL_SPECS`` is the single source of truth for: what we ship to the
Council UI, which OpenRouter slug each Claude model maps to, and which
Anthropic-only features each model tolerates. ``get_provider(model)``
picks the backend per call so the user can flip an agent's model in the
Council UI and have requests for that agent — and only that agent —
route differently.
"""
from __future__ import annotations

from fastapi import HTTPException

from openexecutive.config import get_settings
from openexecutive.providers.anthropic_provider import AnthropicProvider
from openexecutive.providers.feature_gate import FeatureSpec
from openexecutive.providers.openrouter_provider import OpenRouterProvider
from openexecutive.providers.provider import LLMProvider

# Anthropic-direct slugs — used as canonical model names everywhere in
# the codebase (config defaults, agent class defaults, override DB).
ANTHROPIC_DIRECT_MODELS: list[str] = [
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]

# Models reachable only through OpenRouter. These strings ARE the
# OpenRouter slug — we don't translate them on the way through. Curated
# PAID models only: the rate-limited ``:free`` tier (and the utility_fast-
# only free-model matrix) was removed — its 429s surfaced as user-visible
# errors and the per-agent free-model surface was more than it earned.
# BYO-model routing through OpenRouter is unchanged — add a slug here to
# surface it in the Council UI dropdown.
OPENROUTER_MODELS: list[str] = [
    "openai/gpt-5",
    "openai/gpt-5-mini",
    "openai/gpt-5-nano",
    "google/gemini-2.5-pro",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-lite",
    "meta-llama/llama-3.3-70b-instruct",
    "deepseek/deepseek-r1",
    "x-ai/grok-4",
]


# Per-Claude OpenRouter slug. The Anthropic-direct name is the registry
# key; the value is what we send when OPENROUTER_ENABLED is on.
_CLAUDE_OPENROUTER_SLUGS: dict[str, str] = {
    "claude-opus-4-7": "anthropic/claude-opus-4.7",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "claude-haiku-4-5-20251001": "anthropic/claude-haiku-4.5",
}


_CLAUDE_FEATURE_SPEC = FeatureSpec(
    supports_cache_control=True,
    supports_thinking=True,
    supports_web_search=True,
    supports_tool_use=True,
)


# Per-non-Claude model spec. ``supports_tool_use`` is universal across
# the curated set; the other three are off — Anthropic-specific server
# tools and prompt-caching annotations have no OpenAI-format equivalent.
_DEFAULT_NON_CLAUDE_SPEC = FeatureSpec(
    supports_cache_control=False,
    supports_thinking=False,
    supports_web_search=False,
    supports_tool_use=True,
)


def allowed_models() -> list[str]:
    """Flat allowlist the Council UI's dropdown reads.

    Always exposes the Anthropic-direct trio. The OpenRouter set is folded
    in when ``OPENROUTER_ENABLED`` is on so the dropdown can't offer a
    model the runtime won't actually serve.
    """
    settings = get_settings()
    if settings.openrouter_enabled:
        return [*ANTHROPIC_DIRECT_MODELS, *OPENROUTER_MODELS]
    return list(ANTHROPIC_DIRECT_MODELS)


def allowed_models_for(agent_id: str | None) -> list[str]:
    """Per-agent allowlist for the Council UI dropdown and PATCH validator.

    Every agent — specialists, the Executive, Quality Judge, and the
    ``utility_fast`` virtual agent — gets the same ``allowed_models()``
    list. (The ``utility_fast``-only free/cheap OpenRouter matrix was
    removed; ``agent_id`` is retained for call-site stability and any
    future per-agent rules.)
    """
    return allowed_models()


def _is_claude(model: str) -> bool:
    return model in _CLAUDE_OPENROUTER_SLUGS


# Module-level singletons — providers pool their own HTTP connections and
# are async-safe. Recreating them per call burns ~10 ms each.
_anthropic_provider: AnthropicProvider | None = None
_openrouter_provider: OpenRouterProvider | None = None


def _anthropic() -> AnthropicProvider:
    global _anthropic_provider
    if _anthropic_provider is None:
        settings = get_settings()
        _anthropic_provider = AnthropicProvider(api_key=settings.anthropic_api_key)
    return _anthropic_provider


def _openrouter() -> OpenRouterProvider:
    global _openrouter_provider
    if _openrouter_provider is None:
        settings = get_settings()
        if not settings.openrouter_api_key:
            # The Settings model_validator already prevents OPENROUTER_ENABLED
            # without a key, but defense in depth — a misconfigured env could
            # otherwise produce a None-token request.
            raise HTTPException(
                status_code=400,
                detail="OpenRouter routing requires OPENROUTER_API_KEY",
            )
        # Pre-build the slug + spec lookup so the provider can resolve a
        # Claude model to its OpenRouter slug + feature spec on each call.
        slug_lookup = dict(_CLAUDE_OPENROUTER_SLUGS)
        spec_lookup: dict[str, FeatureSpec] = {
            m: _CLAUDE_FEATURE_SPEC for m in ANTHROPIC_DIRECT_MODELS
        }
        for m in OPENROUTER_MODELS:
            spec_lookup[m] = _DEFAULT_NON_CLAUDE_SPEC
        _openrouter_provider = OpenRouterProvider(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            app_title=settings.openrouter_app_title,
            referer=settings.openrouter_referer,
            timeout_s=settings.openrouter_timeout_s,
            slug_lookup=slug_lookup,
            spec_lookup=spec_lookup,
        )
    return _openrouter_provider


def get_provider(model: str) -> LLMProvider:
    """Return the provider that should serve calls for ``model``.

    Routing rules:

    * Claude family — Anthropic direct by default; OpenRouter when
      ``OPENROUTER_ENABLED`` is on.
    * Non-Claude (anything in ``OPENROUTER_MODELS``, or any unknown slug)
      — OpenRouter only. Raises HTTP 400 when ``OPENROUTER_ENABLED`` is
      off, since we have no other backend that speaks those models.
    """
    settings = get_settings()
    if _is_claude(model):
        if settings.openrouter_enabled:
            return _openrouter()
        return _anthropic()
    # Non-Claude requires OpenRouter to be enabled.
    if not settings.openrouter_enabled:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model {model!r} requires OPENROUTER_ENABLED=true; "
                f"set OPENROUTER_API_KEY and toggle the flag to route this model."
            ),
        )
    return _openrouter()


def _reset_for_tests() -> None:
    """Drop cached provider singletons. Test-only — pytest fixtures call this."""
    global _anthropic_provider, _openrouter_provider
    _anthropic_provider = None
    _openrouter_provider = None
