"""HTTP-backed LLMProvider that targets the OpenRouter chat-completions API.

Implements the same ``messages_create`` / ``messages_stream`` surface as
``AnthropicProvider``, but routes through OpenRouter's OpenAI-format
endpoint. The translator package handles the request/response/stream
shape conversion so call sites stay Anthropic-native.

Streaming notes:

* OpenRouter SSE chunks arrive as JSON objects in ``data: ...`` lines.
* The Executive's loop iterates ``async for event in stream`` looking for
  ``content_block_delta`` with ``delta.type == "text_delta"`` (the streaming
  text path) and then awaits ``stream.get_final_message()`` to read the
  fully assembled message (with tool_use blocks). We yield text deltas
  inline and accumulate tool_call fragments into the final message.
"""
from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any

import httpx

from openexecutive.providers.feature_gate import FeatureSpec, apply_feature_gates
from openexecutive.providers.translator import (
    StreamAccumulator,
    from_openai_response,
    to_openai_request,
)

logger = logging.getLogger(__name__)


class OpenRouterProvider:
    """LLMProvider implementation backed by OpenRouter.

    Holds one ``httpx.AsyncClient`` for the lifetime of the process; the
    client pools its own TCP connections and is async-safe.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        app_title: str = "Open Executive",
        referer: str | None = None,
        timeout_s: float = 180.0,
        slug_lookup: dict[str, str] | None = None,
        spec_lookup: dict[str, FeatureSpec] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        # OpenRouter attribution headers — surfaced in your dashboard alongside
        # the cost data so you can attribute usage back to this app.
        headers = {"HTTP-Referer": referer or "https://github.com/", "X-Title": app_title}
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout_s,
        )
        # internal_name → OpenRouter slug. Populated by the registry from
        # MODEL_SPECS so call sites can use the same model names everywhere.
        self._slug_lookup = slug_lookup or {}
        self._spec_lookup = spec_lookup or {}

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _resolve(self, anthropic_model: str) -> tuple[str, FeatureSpec]:
        slug = self._slug_lookup.get(anthropic_model, anthropic_model)
        spec = self._spec_lookup.get(
            anthropic_model,
            # Default for unknown models: assume non-Claude, no Anthropic-only
            # features. Safer than the optimistic default — a misconfigured
            # slug then quietly gets the right gating.
            FeatureSpec(
                supports_cache_control=False,
                supports_thinking=False,
                supports_web_search=False,
                supports_tool_use=True,
            ),
        )
        return slug, spec

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    # ------------------------------------------------------------------
    # LLMProvider surface
    # ------------------------------------------------------------------

    def messages_create(self, **kwargs: Any) -> Awaitable[Any]:
        return self._messages_create(kwargs)

    async def _messages_create(self, kwargs: dict[str, Any]) -> Any:
        # Strip the SDK's own ``timeout`` kwarg — httpx already has it from
        # the client; passing it into the body would break the request.
        request_timeout = kwargs.pop("timeout", None)
        model = kwargs.pop("model", "")
        slug, spec = self._resolve(model)
        gated = apply_feature_gates(spec, kwargs)
        body = to_openai_request(slug, gated)

        try:
            resp = await self._client.post(
                "/chat/completions",
                json=body,
                headers=self._auth_headers(),
                timeout=request_timeout if request_timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "OpenRouter %s returned %s: %s",
                slug,
                exc.response.status_code,
                exc.response.text[:500],
            )
            raise
        return from_openai_response(resp.json())

    def messages_stream(self, **kwargs: Any) -> AbstractAsyncContextManager[Any]:
        request_timeout = kwargs.pop("timeout", None)
        model = kwargs.pop("model", "")
        slug, spec = self._resolve(model)
        gated = apply_feature_gates(spec, kwargs)
        body = to_openai_request(slug, gated)
        body["stream"] = True
        return _OpenRouterStream(
            client=self._client,
            body=body,
            headers=self._auth_headers(),
            timeout=request_timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class _OpenRouterStream:
    """Async context manager that wraps an OpenRouter SSE response so it
    quacks like ``anthropic.AsyncMessageStreamManager``.

    Consumers do:

        async with provider.messages_stream(...) as stream:
            async for event in stream:
                ...  # event has Anthropic shape
            final = await stream.get_final_message()
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        headers: dict[str, str],
        timeout: float | None,
    ) -> None:
        self._client = client
        self._body = body
        self._headers = headers
        self._timeout = timeout
        self._response_cm: contextlib.AbstractAsyncContextManager[httpx.Response] | None = None
        self._response: httpx.Response | None = None
        self._accumulator = StreamAccumulator()
        self._finalized: SimpleNamespace | None = None

    async def __aenter__(self) -> _OpenRouterStream:
        # ``client.stream(...)`` is a context manager; we open it here and
        # close it in ``__aexit__`` so callers get the same scoping as
        # Anthropic's manager.
        self._response_cm = self._client.stream(
            "POST",
            "/chat/completions",
            json=self._body,
            headers=self._headers,
            timeout=self._timeout if self._timeout is not None else httpx.USE_CLIENT_DEFAULT,
        )
        self._response = await self._response_cm.__aenter__()
        # If OpenRouter returned a non-2xx we must close the response context
        # ourselves — the outer ``async with`` will NOT call __aexit__ when
        # __aenter__ raises, so a naive ``raise_for_status`` would leak the
        # connection back to the pool half-read.
        if self._response.status_code >= 400:
            text = await self._response.aread()
            logger.error(
                "OpenRouter stream returned %s: %s",
                self._response.status_code,
                text.decode("utf-8", errors="replace")[:500],
            )
            try:
                self._response.raise_for_status()
            finally:
                await self._response_cm.__aexit__(None, None, None)
                self._response_cm = None
                self._response = None
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._response_cm is not None:
            await self._response_cm.__aexit__(*exc_info)
        self._response_cm = None
        self._response = None

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iter_events()

    async def _iter_events(self) -> AsyncIterator[Any]:
        if self._response is None:
            return
        async for raw_line in self._response.aiter_lines():
            line = raw_line.strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                logger.debug("OpenRouter: undecodable SSE payload %s", payload[:120])
                continue
            for event in self._accumulator.feed(chunk):
                yield event
        # Cache the final message so a later get_final_message() call returns
        # immediately without re-reading the stream.
        self._finalized = self._accumulator.finalize()

    async def get_final_message(self) -> Any:
        if self._finalized is None:
            # The consumer didn't iterate the stream; drive it to completion.
            async for _ in self._iter_events():
                pass
        # _iter_events sets _finalized in finalize(); if for some reason we
        # got here without it being set, fall back to a fresh finalize().
        if self._finalized is None:
            self._finalized = self._accumulator.finalize()
        return self._finalized
