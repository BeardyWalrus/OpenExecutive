"""LLMProvider backed by the Claude Agent SDK (Claude Code CLI subprocess).

Why this exists: the Anthropic-direct and OpenRouter backends both bill
per token against an API key. The Agent SDK instead drives the Claude Code
CLI, which authenticates with the credentials written by ``claude login``.
For a Claude Pro / Max subscriber that means model calls are served under
the subscription's usage allowance rather than a metered API key.

What it is NOT: a second wire protocol. The CLI runs its own agent loop,
so this adapter constrains that loop down to "produce exactly one
assistant turn and hand it back" — the same unit of work
``messages.create`` returns. Concretely:

* ``max_turns=1`` and a ``can_use_tool`` hook that denies every tool call
  keep the CLI from acting on the model's output. The orchestrator, not
  the CLI, owns the tool-dispatch loop (``consult_specialist`` fans out to
  specialists in Python), so the CLI must hand the ``tool_use`` block back
  untouched instead of trying to execute it.
* ``tools=[]`` strips the built-in Claude Code toolset (Bash, Read, Edit,
  …). Those are developer-workstation tools and have no business being
  offered to the Executive.
* ``setting_sources=None`` + ``strict_mcp_config=True`` stop the CLI from
  picking up the ambient developer environment (``.mcp.json``, project
  settings, plugins). Without these, whatever a contributor happens to
  have configured locally would leak into production prompts.

Fidelity caveats — these are inherent to the CLI surface, not shortcuts:

* ``max_tokens`` is not expressible; the CLI owns the output budget.
* Per-block ``cache_control`` is not expressible. The CLI applies its own
  prompt caching, so the carefully ordered cached system blocks
  (``prompts/cache_manager.py``) are flattened into one system string.
  Under a subscription this costs rate-limit headroom rather than dollars.
* There is no supported way to inject synthetic ``assistant`` /
  ``tool_result`` turns into a fresh CLI session, so prior conversation
  history is replayed as a single delimited transcript in the prompt. The
  live turn is the trailing user message.

Streaming: ``include_partial_messages=True`` makes the CLI forward the raw
Anthropic stream events from its own upstream call, so the event stream
consumers already expect (``content_block_delta`` → ``delta.text_delta``,
then ``get_final_message()``) is reconstructed rather than simulated.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import AbstractAsyncContextManager, aclosing
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

# In-process MCP server name. The CLI namespaces MCP tools as
# ``mcp__<server>__<tool>``, so this prefix is stripped back off before the
# assembled Message reaches a caller that only knows ``consult_specialist``.
_MCP_SERVER = "openexec"
_TOOL_PREFIX = f"mcp__{_MCP_SERVER}__"


class AgentSDKUnavailableError(RuntimeError):
    """Raised when ``claude-agent-sdk`` is not installed."""


def _require_sdk() -> Any:
    """Import ``claude_agent_sdk`` lazily.

    Kept out of module import so the package still imports (and the rest of
    the test suite still runs) on a checkout that never enables this
    backend — it is an optional dependency carrying a bundled CLI binary.
    """
    try:
        import claude_agent_sdk
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise AgentSDKUnavailableError(
            "AGENT_SDK_ENABLED=true requires the claude-agent-sdk package. "
            "Install it from packages/core with: uv sync --extra agent-sdk"
        ) from exc
    return claude_agent_sdk


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


def flatten_system(system: Any) -> str:
    """Anthropic ``system`` (str or list of blocks) → one plain string.

    ``cache_control`` annotations are dropped: the CLI manages its own
    prompt cache and has no per-block cache API. Block *order* is
    preserved, which is what actually matters for the CLI's own prefix
    caching to hit across turns.
    """
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    parts: list[str] = []
    for block in system:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n\n".join(p for p in parts if p)


def _stringify_content(content: Any) -> str:
    """Anthropic message ``content`` (str or block list) → plain text.

    Tool traffic is rendered rather than dropped: a replayed transcript
    that silently lost its ``tool_use`` / ``tool_result`` pairs would make
    the model's own prior reasoning look unmotivated.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text", "")))
        elif btype == "tool_use":
            args = json.dumps(block.get("input", {}), ensure_ascii=False)
            parts.append(f"[called {block.get('name', '')}({args})]")
        elif btype == "tool_result":
            parts.append(f"[result] {_stringify_content(block.get('content', ''))}")
        elif btype == "thinking":
            # Prior-turn thinking is not replayable as input; skip it rather
            # than present it back to the model as if it were said aloud.
            continue
    return "\n".join(p for p in parts if p)


def build_prompt(messages: list[dict[str, Any]]) -> str:
    """Anthropic ``messages`` → a single CLI prompt string.

    The CLI starts a fresh session per call and exposes no way to seed it
    with synthetic assistant turns, so anything before the final user
    message is replayed as a delimited transcript. When the only message is
    a user turn (the common case for specialist calls) the prompt is that
    text verbatim — no wrapper, so nothing perturbs the CLI's prefix cache.
    """
    if not messages:
        return ""
    if len(messages) == 1 and messages[0].get("role") == "user":
        return _stringify_content(messages[0].get("content", ""))

    lines: list[str] = []
    for msg in messages[:-1]:
        role = str(msg.get("role", "user")).capitalize()
        text = _stringify_content(msg.get("content", ""))
        if text:
            lines.append(f"{role}: {text}")
    last = messages[-1]
    tail = _stringify_content(last.get("content", ""))
    if not lines:
        return tail
    transcript = "\n\n".join(lines)
    return (
        "<conversation_history>\n"
        f"{transcript}\n"
        "</conversation_history>\n\n"
        f"{tail}"
    )


def _tool_servers(sdk: Any, tools: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Anthropic tool defs → an in-process MCP server the CLI can advertise.

    The handlers are never reached — ``can_use_tool`` denies first — but a
    tool must have one to be registered. Returning an error keeps a future
    change that relaxes the deny hook from silently succeeding with a stub.
    """
    if not tools:
        return {}, []

    sdk_tools = []
    names: list[str] = []
    for spec in tools:
        name = spec.get("name")
        if not name:
            continue
        # Anthropic server-side tools (web_search) carry no input_schema and
        # cannot be re-hosted as MCP tools. The CLI has its own WebSearch
        # tool, but wiring it here would change which tool the model calls
        # mid-turn; drop it and let the caller see it was unavailable.
        schema = spec.get("input_schema")
        if not isinstance(schema, dict):
            logger.warning(
                "agent_sdk: dropping tool %r — no input_schema (server-side "
                "tools are not supported on this backend)",
                name,
            )
            continue
        names.append(str(name))

        async def _handler(args: dict[str, Any], _name: str = str(name)) -> dict[str, Any]:
            return {
                "content": [
                    {"type": "text", "text": f"{_name} is dispatched by the caller."}
                ],
                "is_error": True,
            }

        sdk_tools.append(
            sdk.tool(str(name), str(spec.get("description", "")), schema)(_handler)
        )

    if not sdk_tools:
        return {}, []
    return {_MCP_SERVER: sdk.create_sdk_mcp_server(name=_MCP_SERVER, tools=sdk_tools)}, names


# ---------------------------------------------------------------------------
# Response assembly
# ---------------------------------------------------------------------------


def _ns(value: Any) -> Any:
    """Recursively convert dicts to SimpleNamespace for attribute access.

    Call sites read ``event.delta.text`` / ``message.usage.input_tokens``,
    matching the Anthropic SDK's pydantic models. SimpleNamespace gives the
    same access shape without importing those models — the same approach
    ``providers/translator.py`` takes for the OpenRouter backend.
    """
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_ns(v) for v in value]
    return value


def strip_tool_prefix(name: str) -> str:
    """``mcp__openexec__consult_specialist`` → ``consult_specialist``."""
    return name[len(_TOOL_PREFIX):] if name.startswith(_TOOL_PREFIX) else name


class MessageAssembler:
    """Rebuilds an Anthropic-shape ``Message`` from raw stream events.

    The CLI forwards the untouched Anthropic stream events from its own
    upstream call, so this is ordinary stream accumulation: block starts
    open a slot, deltas append to it, ``message_delta`` carries the final
    stop_reason and output token count.
    """

    def __init__(self) -> None:
        self._id = ""
        self._model = ""
        self._blocks: dict[int, dict[str, Any]] = {}
        self._text_parts: dict[int, list[str]] = {}
        self._json_parts: dict[int, list[str]] = {}
        self._stop_reason: str | None = None
        self._usage: dict[str, Any] = {}
        self.complete = False

    def feed(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "message_start":
            msg = event.get("message") or {}
            self._id = msg.get("id", "")
            self._model = msg.get("model", "")
            self._usage.update(msg.get("usage") or {})
        elif etype == "content_block_start":
            idx = int(event.get("index", 0))
            block = dict(event.get("content_block") or {})
            self._blocks[idx] = block
            if block.get("type") == "text":
                self._text_parts[idx] = [str(block.get("text", ""))]
            elif block.get("type") == "tool_use":
                self._json_parts[idx] = []
        elif etype == "content_block_delta":
            idx = int(event.get("index", 0))
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                self._text_parts.setdefault(idx, []).append(str(delta.get("text", "")))
            elif dtype == "thinking_delta":
                # Deep-reasoning turns (thinking={"type":"adaptive"}) stream
                # their reasoning through a separate delta type; without this
                # branch every thinking block would finalize empty.
                self._text_parts.setdefault(idx, []).append(str(delta.get("thinking", "")))
            elif dtype == "signature_delta":
                block = self._blocks.setdefault(idx, {"type": "thinking"})
                block["signature"] = str(block.get("signature", "")) + str(
                    delta.get("signature", "")
                )
            elif dtype == "input_json_delta":
                self._json_parts.setdefault(idx, []).append(
                    str(delta.get("partial_json", ""))
                )
        elif etype == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                self._stop_reason = str(delta["stop_reason"])
            self._usage.update(event.get("usage") or {})
        elif etype == "message_stop":
            self.complete = True

    def finalize(self) -> SimpleNamespace:
        """Build the final Message. Safe to call more than once."""
        content: list[SimpleNamespace] = []
        for idx in sorted(self._blocks):
            block = self._blocks[idx]
            btype = block.get("type")
            if btype == "text":
                content.append(
                    SimpleNamespace(type="text", text="".join(self._text_parts.get(idx, [])))
                )
            elif btype == "tool_use":
                raw = "".join(self._json_parts.get(idx, []))
                try:
                    parsed = json.loads(raw) if raw.strip() else dict(block.get("input") or {})
                except json.JSONDecodeError:
                    logger.warning("agent_sdk: unparseable tool input for block %s", idx)
                    parsed = {}
                content.append(
                    SimpleNamespace(
                        type="tool_use",
                        id=block.get("id", ""),
                        name=strip_tool_prefix(str(block.get("name", ""))),
                        input=parsed,
                    )
                )
            elif btype == "thinking":
                content.append(
                    SimpleNamespace(
                        type="thinking",
                        thinking="".join(self._text_parts.get(idx, [])),
                        signature=block.get("signature", ""),
                    )
                )
        return SimpleNamespace(
            id=self._id,
            type="message",
            role="assistant",
            model=self._model,
            content=content,
            stop_reason=self._stop_reason,
            stop_sequence=None,
            usage=SimpleNamespace(
                input_tokens=self._usage.get("input_tokens", 0),
                output_tokens=self._usage.get("output_tokens", 0),
                cache_creation_input_tokens=self._usage.get(
                    "cache_creation_input_tokens", 0
                ),
                cache_read_input_tokens=self._usage.get("cache_read_input_tokens", 0),
                # Subscription usage is not billed per call, so there is no
                # per-request USD figure to report. None (not 0.0) so cost
                # dashboards can tell "not metered" from "free".
                cost=None,
            ),
        )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AgentSDKProvider:
    """LLMProvider implementation backed by the Claude Code CLI.

    ``model_map`` translates this repo's canonical Claude slugs into names
    the CLI accepts (aliases such as ``opus`` / ``sonnet`` / ``haiku``),
    mirroring how ``registry._CLAUDE_OPENROUTER_SLUGS`` maps the same slugs
    onto OpenRouter's.
    """

    def __init__(
        self,
        *,
        model_map: dict[str, str],
        cli_path: str | None = None,
        default_timeout_s: float = 300.0,
    ) -> None:
        self._model_map = dict(model_map)
        self._cli_path = cli_path
        self._default_timeout_s = default_timeout_s

    def _resolve_model(self, model: str) -> str:
        return self._model_map.get(model, model)

    def _build_options(self, sdk: Any, kwargs: dict[str, Any]) -> Any:
        """Anthropic ``messages.create`` kwargs → ``ClaudeAgentOptions``."""
        mcp_servers, _tool_names = _tool_servers(sdk, kwargs.get("tools") or [])

        options: dict[str, Any] = {
            "model": self._resolve_model(str(kwargs.get("model", ""))),
            "system_prompt": flatten_system(kwargs.get("system")),
            "max_turns": 1,
            # Built-in Claude Code tools are workstation tools; the Executive
            # must only ever see the tools its caller declared.
            "tools": [],
            "mcp_servers": mcp_servers,
            # Deliberately NO allowed_tools. An entry that allows a whole
            # tool auto-approves it *before* can_use_tool is consulted, so
            # listing our tools here would shadow the deny hook and let the
            # CLI actually execute the stub handlers (verified: the handler
            # runs). Leaving it empty makes every call fall through to the
            # hook, which is what keeps dispatch with the caller.
            "strict_mcp_config": True,
            # Do not inherit the developer's local Claude Code configuration.
            "setting_sources": None,
            "include_partial_messages": True,
            "can_use_tool": self._deny_tool,
            # An empty value reads as unset to the CLI, forcing it onto the
            # subscription credentials from `claude login` even when the
            # shell (or .env) exports a key for the other backends.
            "env": {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""},
        }
        if self._cli_path:
            options["cli_path"] = self._cli_path

        thinking = kwargs.get("thinking")
        if thinking:
            options["thinking"] = thinking
        effort = (kwargs.get("output_config") or {}).get("effort")
        if effort:
            options["effort"] = effort

        return sdk.ClaudeAgentOptions(**options)

    @staticmethod
    async def _deny_tool(tool_name: str, _input: dict[str, Any], _ctx: Any) -> Any:
        """Refuse every tool call so the CLI never runs one.

        By the time this fires the assistant message has already been fully
        streamed, so the caller's ``tool_use`` blocks are safely captured —
        this only stops the CLI from *acting* on them.

        ``interrupt=False`` is deliberate: ``interrupt=True`` aborts the CLI
        session hard enough that it reports a failed result instead of
        completing the turn. The denial alone is sufficient, because the
        read loop stops at ``message_stop`` — the assistant turn we came
        for is complete at that point and nothing further is consumed.
        """
        sdk = _require_sdk()
        return sdk.PermissionResultDeny(
            message="Tool execution is owned by the caller.", interrupt=False
        )

    async def _run(self, kwargs: dict[str, Any]) -> SimpleNamespace:
        sdk = _require_sdk()
        options = self._build_options(sdk, kwargs)
        prompt = build_prompt(list(kwargs.get("messages") or []))
        assembler = MessageAssembler()
        # aclosing is load-bearing: breaking out of the loop leaves the SDK's
        # async generator suspended, and it owns the CLI subprocess. Relying
        # on GC to finalize it leaks a `claude` process per call.
        async with aclosing(sdk.query(prompt=prompt, options=options)) as stream:
            async for message in stream:
                if isinstance(message, sdk.StreamEvent):
                    assembler.feed(message.event)
                    # Stop at the end of the assistant turn. Reading further
                    # would surface the CLI's own end-of-run error when the
                    # turn ended on a denied tool call — which is the normal,
                    # intended outcome here, not a failure.
                    if assembler.complete:
                        break
                elif isinstance(message, sdk.ResultMessage) and message.is_error:
                    raise RuntimeError(
                        f"Claude Agent SDK error ({message.subtype}): "
                        f"{message.result or 'no detail'}"
                    )
        return assembler.finalize()

    def messages_create(self, **kwargs: Any) -> Awaitable[Any]:
        timeout = kwargs.pop("timeout", None) or self._default_timeout_s
        return asyncio.wait_for(self._run(kwargs), timeout=timeout)

    def messages_stream(self, **kwargs: Any) -> AbstractAsyncContextManager[Any]:
        timeout = kwargs.pop("timeout", None) or self._default_timeout_s
        return _AgentSDKStream(provider=self, kwargs=kwargs, timeout=timeout)


class _AgentSDKStream:
    """Async context manager matching ``AsyncMessageStreamManager``.

    Consumers do::

        async with provider.messages_stream(...) as stream:
            async for event in stream:
                ...
            final = await stream.get_final_message()
    """

    def __init__(
        self,
        *,
        provider: AgentSDKProvider,
        kwargs: dict[str, Any],
        timeout: float,
    ) -> None:
        self._provider = provider
        self._kwargs = kwargs
        self._timeout = timeout
        self._assembler = MessageAssembler()
        self._final: SimpleNamespace | None = None

    async def __aenter__(self) -> _AgentSDKStream:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iter_events()

    async def _iter_events(self) -> AsyncIterator[Any]:
        sdk = _require_sdk()
        options = self._provider._build_options(sdk, self._kwargs)
        prompt = build_prompt(list(self._kwargs.get("messages") or []))

        async def _pump(queue: asyncio.Queue[Any]) -> None:
            try:
                # See _run: aclosing keeps the CLI subprocess from leaking
                # when we stop reading at message_stop.
                async with aclosing(sdk.query(prompt=prompt, options=options)) as source:
                    async for message in source:
                        if isinstance(message, sdk.StreamEvent):
                            await queue.put(("event", message.event))
                            if message.event.get("type") == "message_stop":
                                break
                        elif isinstance(message, sdk.ResultMessage) and message.is_error:
                            await queue.put(
                                (
                                    "error",
                                    RuntimeError(
                                        f"Claude Agent SDK error ({message.subtype}): "
                                        f"{message.result or 'no detail'}"
                                    ),
                                )
                            )
                            return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - relayed to the consumer
                await queue.put(("error", exc))
            finally:
                await queue.put(("done", None))

        queue: asyncio.Queue[Any] = asyncio.Queue()
        task = asyncio.create_task(_pump(queue))
        try:
            while True:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=self._timeout)
                if kind == "done":
                    break
                if kind == "error":
                    raise payload
                self._assembler.feed(payload)
                yield _ns(self._normalize(payload))
        finally:
            task.cancel()
            # Await the cancellation so the pump's `aclosing` actually runs
            # before we return — otherwise an early `break` by the consumer
            # returns while the CLI subprocess is still being torn down.
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._final = self._assembler.finalize()

    @staticmethod
    def _normalize(event: dict[str, Any]) -> dict[str, Any]:
        """Strip the MCP prefix from tool names in outbound stream events.

        Without this a consumer watching the event stream (rather than the
        final message) would see ``mcp__openexec__consult_specialist``.
        """
        if event.get("type") != "content_block_start":
            return event
        block = event.get("content_block") or {}
        if block.get("type") != "tool_use":
            return event
        patched = dict(event)
        patched["content_block"] = {
            **block,
            "name": strip_tool_prefix(str(block.get("name", ""))),
        }
        return patched

    async def get_final_message(self) -> Any:
        if self._final is None:
            self._final = self._assembler.finalize()
        return self._final
