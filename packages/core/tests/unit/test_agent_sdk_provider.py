"""Claude Agent SDK backend — request translation, response assembly, routing.

The backend drives the Claude Code CLI as a subprocess, so these tests
stand in a fake ``claude_agent_sdk`` module rather than launching a real
CLI (which would need an interactive `claude auth login` and would bill a real
subscription). What is verified here is everything the adapter itself owns:

* the Anthropic-shape request is translated into ``ClaudeAgentOptions``
  with the constraints that keep the CLI from running its own agent loop,
* raw Anthropic stream events are reassembled into the ``Message`` shape
  every call site in this repo already expects,
* the MCP tool-name prefix the CLI adds is stripped back off,
* registry routing honours the opt-in flag and its precedence.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

import pytest  # noqa: E402

from openexecutive.providers import agent_sdk_provider as mod  # noqa: E402
from openexecutive.providers import registry as registry_mod  # noqa: E402
from openexecutive.providers.agent_sdk_provider import (  # noqa: E402
    AgentSDKProvider,
    MessageAssembler,
    build_prompt,
    flatten_system,
    strip_tool_prefix,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> Any:
    registry_mod._reset_for_tests()
    yield
    registry_mod._reset_for_tests()


# ---------------------------------------------------------------------------
# Fake SDK
# ---------------------------------------------------------------------------


class _FakeStreamEvent:
    def __init__(self, event: dict[str, Any]) -> None:
        self.event = event


class _FakeResultMessage:
    def __init__(self, *, is_error: bool = False, subtype: str = "success",
                 result: str | None = None) -> None:
        self.is_error = is_error
        self.subtype = subtype
        self.result = result


class _FakePermissionResultDeny:
    def __init__(self, *, message: str = "", interrupt: bool = False) -> None:
        self.message = message
        self.interrupt = interrupt


def _fake_sdk(events: list[Any]) -> Any:
    """Build a stand-in ``claude_agent_sdk`` that replays ``events``."""
    captured: dict[str, Any] = {}

    def _tool(name: str, description: str, schema: dict[str, Any]) -> Any:
        def _wrap(fn: Any) -> Any:
            return SimpleNamespace(name=name, description=description, schema=schema, fn=fn)
        return _wrap

    def _create_server(*, name: str, tools: list[Any]) -> Any:
        return SimpleNamespace(name=name, tools=tools)

    def _options(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    async def _query(*, prompt: str, options: Any) -> Any:
        captured["prompt"] = prompt
        for ev in events:
            yield ev

    return SimpleNamespace(
        tool=_tool,
        create_sdk_mcp_server=_create_server,
        ClaudeAgentOptions=_options,
        StreamEvent=_FakeStreamEvent,
        ResultMessage=_FakeResultMessage,
        PermissionResultDeny=_FakePermissionResultDeny,
        query=_query,
        captured=captured,
    )


def _text_stream(text: str, *, model: str = "claude-sonnet-4-6") -> list[Any]:
    return [
        _FakeStreamEvent({
            "type": "message_start",
            "message": {"id": "msg_1", "model": model,
                        "usage": {"input_tokens": 11, "cache_read_input_tokens": 7}},
        }),
        _FakeStreamEvent({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        _FakeStreamEvent({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }),
        _FakeStreamEvent({"type": "content_block_stop", "index": 0}),
        _FakeStreamEvent({
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        }),
        _FakeStreamEvent({"type": "message_stop"}),
    ]


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


def test_flatten_system_joins_blocks_and_drops_cache_control() -> None:
    system = [
        {"type": "text", "text": "PERSONA", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "COMPANY"},
    ]
    # Order is preserved — the CLI does its own prefix caching, so a
    # reordering here would cost cache hits on every turn.
    assert flatten_system(system) == "PERSONA\n\nCOMPANY"


def test_flatten_system_accepts_plain_string_and_none() -> None:
    assert flatten_system("hello") == "hello"
    assert flatten_system(None) == ""


def test_build_prompt_passes_single_user_turn_through_verbatim() -> None:
    """A lone user turn must not gain a wrapper — specialist calls are the
    hot path and any added preamble perturbs the CLI's prefix cache."""
    assert build_prompt([{"role": "user", "content": "What is our runway?"}]) == (
        "What is our runway?"
    )


def test_build_prompt_replays_history_as_delimited_transcript() -> None:
    prompt = build_prompt([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ])
    assert "<conversation_history>" in prompt
    assert "User: first" in prompt
    assert "Assistant: reply" in prompt
    # The live turn sits outside the transcript block.
    assert prompt.endswith("second")
    assert "User: second" not in prompt


def test_build_prompt_renders_tool_traffic_in_history() -> None:
    """Tool calls and results must survive the flattening — dropping them
    would leave the assistant's prior reasoning looking unmotivated."""
    prompt = build_prompt([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "consult_specialist",
             "input": {"specialist": "cfo"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "18 months"},
        ]},
        {"role": "user", "content": "and?"},
    ])
    assert "consult_specialist" in prompt
    assert "cfo" in prompt
    assert "18 months" in prompt


def test_build_prompt_empty_messages_is_empty_string() -> None:
    assert build_prompt([]) == ""


def test_options_constrain_the_cli_agent_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI must produce one assistant turn and execute nothing."""
    sdk = _fake_sdk(_text_stream("ok"))
    monkeypatch.setattr(mod, "_require_sdk", lambda: sdk)
    provider = AgentSDKProvider(model_map={"claude-sonnet-4-6": "sonnet"})

    asyncio.run(provider.messages_create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=[{"type": "text", "text": "S"}],
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "consult_specialist", "description": "d",
                "input_schema": {"type": "object", "properties": {}}}],
    ))

    opts = sdk.captured
    assert opts["max_turns"] == 1
    # Built-in Claude Code tools (Bash/Read/Edit) must never be offered.
    assert opts["tools"] == []
    assert opts["strict_mcp_config"] is True
    assert opts["setting_sources"] is None
    assert opts["include_partial_messages"] is True
    assert opts["model"] == "sonnet"
    assert opts["system_prompt"] == "S"
    # The caller's tool is advertised via the in-process MCP server...
    assert list(opts["mcp_servers"]) == ["openexec"]
    # ...but NEVER via allowed_tools: an allow entry auto-approves the tool
    # before can_use_tool runs, which would let the CLI execute the stub
    # handler instead of handing the tool_use block back to the caller.
    assert "allowed_tools" not in opts


def test_options_scrub_api_key_env_to_force_subscription_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of this backend is subscription auth. If the shell
    exports a key for the other backends, the CLI must not pick it up and
    silently bill the API instead."""
    sdk = _fake_sdk(_text_stream("ok"))
    monkeypatch.setattr(mod, "_require_sdk", lambda: sdk)
    provider = AgentSDKProvider(model_map={})

    asyncio.run(provider.messages_create(
        model="claude-sonnet-4-6", messages=[{"role": "user", "content": "hi"}]
    ))
    assert sdk.captured["env"]["ANTHROPIC_API_KEY"] == ""
    assert sdk.captured["env"]["ANTHROPIC_AUTH_TOKEN"] == ""


def test_thinking_and_effort_are_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = _fake_sdk(_text_stream("ok"))
    monkeypatch.setattr(mod, "_require_sdk", lambda: sdk)
    provider = AgentSDKProvider(model_map={})

    asyncio.run(provider.messages_create(
        model="claude-opus-4-7",
        messages=[{"role": "user", "content": "hi"}],
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
    ))
    assert sdk.captured["thinking"] == {"type": "adaptive"}
    assert sdk.captured["effort"] == "low"


def test_server_side_tool_without_input_schema_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic's server-side web_search tool has no input_schema and cannot
    be re-hosted as an MCP tool; it must be dropped, not crash the call."""
    sdk = _fake_sdk(_text_stream("ok"))
    monkeypatch.setattr(mod, "_require_sdk", lambda: sdk)
    provider = AgentSDKProvider(model_map={})

    asyncio.run(provider.messages_create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
    ))
    assert sdk.captured["mcp_servers"] == {}


# ---------------------------------------------------------------------------
# Response assembly
# ---------------------------------------------------------------------------


def test_strip_tool_prefix() -> None:
    assert strip_tool_prefix("mcp__openexec__consult_specialist") == "consult_specialist"
    assert strip_tool_prefix("consult_specialist") == "consult_specialist"


def test_assembler_builds_text_message_with_usage() -> None:
    asm = MessageAssembler()
    for ev in _text_stream("hello world"):
        asm.feed(ev.event)
    msg = asm.finalize()

    assert msg.role == "assistant"
    assert msg.stop_reason == "end_turn"
    assert [b.type for b in msg.content] == ["text"]
    assert msg.content[0].text == "hello world"
    assert msg.usage.input_tokens == 11
    assert msg.usage.output_tokens == 5
    assert msg.usage.cache_read_input_tokens == 7
    # Subscription calls are not metered per request.
    assert msg.usage.cost is None


def test_assembler_reassembles_tool_use_and_strips_prefix() -> None:
    """Tool input arrives as fragmented JSON deltas; the caller needs one
    parsed dict under the un-prefixed tool name."""
    asm = MessageAssembler()
    for ev in [
        {"type": "message_start", "message": {"id": "m", "model": "x", "usage": {}}},
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "tu_1",
            "name": "mcp__openexec__consult_specialist", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"special'}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": 'ist": "cfo"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}},
        {"type": "message_stop"},
    ]:
        asm.feed(ev)
    msg = asm.finalize()

    assert msg.stop_reason == "tool_use"
    assert msg.content[0].type == "tool_use"
    assert msg.content[0].name == "consult_specialist"
    assert msg.content[0].input == {"specialist": "cfo"}
    assert msg.content[0].id == "tu_1"


def test_assembler_preserves_parallel_tool_calls_in_order() -> None:
    """Cross-domain turns fan out to several specialists in one assistant
    message — every tool_use block must survive."""
    asm = MessageAssembler()
    asm.feed({"type": "message_start", "message": {"id": "m", "model": "x", "usage": {}}})
    for idx, spec in enumerate(["cfo", "cmo", "coo"]):
        asm.feed({"type": "content_block_start", "index": idx, "content_block": {
            "type": "tool_use", "id": f"tu_{idx}",
            "name": "mcp__openexec__consult_specialist", "input": {}}})
        asm.feed({"type": "content_block_delta", "index": idx, "delta": {
            "type": "input_json_delta", "partial_json": f'{{"specialist": "{spec}"}}'}})
        asm.feed({"type": "content_block_stop", "index": idx})
    asm.feed({"type": "message_stop"})

    msg = asm.finalize()
    assert [b.input["specialist"] for b in msg.content] == ["cfo", "cmo", "coo"]


def test_assembler_accumulates_thinking_deltas() -> None:
    """Deep-reasoning turns stream reasoning as thinking_delta, not
    text_delta — without that branch the block finalizes empty."""
    asm = MessageAssembler()
    asm.feed({"type": "message_start", "message": {"id": "m", "model": "x", "usage": {}}})
    asm.feed({"type": "content_block_start", "index": 0,
              "content_block": {"type": "thinking", "thinking": ""}})
    asm.feed({"type": "content_block_delta", "index": 0,
              "delta": {"type": "thinking_delta", "thinking": "weigh "}})
    asm.feed({"type": "content_block_delta", "index": 0,
              "delta": {"type": "thinking_delta", "thinking": "options"}})
    asm.feed({"type": "content_block_delta", "index": 0,
              "delta": {"type": "signature_delta", "signature": "sig123"}})
    asm.feed({"type": "content_block_stop", "index": 0})
    asm.feed({"type": "content_block_start", "index": 1,
              "content_block": {"type": "text", "text": ""}})
    asm.feed({"type": "content_block_delta", "index": 1,
              "delta": {"type": "text_delta", "text": "answer"}})
    asm.feed({"type": "message_stop"})

    msg = asm.finalize()
    assert [b.type for b in msg.content] == ["thinking", "text"]
    assert msg.content[0].thinking == "weigh options"
    assert msg.content[0].signature == "sig123"
    assert msg.content[1].text == "answer"


def test_assembler_survives_unparseable_tool_json() -> None:
    asm = MessageAssembler()
    asm.feed({"type": "message_start", "message": {"id": "m", "model": "x", "usage": {}}})
    asm.feed({"type": "content_block_start", "index": 0, "content_block": {
        "type": "tool_use", "id": "t", "name": "x", "input": {}}})
    asm.feed({"type": "content_block_delta", "index": 0,
              "delta": {"type": "input_json_delta", "partial_json": "{truncated"}})
    asm.feed({"type": "message_stop"})
    assert asm.finalize().content[0].input == {}


def test_messages_create_returns_anthropic_shaped_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _fake_sdk(_text_stream("done"))
    monkeypatch.setattr(mod, "_require_sdk", lambda: sdk)
    provider = AgentSDKProvider(model_map={})

    msg = asyncio.run(provider.messages_create(
        model="claude-sonnet-4-6", messages=[{"role": "user", "content": "hi"}]
    ))
    # This is exactly what agents/base.py does with the result.
    text_blocks = [b for b in msg.content if b.type == "text"]
    assert text_blocks[0].text == "done"


def test_messages_create_raises_on_sdk_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI-side failure (rate limit, auth) must surface, not return empty."""
    sdk = _fake_sdk([
        _FakeResultMessage(is_error=True, subtype="error_max_budget_usd",
                           result="limit reached"),
    ])
    monkeypatch.setattr(mod, "_require_sdk", lambda: sdk)
    provider = AgentSDKProvider(model_map={})

    with pytest.raises(RuntimeError, match="limit reached"):
        asyncio.run(provider.messages_create(
            model="claude-sonnet-4-6", messages=[{"role": "user", "content": "hi"}]
        ))


def test_messages_stream_yields_text_deltas_then_final_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors the executive.py consumption pattern exactly."""
    sdk = _fake_sdk(_text_stream("streamed"))
    monkeypatch.setattr(mod, "_require_sdk", lambda: sdk)
    provider = AgentSDKProvider(model_map={})

    async def _drive() -> tuple[str, Any]:
        out = ""
        async with provider.messages_stream(
            model="claude-sonnet-4-6", messages=[{"role": "user", "content": "hi"}]
        ) as stream:
            async for event in stream:
                if (
                    getattr(event, "type", None) == "content_block_delta"
                    and getattr(event.delta, "type", None) == "text_delta"
                ):
                    out += event.delta.text
            return out, await stream.get_final_message()

    text, final = asyncio.run(_drive())
    assert text == "streamed"
    assert final.content[0].text == "streamed"
    assert final.stop_reason == "end_turn"


def test_stream_events_expose_unprefixed_tool_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer watching the event stream (not just the final message)
    must not see the CLI's mcp__ namespace leak through."""
    sdk = _fake_sdk([
        _FakeStreamEvent({"type": "message_start",
                          "message": {"id": "m", "model": "x", "usage": {}}}),
        _FakeStreamEvent({"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "t",
            "name": "mcp__openexec__consult_specialist", "input": {}}}),
        _FakeStreamEvent({"type": "message_stop"}),
    ])
    monkeypatch.setattr(mod, "_require_sdk", lambda: sdk)
    provider = AgentSDKProvider(model_map={})

    async def _drive() -> list[str]:
        names = []
        async with provider.messages_stream(
            model="claude-sonnet-4-6", messages=[{"role": "user", "content": "hi"}]
        ) as stream:
            async for event in stream:
                if getattr(event, "type", None) == "content_block_start":
                    names.append(event.content_block.name)
        return names

    assert asyncio.run(_drive()) == ["consult_specialist"]


def test_deny_hook_blocks_execution_without_interrupting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator owns tool dispatch; the CLI must never execute.

    interrupt must stay False — interrupting aborts the CLI session hard
    enough that it reports a failed result instead of completing the turn.
    """
    sdk = _fake_sdk([])
    monkeypatch.setattr(mod, "_require_sdk", lambda: sdk)

    result = asyncio.run(
        AgentSDKProvider._deny_tool("mcp__openexec__consult_specialist", {}, None)
    )
    assert isinstance(result, _FakePermissionResultDeny)
    assert result.interrupt is False


# ---------------------------------------------------------------------------
# Registry routing
# ---------------------------------------------------------------------------


def _settings(**over: Any) -> Any:
    base = {
        "anthropic_api_key": "sk-test",
        "openrouter_enabled": False,
        "openrouter_api_key": None,
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "openrouter_app_title": "Open Executive",
        "openrouter_referer": None,
        "openrouter_timeout_s": 180.0,
        "local_models_enabled": False,
        "local_models": [],
        "agent_sdk_enabled": False,
        "agent_sdk_cli_path": None,
        "agent_sdk_timeout_s": 300.0,
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_agent_sdk_flag_routes_claude_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry_mod, "get_settings", lambda: _settings(agent_sdk_enabled=True)
    )
    registry_mod._reset_for_tests()
    assert isinstance(registry_mod.get_provider("claude-sonnet-4-6"), AgentSDKProvider)


def test_agent_sdk_flag_off_keeps_anthropic_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default behavior must be byte-identical to before this backend existed."""
    monkeypatch.setattr(registry_mod, "get_settings", lambda: _settings())
    registry_mod._reset_for_tests()
    assert not isinstance(
        registry_mod.get_provider("claude-sonnet-4-6"), AgentSDKProvider
    )


def test_openrouter_takes_precedence_over_agent_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabling the Agent SDK must not silently redirect an existing
    OpenRouter deployment."""
    monkeypatch.setattr(
        registry_mod,
        "get_settings",
        lambda: _settings(
            agent_sdk_enabled=True, openrouter_enabled=True,
            openrouter_api_key="sk-or-v1-x",
        ),
    )
    registry_mod._reset_for_tests()
    assert not isinstance(
        registry_mod.get_provider("claude-sonnet-4-6"), AgentSDKProvider
    )


def test_agent_sdk_provider_is_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry_mod, "get_settings", lambda: _settings(agent_sdk_enabled=True)
    )
    registry_mod._reset_for_tests()
    assert registry_mod.get_provider("claude-opus-4-7") is registry_mod.get_provider(
        "claude-sonnet-4-6"
    )


def test_claude_models_offered_when_only_agent_sdk_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no API key at all, the Council dropdown must still offer Claude —
    it is reachable through the logged-in CLI."""
    monkeypatch.setattr(
        registry_mod,
        "get_settings",
        lambda: _settings(anthropic_api_key=None, agent_sdk_enabled=True),
    )
    registry_mod._reset_for_tests()
    assert "claude-sonnet-4-6" in registry_mod.allowed_models()


def test_cli_model_map_covers_every_anthropic_direct_model() -> None:
    """A slug missing from the map would be sent to the CLI verbatim and 400."""
    assert set(registry_mod._CLAUDE_CLI_MODELS) == set(
        registry_mod.ANTHROPIC_DIRECT_MODELS
    )
