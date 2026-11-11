"""Phase 6: per-step tracing, run records, the run log and the /metrics aggregates."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from tests.conftest import settings_for
from tests.fakes import ScriptedLLM
from tests.test_graph import APPROVE, NUMBER, SCRIPT
from triage.agent.graph import build_graph
from triage.agent.runtime import AgentDeps, ToolGateway, connect_tools
from triage.mcp_server.server import build_server
from triage.observability import RunLog, approval_wait_s, metrics, run_record

pytestmark = pytest.mark.anyio
NODES = [
    "load",
    "guard_input",
    "classify",
    "retrieve",
    "propose",
    "check_policy",
    "approve",
    "execute",
    "summarize",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def tools(live_server: str, mock_admin: httpx.Client) -> AsyncIterator[ToolGateway]:
    async with connect_tools(build_server(settings_for(live_server))) as gateway:
        yield gateway


async def approved_run(tools: ToolGateway) -> dict[str, Any]:
    graph = build_graph().compile(checkpointer=InMemorySaver())
    deps, config = AgentDeps(ScriptedLLM(SCRIPT), tools), {"configurable": {"thread_id": "t"}}
    await graph.ainvoke({"number": NUMBER}, config, context=deps)
    return await graph.ainvoke(Command(resume=APPROVE), config, context=deps)


async def test_every_step_is_timed_and_llm_usage_is_attributed(tools: ToolGateway) -> None:
    state = await approved_run(tools)
    trace = state["trace"]
    # approve appears once: its first pass raised the interrupt, so only the resume counts
    assert [e["node"] for e in trace] == NODES
    assert all(e["ms"] >= 0 and e["started_at"] for e in trace)
    llm = {e["node"]: e["llm"] for e in trace if "llm" in e}
    assert set(llm) == {"classify", "propose"}  # the only nodes that call the LLM
    assert llm["classify"][0]["schema"] == "Classification"
    assert llm["classify"][0]["input_tokens"] == 100  # tests/fakes.py USAGE
    assert llm["propose"][0]["attempts"] == 1 and llm["propose"][0]["ok"]


async def test_run_record_totals_and_cost(tools: ToolGateway, live_server: str) -> None:
    state = await approved_run(tools)
    unpriced = run_record("t", state, settings_for(live_server))
    assert unpriced["outcome"] == "applied" and unpriced["reviewer"] == "deepin"
    assert (unpriced["llm_calls"], unpriced["input_tokens"], unpriced["output_tokens"]) == (
        2,
        200,
        40,
    )
    assert unpriced["cost_usd"] is None  # no prices configured: unknown, never guessed
    assert unpriced["classification"]["group"] == "Network Operations"
    assert [s["node"] for s in unpriced["steps"]] == NODES

    priced = run_record(
        "t",
        state,
        settings_for(live_server, llm_price_input_per_mtok=0.10, llm_price_output_per_mtok=0.40),
    )
    assert priced["cost_usd"] == pytest.approx((200 * 0.10 + 40 * 0.40) / 1_000_000)


def test_approval_wait_is_the_gap_between_proposal_and_decision() -> None:
    trace = [
        {"node": "check_policy", "started_at": "2026-11-11T10:00:00.000+00:00", "ms": 500.0},
        {"node": "approve", "started_at": "2026-11-11T10:02:00.500+00:00", "ms": 1.0},
    ]
    assert approval_wait_s(trace) == 120.0
    assert approval_wait_s(trace[:1]) is None  # never decided


def test_outcomes_cover_crashes_blocks_and_rejections(live_server: str) -> None:
    s = settings_for(live_server)
    assert run_record("t", {}, s, error="GoogleAPIError: 503")["outcome"] == "error"
    assert run_record("t", {"guard": {"blocked": True}}, s)["outcome"] == "blocked"
    rejected = {"decision": {"approved": False, "reviewer": "r"}}
    assert run_record("t", rejected, s)["outcome"] == "rejected"
    failed = {"decision": {"approved": True}, "results": [{"tool": "x", "ok": False}]}
    assert run_record("t", failed, s)["outcome"] == "failed"


def test_metrics_aggregate_the_log() -> None:
    def rec(outcome: str, agent_ms: float, **kw: Any) -> dict[str, Any]:
        return {"outcome": outcome, "agent_ms": agent_ms, "llm_calls": 2, "llm_attempts": 2} | kw

    records = [
        rec("applied", 4000, approval_wait_s=30, cost_usd=0.001, input_tokens=900),
        rec("applied", 6000, approval_wait_s=90, cost_usd=0.003, llm_attempts=3),
        rec("rejected", 5000, approval_wait_s=10, injection_flagged=True),
        rec("blocked", 100, injection_flagged=True),
        rec("error", 60000),
    ]
    m = metrics(records)
    assert m["runs"] == 5 and m["errors"] == 1
    assert m["approval_rate"] == round(2 / 3, 3)  # blocked and crashed runs weren't decided
    assert m["agent_latency_s"] == {"p50": 5.0, "p95": 6.0}  # the crash's 60 s is excluded
    assert m["approval_wait_s"]["p50"] == 30
    assert m["cost_usd"] == {"total": 0.004, "per_run": 0.002}
    assert m["llm_repair_retries"] == 1 and m["injection_flagged"] == 2
    assert metrics([])["approval_rate"] is None


def test_run_log_round_trip_survives_a_corrupt_line(tmp_path: Path) -> None:
    log = RunLog(tmp_path / "runs" / "runs.jsonl")
    assert log.read() == []
    log.append({"outcome": "applied", "agent_ms": 1.0})
    with log.path.open("a", encoding="utf-8") as f:
        f.write('{"half a line\n')  # e.g. the process died mid-write
    log.append({"outcome": "rejected", "agent_ms": 2.0})
    assert [r["outcome"] for r in log.read()] == ["applied", "rejected"]
