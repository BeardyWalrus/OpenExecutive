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
    # name != domain on purpose: the real board_comms agent is name
    # "board_comms" / domain "board", and a stub where they coincide hides
    # every confusion between the two.
    name = "board_comms"
    domain = "board"
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
    assert row["actor"] == "board_comms"
    # No department tag: `domain` is not a department slug (this agent's slug
    # is "board_comms", its domain is "board"), and cache_event is too
    # high-volume to sit in department_check_in's pre-filter window.
    assert row.get("department") is None


def test_the_row_inherits_the_turn_from_the_audit_contextvars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert on the PERSISTED row, not on what the emitter passes.

    The earlier version of this test patched audit.usage.audit_log, which sits
    ABOVE log_event's ContextVar fallback -- the entire mechanism it names. It
    asserted the emitter passes None (true, and the point) and separately that
    asyncio.create_task copies ContextVars (a CPython property, not this
    diff's). It passed with the fallback deleted. This one drives a real
    logger, so deleting the fallback fails it.
    """
    logged: list[dict[str, Any]] = []

    class _Logger:
        def log(self, event_type: str, summary: str, **kw: Any) -> int:
            logged.append({"event_type": event_type, **kw})
            return 1

    from openexecutive.audit import set_audit_logger

    async def _fake_create(**kwargs: Any) -> SimpleNamespace:
        return _message()

    monkeypatch.setattr(
        "openexecutive.agents.base.get_provider",
        lambda model: SimpleNamespace(messages_create=_fake_create),
    )
    set_audit_logger(_Logger())
    try:

        async def _run() -> None:
            with set_turn(session_id="sess-1", turn_id="turn-9"):
                await _Agent().analyze("q")

        asyncio.run(_run())
    finally:
        set_audit_logger(None)

    rows = [e for e in logged if e["event_type"] == "cache_event"]
    assert len(rows) == 1, "the specialist row never reached the logger"
    assert rows[0]["session_id"] == "sess-1"
    assert rows[0]["turn_id"] == "turn-9"


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


def test_parallel_fan_out_tags_every_row_with_its_own_turn(
    captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real dispatch is `asyncio.gather` over coroutines (router.py:271),
    not create_task, and two chat turns can be in flight at once. Each row must
    carry its own turn: a leak would misattribute one user's spend to another,
    and an untagged row is invisible in the session-grouped view."""

    class _Named(_Agent):
        def __init__(self, name: str) -> None:
            self.name = name  # type: ignore[misc]
            self.domain = name  # type: ignore[misc]

    seen: list[tuple[str | None, str | None, str]] = []

    def _fake_log(event_type: str, summary: str, **kw: Any) -> None:
        if event_type != "cache_event":
            return
        from openexecutive.audit.context import get_active_ids

        # log_event resolves None ids from the ContextVars; do the same here so
        # the assertion sees what the row would actually be stored with.
        sid, tid = get_active_ids()
        seen.append((kw.get("session_id") or sid, kw.get("turn_id") or tid,
                     kw["actor"]))

    # monkeypatch, not a bare assignment: an unrestored patch here would leak
    # into every later test in the session and fail by ordering.
    monkeypatch.setattr("openexecutive.audit.usage.audit_log", _fake_log)

    async def _turn(session: str, names: list[str]) -> None:
        with set_turn(session_id=session, turn_id=f"{session}-t1"):
            await asyncio.gather(*(_Named(n).analyze("q") for n in names))

    async def _both() -> None:
        await asyncio.gather(
            _turn("sess-A", ["finance", "legal"]),
            _turn("sess-B", ["ops"]),
        )

    asyncio.run(_both())

    assert len(seen) == 3, seen
    by_actor = {actor: (sid, tid) for sid, tid, actor in seen}
    assert by_actor["finance"] == ("sess-A", "sess-A-t1")
    assert by_actor["legal"] == ("sess-A", "sess-A-t1")
    assert by_actor["ops"] == ("sess-B", "sess-B-t1")


def test_analyze_with_tools_also_emits(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]
) -> None:
    """The second call site. Workflow specialist calls are real spend too."""

    async def _fake_create(**kwargs: Any) -> SimpleNamespace:
        return _message()

    monkeypatch.setattr(
        "openexecutive.agents.base.get_provider",
        lambda model: SimpleNamespace(messages_create=_fake_create),
    )
    asyncio.run(_Agent().analyze_with_tools("q", tools=[]))
    assert len([r for r in captured if r["event_type"] == "cache_event"]) == 1


def test_a_malformed_token_count_cannot_abort_the_fan_out(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]
) -> None:
    """The real failure mode the 'never takes the answer down' test missed.

    The counts were converted with a bare int(). router.py dispatches the
    fan-out with asyncio.gather WITHOUT return_exceptions=True, so one provider
    returning a non-numeric count would abort every specialist in the turn --
    not just its own.
    """

    async def _bad(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="fine")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens="1.2k", output_tokens=None),
        )

    monkeypatch.setattr(
        "openexecutive.agents.base.get_provider",
        lambda model: SimpleNamespace(messages_create=_bad),
    )
    assert asyncio.run(_Agent().analyze("q")) == "fine"
    row = next(r for r in captured if r["event_type"] == "cache_event")
    assert row["details"]["input_tokens"] == 0


def test_dict_usage_is_read_rather_than_silently_zeroed(
    captured: list[dict[str, Any]]
) -> None:
    """Every getattr falling through to 0 emits a row that looks real, sums to
    nothing, and understates the totals. Zero is a lie here; absence is not."""
    from openexecutive.audit.usage import emit_cache_event

    emit_cache_event(
        final_msg=SimpleNamespace(
            usage={"input_tokens": 500, "output_tokens": 100}, stop_reason="end_turn"
        ),
        model="claude-opus-5",
    )
    d = next(r for r in captured if r["event_type"] == "cache_event")["details"]
    assert (d["input_tokens"], d["output_tokens"]) == (500, 100)
