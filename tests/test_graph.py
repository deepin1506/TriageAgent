"""Spec for Phase 2 (triage/agent/graph.py). Checks the CONTRACT in graph.py's docstring
and real side effects in the mock, not your internal design.

Wiring is real except the LLM: graph -> ToolGateway -> MCP client -> your MCP server
(in-process) -> SnowClient -> mock API over HTTP. The LLM is tests/fakes.ScriptedLLM.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from mock_snow.seed import INCIDENT_IDS
from tests.conftest import PASSWORD, USER, settings_for
from tests.fakes import ScriptedLLM
from triage.agent.graph import build_graph
from triage.agent.runtime import AGENT_TOOLS, WRITE_TOOLS, AgentDeps, ToolGateway, connect_tools
from triage.mcp_server.server import build_server

pytestmark = pytest.mark.anyio

NUMBER = "INC0010001"  # "VPN keeps disconnecting every 10 minutes", untriaged
SCRIPT: dict[str, Any] = {
    "category": "network",
    "impact": "3",
    "urgency": "2",
    "assignment_group": "Network Operations",
    "rationale": "Single remote user with VPN drops: network issue, low impact, medium urgency.",
    "actions": [
        {
            "tool": "update_incident",
            "args": {
                "number": NUMBER,
                "category": "network",
                "impact": "3",
                "urgency": "2",
                "work_note": "Triage: VPN instability for one remote user.",
            },
        },
        {"tool": "assign_group", "args": {"number": NUMBER, "group": "Network Operations"}},
    ],
    "summary": "Classified as network (P4) and assigned to Network Operations.",
}
APPROVE = {"approved": True, "reviewer": "deepin", "comment": None}
REJECT = {"approved": False, "reviewer": "deepin", "comment": "Wrong group"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def tools(live_server: str, mock_admin: httpx.Client) -> AsyncIterator[ToolGateway]:
    async with connect_tools(build_server(settings_for(live_server))) as gateway:
        yield gateway


@asynccontextmanager
async def compiled(db: Path) -> AsyncIterator[CompiledStateGraph]:
    async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
        yield build_graph().compile(checkpointer=saver)


def config(thread: str = "t-1") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread}}


def writes(admin: httpx.Client) -> list[dict]:
    return [r for r in admin.get("/_mock/requests").json() if r["method"] != "GET"]


def incident(base_url: str, number: str = NUMBER) -> dict[str, str]:
    resp = httpx.get(
        f"{base_url}/api/now/table/incident/{INCIDENT_IDS[number]}",
        params={"sysparm_display_value": "true"},
        auth=(USER, PASSWORD),
    )
    return resp.json()["result"]


async def start(graph: CompiledStateGraph, deps: AgentDeps, thread: str = "t-1") -> dict:
    return await graph.ainvoke({"number": NUMBER}, config(thread), context=deps)


async def test_pauses_for_approval_before_any_write(
    tools: ToolGateway, mock_admin: httpx.Client, tmp_path: Path
) -> None:
    async with compiled(tmp_path / "cp.db") as graph:
        out = await start(graph, AgentDeps(ScriptedLLM(SCRIPT), tools))

    assert len(out.get("__interrupt__", [])) == 1, "expected exactly one pending approval"
    payload = out["__interrupt__"][0].value
    assert payload["number"] == NUMBER
    assert payload["rationale"]
    assert payload["actions"], "the reviewer must see what will run"
    for action in payload["actions"]:
        assert action["tool"] in AGENT_TOOLS and isinstance(action["args"], dict)
    assert any(a["tool"] in WRITE_TOOLS for a in payload["actions"])
    assert writes(mock_admin) == [], "nothing may be written before approval"


async def test_prompts_carry_the_ticket_and_the_group_catalogue(
    tools: ToolGateway, tmp_path: Path
) -> None:
    llm = ScriptedLLM(SCRIPT)
    async with compiled(tmp_path / "cp.db") as graph:
        await start(graph, AgentDeps(llm, tools))
    prompts = "\n".join(str(m.content) for _, messages in llm.calls for m in messages)
    assert "VPN keeps disconnecting" in prompts, "the incident text must reach the LLM"
    # Group descriptions come from the snow://assignment-groups resource, not hardcoding:
    assert "VPN, Wi-Fi, LAN, DNS, firewall, proxy" in prompts


async def test_approve_executes_the_actions(
    tools: ToolGateway, live_server: str, tmp_path: Path
) -> None:
    deps = AgentDeps(ScriptedLLM(SCRIPT), tools)
    async with compiled(tmp_path / "cp.db") as graph:
        await start(graph, deps)
        out = await graph.ainvoke(Command(resume=APPROVE), config(), context=deps)

    assert "__interrupt__" not in out
    assert out["results"] and all(r["ok"] for r in out["results"]), out["results"]
    assert out["summary"]
    record = incident(live_server)
    assert record["category"] == "Network"
    assert record["priority"] == "4 - Low"  # 3 + 2 - 1, derived by ServiceNow
    assert record["assignment_group"] == "Network Operations"


async def test_reject_writes_nothing(
    tools: ToolGateway, mock_admin: httpx.Client, tmp_path: Path
) -> None:
    deps = AgentDeps(ScriptedLLM(SCRIPT), tools)
    async with compiled(tmp_path / "cp.db") as graph:
        await start(graph, deps)
        out = await graph.ainvoke(Command(resume=REJECT), config(), context=deps)

    assert out["results"] == []
    assert out["summary"]
    assert writes(mock_admin) == []


async def test_resume_after_a_restart(
    live_server: str, mock_admin: httpx.Client, tmp_path: Path
) -> None:
    db = tmp_path / "cp.db"
    # Process 1: run until the approval point, then everything is torn down.
    async with (
        connect_tools(build_server(settings_for(live_server))) as tools,
        compiled(db) as graph,
    ):
        await start(graph, AgentDeps(ScriptedLLM(SCRIPT), tools), thread="restart")
    # Process 2: new MCP connection, new graph, same checkpoint file.
    async with (
        connect_tools(build_server(settings_for(live_server))) as tools,
        compiled(db) as graph,
    ):
        deps = AgentDeps(ScriptedLLM(SCRIPT), tools)
        out = await graph.ainvoke(Command(resume=APPROVE), config("restart"), context=deps)
    assert all(r["ok"] for r in out["results"])
    assert incident(live_server)["assignment_group"] == "Network Operations"


async def test_tool_failure_is_recorded_not_raised(
    tools: ToolGateway, mock_admin: httpx.Client, tmp_path: Path
) -> None:
    mock_admin.post(
        "/_mock/faults", json={"status": 503, "count": 20, "method": "PATCH"}
    ).raise_for_status()
    deps = AgentDeps(ScriptedLLM(SCRIPT), tools)
    async with compiled(tmp_path / "cp.db") as graph:
        await start(graph, deps)
        out = await graph.ainvoke(Command(resume=APPROVE), config(), context=deps)

    assert len(out["results"]) == 1, "stop at the first failed action"
    failed = out["results"][0]
    assert failed["ok"] is False and "unavailable" in (failed["error"] or "").lower()
    assert out["summary"]
