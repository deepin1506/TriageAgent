"""The graph's nodes. Each takes the state and returns a dict of the keys it changes.

Deterministic steps (load, retrieve, route, execute, summarize) are plain code; only
classify and propose call the LLM, and both go through call_structured.
"""

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from triage.llm import call_structured

from . import prompts
from .runtime import AgentDeps, ToolCallError
from .schemas import Classification, Decision, Proposal
from .state import TriageState

# ---------------------------------------------------------------- worked examples


async def load(state: TriageState, runtime: Runtime[AgentDeps]) -> dict[str, Any]:
    """Fetch the incident and the group catalogue through MCP. Deterministic: no LLM."""
    tools = runtime.context.tools  # dependencies come from the runtime, never from state
    incident = await tools.call("get_incident", {"number": state["number"]})
    groups = await tools.read_json("snow://assignment-groups")
    return {"incident": incident, "groups": groups}  # only the keys this node sets


async def retrieve(state: TriageState) -> dict[str, Any]:
    """Phase 3 replaces this with vector search over the KB articles."""
    return {"kb": []}


# ------------------------------------------------------------------ agent steps


async def classify(state: TriageState, runtime: Runtime[AgentDeps]) -> dict[str, Any]:
    """LLM call 1: category, impact, urgency and group for the incident."""

    groups_text = "\n".join(f"- {g['name']}: {g['description']}" for g in state["groups"])
    incident = state["incident"]
    messages = [
        SystemMessage(prompts.CLASSIFY_SYSTEM.format(groups=groups_text)),
        HumanMessage(
            prompts.CLASSIFY_TICKET.format(
                number=incident["number"],
                short_description=incident["short_description"],
                description=incident["description"],
            )
        ),
    ]
    result = await call_structured(runtime.context.llm, Classification, messages)
    return {"classification": result.value.model_dump()}


async def propose(state: TriageState, runtime: Runtime[AgentDeps]) -> dict[str, Any]:
    """LLM call 2: turn the classification into tool calls. Code adds the incident number."""
    messages = [
        SystemMessage(prompts.PROPOSE_SYSTEM),
        HumanMessage(
            prompts.PROPOSE_DECISION.format(
                number=state["number"],
                classification=json.dumps(state["classification"], indent=2),
                kb=json.dumps(state.get("kb", []), indent=2),
            )
        ),
    ]
    result = await call_structured(runtime.context.llm, Proposal, messages)
    actions = []
    for action in result.value.actions:
        args = action.args.model_dump(exclude_none=True)
        args["number"] = state["number"]
        actions.append({"tool": action.tool, "args": args})
    return {"proposal": {"actions": actions, "rationale": result.value.rationale}}


def approve(state: TriageState) -> dict[str, Any]:
    """Pause for a human. Nothing else happens here: on resume this node re-runs from the top."""
    decision = interrupt(
        {
            "number": state["number"],
            "actions": state["proposal"]["actions"],
            "rationale": state["proposal"]["rationale"],
            "classification": state["classification"],
        }
    )
    validated = Decision.model_validate(decision)
    return {"decision": validated.model_dump()}


def route(state: TriageState) -> str:
    """After approval: which node runs next."""
    approved = state.get("decision", {}).get("approved") is True
    if approved:
        return "execute"
    return "summarize"


async def execute(state: TriageState, runtime: Runtime[AgentDeps]) -> dict[str, Any]:
    """Run the approved actions in order. Stop at the first failure; never raise."""
    if state.get("decision", {}).get("approved") is not True:
        return {"results": []}  # second check behind the routing: no approval, no writes

    results: list[dict[str, Any]] = []
    for action in state["proposal"]["actions"]:
        try:
            await runtime.context.tools.call(action["tool"], action["args"])
            results.append({"tool": action["tool"], "ok": True, "error": None})
        except ToolCallError as err:
            results.append({"tool": action["tool"], "ok": False, "error": err.message})
            break
    return {"results": results}


def summarize(state: TriageState) -> dict[str, Any]:
    """One readable line for the reviewer and the run log. Deterministic: no LLM."""
    results = state.get("results", [])
    c = state["classification"]
    decided = f"{c['category']}, impact {c['impact']}, urgency {c['urgency']}"
    if not state.get("decision", {}).get("approved"):
        summary = f"Rejected; nothing changed. Proposed: {decided}, group {c['assignment_group']}."
    elif all(r["ok"] for r in results):
        summary = f"Applied: {decided}; assigned to {c['assignment_group']}."
    else:
        failed = results[-1]
        summary = f"Stopped at {failed['tool']}: {failed['error']}"
    return {"results": results, "summary": summary}
