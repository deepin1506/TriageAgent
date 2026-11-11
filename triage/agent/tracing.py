"""Per-step tracing: how long each node took, and what its LLM calls cost in tokens.

`traced(name, fn)` wraps a node. Each run of the node appends one event to state["trace"]
(a list with an append reducer), so the trace is checkpointed with everything else and
survives the pause for approval, even when the run is resumed by another process.

    {"node": "classify", "started_at": "2026-...Z", "ms": 812.4,
     "llm": [{"schema": "Classification", "input_tokens": 900, "output_tokens": 60, ...}]}

The approval pause is not a node's time: approve raises an interrupt on the first pass
(no event) and is re-run on resume, so the human wait shows up as the gap between
check_policy's end and approve's start (see triage/observability.py).
"""

import inspect
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from langgraph.runtime import Runtime

from triage.llm import metering

from .runtime import AgentDeps
from .state import TriageState

Node = Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]]


def traced(name: str, fn: Node) -> Callable[..., Awaitable[dict[str, Any]]]:
    wants_runtime = "runtime" in inspect.signature(fn).parameters

    # Deliberately not functools.wraps: LangGraph reads the signature to decide whether to
    # pass `runtime`, and wraps would make it see the wrapped function's signature instead.
    async def node(state: TriageState, runtime: Runtime[AgentDeps]) -> dict[str, Any]:
        started_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        start = time.perf_counter()
        with metering() as calls:
            out = fn(state, runtime) if wants_runtime else fn(state)
            if inspect.isawaitable(out):
                out = await out
        event: dict[str, Any] = {
            "node": name,
            "started_at": started_at,
            "ms": round((time.perf_counter() - start) * 1000, 1),
        }
        if calls:
            event["llm"] = calls
        return {**out, "trace": [event]}

    node.__name__ = name
    return node
