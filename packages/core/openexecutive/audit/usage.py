"""One emitter for the per-call token + cost audit row (`cache_event`).

Lives here rather than in the orchestrator because two very different callers
need it and neither may import the other: the Executive's own iterations
(orchestrator/executive.py) and every specialist call (agents/base.py).

That split is the whole point. Before this module the emitter was private to
executive.py, so only Executive iterations were ever recorded — the specialist
fan-out, which is where a cross-domain question spends most of its tokens,
never reached `/audit/usage` at all. The totals were not merely imprecise;
they systematically omitted the largest component of real usage, and on a
Claude subscription that is the number the allowance is actually consumed by.

session_id / turn_id are deliberately NOT parameters at the specialist call
sites. `audit.log_event` already falls back to the audit ContextVars bound for
the turn (see audit/context.py, whose docstring names specialist routing as
exactly the fan-out this solves), and ContextVars propagate into the tasks a
parallel fan-out creates. Threading the ids through `analyze()` and every
caller would touch far more surface for no additional correctness.
"""

from __future__ import annotations

from typing import Any

from openexecutive.audit import log_event as audit_log


def emit_cache_event(
    *,
    final_msg: Any,
    model: str,
    iteration: int = 0,
    actor: str = "executive",
    session_id: str | None = None,
    turn_id: str | None = None,
    department: str | None = None,
) -> None:
    """Capture token + cache stats from one completed provider response.

    Reads from ``final_msg.usage`` (a SimpleNamespace from the provider
    abstraction in providers/translator.py). All getattrs are guarded so a
    provider that doesn't surface a particular field still emits a row with
    what it does have — never blocks the caller.

    ``iteration`` is the Executive's loop counter; specialist calls are a
    single shot and pass 0.
    """
    usage = getattr(final_msg, "usage", None)
    if usage is None:
        return
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    cache_create = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    stop_reason = getattr(final_msg, "stop_reason", None)
    # USD for this call. Two different kinds of number share this field:
    # OpenRouter wires back what it ACTUALLY charged, while the Agent SDK
    # (subscription) path has nothing to charge and instead reports a
    # dollar-equivalent estimated from token counts at list prices. Both are
    # worth recording -- a subscription's allowance is what runs out, and
    # zero would hide it -- but they must not be silently interchangeable, so
    # `cost_is_estimate` travels with the figure. Still None on the
    # Anthropic-direct path (no cost wire, no estimate); aggregation treats a
    # missing cost as 0.
    raw_cost = getattr(usage, "cost", None)
    try:
        cost_usd = float(raw_cost) if raw_cost is not None else None
    except (TypeError, ValueError):
        cost_usd = None
    cost_is_estimate = bool(getattr(usage, "cost_is_estimate", False))
    audit_log(
        "cache_event",
        f"{model} iter={iteration} in={inp} out={out} cache_read={cache_read} "
        f"cache_create={cache_create} cost={cost_usd}"
        f"{' (est)' if cost_is_estimate else ''} stop={stop_reason}",
        session_id=session_id,
        turn_id=turn_id,
        actor=actor,
        department=department,
        details={
            "model": model,
            "iteration": iteration,
            "input_tokens": inp,
            "output_tokens": out,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
            "cost_usd": cost_usd,
            "cost_is_estimate": cost_is_estimate,
            "stop_reason": stop_reason,
        },
    )
