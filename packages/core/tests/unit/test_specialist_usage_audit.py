"""Specialist calls must reach /audit/usage.

`_emit_cache_event` used to be private to orchestrator/executive.py, so only
Executive iterations were recorded. The specialist fan-out — where a
cross-domain turn spends most of its tokens — produced no row at all, so the
usage totals systematically omitted their largest component. On a Claude
subscription that is the number the allowance is consumed by.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.agents.base import BaseAgent
from openexecutive.audit.context import set_turn


class _Agent(BaseAgent):
    name = "finance"
    domain = "finance"
    model = "claude-sonnet-5"

    def get_system_prompt(self) -> str:
        return "you are finance"


def _message(text: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=1_000,
            output_tokens=200,
            cache_creation_input_tokens=50,
            cache_read_input_tokens=9_000,
            cost=0.0125,
            cost_is_estimate=True,
        ),
    )


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def _fake_log(event_type: str, summary: str, **kw: Any) -> None:
        rows.append({"event_type": event_type, "summary": summary, **kw})

    monkeypatch.setattr("openexecutive.audit.usage.audit_log", _fake_log)

    async def _fake_create(**kwargs: Any) -> SimpleNamespace:
        return _message()

    monkeypatch.setattr(
        "openexecutive.agents.base.get_provider",
        lambda model: SimpleNamespace(messages_create=_fake_create),
    )
    return rows


def test_analyze_emits_a_cache_event(captured: list[dict[str, Any]]) -> None:
    asyncio.run(_Agent().analyze("what is our runway?"))
    events = [r for r in captured if r["event_type"] == "cache_event"]
    assert len(events) == 1, "the specialist fan-out must reach /audit/usage"
    d = events[0]["details"]
    assert d["input_tokens"] == 1_000
    assert d["output_tokens"] == 200
    assert d["cache_read_input_tokens"] == 9_000
    assert d["cost_usd"] == pytest.approx(0.0125)
    assert d["cost_is_estimate"] is True


def test_the_row_is_attributed_to_the_specialist_not_the_executive(
    captured: list[dict[str, Any]],
) -> None:
    """Otherwise the by-actor view cannot tell which specialist spent what."""
    asyncio.run(_Agent().analyze("q"))
    row = next(r for r in captured if r["event_type"] == "cache_event")
    assert row["actor"] == "finance"
    assert row["department"] == "finance"


def test_the_row_inherits_the_turn_from_the_audit_contextvars(
    captured: list[dict[str, Any]],
) -> None:
    """No session_id would make the row invisible in the session-grouped view.
    The specialist signature carries no ids, so this is the whole mechanism."""

    async def _run() -> None:
        with set_turn(session_id="sess-1", turn_id="turn-9"):
            await _Agent().analyze("q")

    asyncio.run(_run())
    row = next(r for r in captured if r["event_type"] == "cache_event")
    # Passed as None here; audit.log_event resolves them from the ContextVars.
    assert row["session_id"] is None and row["turn_id"] is None

    from openexecutive.audit.context import get_active_ids

    async def _check() -> tuple[str | None, str | None]:
        with set_turn(session_id="sess-1", turn_id="turn-9"):
            return await asyncio.create_task(_ids())

    async def _ids() -> tuple[str | None, str | None]:
        return get_active_ids()

    assert asyncio.run(_check()) == ("sess-1", "turn-9"), (
        "ContextVars must propagate into fan-out tasks, or parallel "
        "specialists would log untagged rows"
    )


def test_a_provider_without_usage_does_not_break_the_call(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]
) -> None:
    """Auditing is never allowed to take the specialist's answer down."""

    async def _no_usage(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="fine")],
            stop_reason="end_turn",
        )

    monkeypatch.setattr(
        "openexecutive.agents.base.get_provider",
        lambda model: SimpleNamespace(messages_create=_no_usage),
    )
    assert asyncio.run(_Agent().analyze("q")) == "fine"
    assert not [r for r in captured if r["event_type"] == "cache_event"]
