"""LLM provider abstraction.

All Anthropic-shaped LLM calls go through ``get_provider(model)`` rather
than instantiating ``anthropic.AsyncAnthropic`` directly. Backends: the
Anthropic API itself, OpenRouter (so a Claude slug can be routed there,
and non-Claude OpenRouter slugs selected per-agent), a local
OpenAI-compatible server, and the Claude Code CLI via the Claude Agent
SDK (so calls run on a Claude subscription instead of a metered API
key). The layer exists so any of these can serve a call without
rewriting every call site's Anthropic-shaped request.
"""
from openexecutive.providers.agent_sdk_provider import AgentSDKProvider
from openexecutive.providers.provider import LLMProvider
from openexecutive.providers.registry import (
    ANTHROPIC_DIRECT_MODELS,
    OPENROUTER_MODELS,
    allowed_models,
    allowed_models_for,
    get_provider,
)

__all__ = [
    "ANTHROPIC_DIRECT_MODELS",
    "AgentSDKProvider",
    "LLMProvider",
    "OPENROUTER_MODELS",
    "allowed_models",
    "allowed_models_for",
    "get_provider",
]
