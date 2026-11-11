"""Per-node specs for Phase 2: fast feedback while you write nodes.py one function at a time.

uv run pytest tests/test_nodes.py -k classify      # just the node you're on
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import ValidationError

from tests.conftest import settings_for
from tests.fakes import ScriptedLLM
from tests.test_graph import NUMBER, SCRIPT, incident, writes
from triage.agent import nodes
from triage.agent.runtime import AgentDeps, ToolGateway, connect_tools
from triage.agent.state import TriageState
from triage.mcp_server.server import build_server

pytestmark = pytest.mark.anyio

INCIDENT = {
    "number": NUMBER,
    "short_description": "VPN keeps disconnecting every 10 minutes",
    "description": "Since yesterday my VPN drops roughly every 10 minutes when working from home.",
    "category": "",
    "impact": "3",
    "urgency": "3",
    "priority": "5",
    "state": "1",
    "assignment_group": "",
    "caller": "Rahul Verma",
    "correlation_id": "",
    "updated_on": "2026-10-20 09:00:00",
}
GROUPS = [
    {"name": "Network Operations", "description": "VPN, Wi-Fi, LAN, DNS, firewall, proxy"},
    {"name": "Service Desk", "description": "L1: how-to questions, general requests"},
]
CLASSIFIED: TriageState = {
    "number": NUMBER,
    "incident": INCIDENT,
    "groups": GROUPS,
    "kb": [],
    "classification": {
        "category": "network",
        "impact": "3",
        "urgency": "2",
        "assignment_group": "Network Operations",
        "rationale": "VPN drops for one user.",
    },
}
ACTIONS = [
    {"tool": "update_incident", "args": {"number": NUMBER, "category": "network", "impact": "3"}},
    {"tool": "assign_group", "args": {"number": NUMBER, "group": "Network Operations"}},
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def tools(live_server: str, mock_admin: httpx.Client) -> AsyncIterator[ToolGateway]:
    async with connect_tools(build_server(settings_for(live_server))) as gateway:
        yield gateway


def runtime(llm: Any = None, tools: Any = None) -> Runtime:
    return Runtime(context=AgentDeps(llm=llm, tools=tools))


def with_decision(approved: bool | None, actions: list[dict] = ACTIONS) -> TriageState:
    state: TriageState = {**CLASSIFIED, "proposal": {"actions": actions, "rationale": "r"}}
    if approved is not None:
        state["decision"] = {"approved": approved, "reviewer": "deepin", "comment": None}
    return state


# --- load (worked example: passes already) --------------------------------------------


async def test_load_fetches_the_incident_and_the_group_catalogue(tools: ToolGateway) -> None:
    out = await nodes.load({"number": NUMBER}, runtime(tools=tools))
    assert out["incident"]["short_description"] == INCIDENT["short_description"]
    assert {"name": "Network Operations", "description": GROUPS[0]["description"]} in out["groups"]


# --- classify -----------------------------------------------------------------------------


async def test_classify_returns_the_classification_as_a_dict() -> None:
    state = {k: CLASSIFIED[k] for k in ("number", "incident", "groups")}
    out = await nodes.classify(state, runtime(ScriptedLLM(SCRIPT)))
    c = out["classification"]
    assert isinstance(c, dict), "store plain dicts in state (model.model_dump())"
    assert (c["category"], c["impact"], c["urgency"]) == ("network", "3", "2")
    assert c["assignment_group"] == "Network Operations"


async def test_classify_keeps_the_ticket_out_of_the_instructions() -> None:
    llm = ScriptedLLM(SCRIPT)
    await nodes.classify(CLASSIFIED, runtime(llm))
    system, human = llm.calls[0][1][0], llm.calls[0][1][-1]
    assert "VPN, Wi-Fi, LAN, DNS, firewall, proxy" in str(system.content)  # groups listed
    assert "VPN keeps disconnecting" not in str(system.content)  # ticket text is not instructions
    assert "<ticket>" in str(human.content) and "VPN keeps disconnecting" in str(human.content)


# --- propose ------------------------------------------------------------------------------


async def test_propose_returns_tool_calls_for_this_incident() -> None:
    out = await nodes.propose(CLASSIFIED, runtime(ScriptedLLM(SCRIPT)))
    actions = out["proposal"]["actions"]
    assert [a["tool"] for a in actions] == ["update_incident", "assign_group"]
    assert all(a["args"]["number"] == NUMBER for a in actions)
    assert actions[1]["args"]["group"] == "Network Operations"
    assert out["proposal"]["rationale"]


async def test_propose_never_lets_the_llm_choose_the_incident() -> None:
    hijack = {
        **SCRIPT,
        "actions": [
            {
                "tool": "assign_group",
                "args": {"number": "INC0010006", "group": "Security Operations"},
            }
        ],
    }
    out = await nodes.propose(CLASSIFIED, runtime(ScriptedLLM(hijack)))
    assert out["proposal"]["actions"][0]["args"]["number"] == NUMBER


# --- approve + route ------------------------------------------------------------------------


def one_node_graph() -> Any:
    g = StateGraph(TriageState)
    g.add_node("approve", nodes.approve)
    g.add_edge(START, "approve")
    g.add_edge("approve", END)
    return g.compile(checkpointer=InMemorySaver())


async def test_approve_pauses_with_the_proposal_then_stores_the_decision() -> None:
    graph, cfg = one_node_graph(), {"configurable": {"thread_id": "a"}}
    out = await graph.ainvoke(with_decision(None), cfg)
    payload = out["__interrupt__"][0].value
    assert payload["number"] == NUMBER and payload["actions"] == ACTIONS and payload["rationale"]

    decision = {"approved": True, "reviewer": "deepin", "comment": "ok"}
    out = await graph.ainvoke(Command(resume=decision), cfg)
    assert out["decision"] == decision


async def test_approve_rejects_a_malformed_decision() -> None:
    graph, cfg = one_node_graph(), {"configurable": {"thread_id": "b"}}
    await graph.ainvoke(with_decision(None), cfg)
    with pytest.raises(ValidationError):  # "approved" is missing
        await graph.ainvoke(Command(resume={"reviewer": "deepin"}), cfg)


async def test_route() -> None:
    assert nodes.route(with_decision(True)) == "execute"
    assert nodes.route(with_decision(False)) == "summarize"
    assert nodes.route(with_decision(None)) == "summarize"


# --- execute --------------------------------------------------------------------------------


async def test_execute_runs_the_actions_in_order(tools: ToolGateway, live_server: str) -> None:
    out = await nodes.execute(with_decision(True), runtime(tools=tools))
    assert out["results"] == [
        {"tool": "update_incident", "ok": True, "error": None},
        {"tool": "assign_group", "ok": True, "error": None},
    ]
    assert incident(live_server)["assignment_group"] == "Network Operations"


async def test_execute_refuses_without_approval(
    tools: ToolGateway, mock_admin: httpx.Client
) -> None:
    for state in (with_decision(False), with_decision(None)):
        assert (await nodes.execute(state, runtime(tools=tools)))["results"] == []
    assert writes(mock_admin) == []


async def test_execute_stops_at_the_first_failure(
    tools: ToolGateway, mock_admin: httpx.Client
) -> None:
    bad_first = [{"tool": "create_incident", "args": {}}, *ACTIONS]  # not in the allowlist
    out = await nodes.execute(with_decision(True, bad_first), runtime(tools=tools))
    assert len(out["results"]) == 1
    assert out["results"][0]["ok"] is False and "allowlist" in out["results"][0]["error"]
    assert writes(mock_admin) == []


# --- summarize ------------------------------------------------------------------------------


async def test_summarize_covers_rejected_applied_and_failed() -> None:
    rejected = nodes.summarize(with_decision(False))
    assert rejected["results"] == [] and "reject" in rejected["summary"].lower()

    ok = [{"tool": "update_incident", "ok": True, "error": None}]
    applied = nodes.summarize({**with_decision(True), "results": ok})
    assert "Network Operations" in applied["summary"]

    bad = [{"tool": "assign_group", "ok": False, "error": "ServiceNow is unavailable"}]
    failed = nodes.summarize({**with_decision(True), "results": bad})
    assert "assign_group" in failed["summary"] and "unavailable" in failed["summary"]
