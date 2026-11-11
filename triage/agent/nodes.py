"""The graph's nodes. Each takes the state and returns a dict of the keys it changes.

Deterministic steps (load, guard_input, retrieve, check_policy, route, execute, summarize)
are plain code; only classify and propose call the LLM, both through call_structured.
"""

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from triage.llm import call_structured

from . import prompts
from .guardrails import actions_hash, check_proposal, scan_for_injection
from .runtime import AgentDeps, ToolCallError
from .schemas import Classification, Decision, Proposal
from .state import TriageState

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- worked examples


async def load(state: TriageState, runtime: Runtime[AgentDeps]) -> dict[str, Any]:
    """Fetch the incident and the group catalogue through MCP. Deterministic: no LLM."""
    tools = runtime.context.tools  # dependencies come from the runtime, never from state
    incident = await tools.call("get_incident", {"number": state["number"]})
    groups = await tools.read_json("snow://assignment-groups")
    return {"incident": incident, "groups": groups}  # only the keys this node sets


def guard_input(state: TriageState, runtime: Runtime[AgentDeps]) -> dict[str, Any]:
    """Scan the ticket text for prompt injection before any LLM sees it.

    "flag" mode: triage continues and the reviewer gets a warning.
    "block" mode: no automatic triage at all; the ticket is left for a human.
    """
    incident = state["incident"]
    scan = scan_for_injection(
        f"{incident['short_description']}\n{incident['description']}",
        threshold=runtime.context.injection_threshold,
    )
    blocked = scan.flagged and runtime.context.injection_mode == "block"
    if scan.flagged:
        log.warning("Possible prompt injection in %s: %s", state["number"], scan.signals)
    return {"guard": {"injection": scan.to_dict(), "blocked": blocked}}


def route_after_guard(state: TriageState) -> str:
    return "summarize" if state.get("guard", {}).get("blocked") else "classify"


async def retrieve(state: TriageState, runtime: Runtime[AgentDeps]) -> dict[str, Any]:
    """Find KB sections for the ticket, through the search_kb MCP tool. Deterministic: no LLM.

    If search fails (no index, embedding API down), triage continues WITHOUT knowledge:
    the KB improves a proposal but must never block one.
    """
    incident = state["incident"]
    query = f"{incident['short_description']}\n{incident['description']}"
    try:
        found = await runtime.context.tools.call("search_kb", {"query": query})
    except ToolCallError as err:
        log.warning("KB search failed, continuing without KB: %s", err.message)
        return {"kb": []}
    # Indirect injection: retrieved text goes into a prompt too, so screen it the same way.
    hits = [h for h in found["hits"] if not scan_for_injection(h["text"]).flagged]
    if len(hits) < len(found["hits"]):
        log.warning(
            "Dropped %d KB hit(s) that looked like injected instructions",
            len(found["hits"]) - len(hits),
        )
    return {"kb": hits}


def format_kb(hits: list[dict[str, Any]]) -> str:
    """KB excerpts for the prompt, each labelled with the citation id the LLM must use."""
    if not hits:
        return "(no relevant articles found)"
    return "\n\n".join(f"[{h['id']}]\n{h['text']}" for h in hits)


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
                kb=format_kb(state.get("kb", [])),
            )
        ),
    ]
    result = await call_structured(runtime.context.llm, Proposal, messages)
    actions = []
    for action in result.value.actions:
        args = action.args.model_dump(exclude_none=True)
        args["number"] = state["number"]
        actions.append({"tool": action.tool, "args": args})

    # Keep only citations that were actually retrieved: a cited id we never showed the
    # model is a made-up source (a groundedness failure), so it never reaches the reviewer.
    retrieved = {h["id"]: h for h in state.get("kb", [])}
    citations = [
        {"id": c, "title": retrieved[c]["title"], "section": retrieved[c]["section"]}
        for c in dict.fromkeys(result.value.citations)  # de-duplicate, keep order
        if c in retrieved
    ]
    dropped = sorted(set(result.value.citations) - set(retrieved))
    if dropped:
        log.warning("Dropped citations that were not retrieved: %s", dropped)
    return {
        "proposal": {
            "actions": actions,
            "rationale": result.value.rationale,
            "citations": citations,
            "dropped_citations": dropped,  # kept for the groundedness eval
        }
    }


def check_policy(state: TriageState) -> dict[str, Any]:
    """Output guardrail: drop proposed actions that break policy, before a human sees them."""
    result = check_proposal(
        state["proposal"]["actions"],
        state["classification"],
        [g["name"] for g in state["groups"]],
    )
    for violation in result.violations:
        log.warning("Policy: %s", violation)
    return {
        "proposal": {**state["proposal"], "actions": result.actions},
        "policy": {"violations": result.violations},
    }


def _warnings(state: TriageState) -> list[str]:
    """Plain-language warnings shown to the reviewer next to the proposal."""
    warnings = []
    injection = state.get("guard", {}).get("injection", {})
    if injection.get("flagged"):
        warnings.append(
            "Ticket text looks like a prompt-injection attempt ("
            + ", ".join(injection["signals"])
            + "). Check the proposal against the actual problem."
        )
    warnings += state.get("policy", {}).get("violations", [])
    return warnings


def approve(state: TriageState) -> dict[str, Any]:
    """Pause for a human. Nothing else happens here: on resume this node re-runs from the top."""
    decision = interrupt(
        {
            "number": state["number"],
            "actions": state["proposal"]["actions"],
            "rationale": state["proposal"]["rationale"],
            "classification": state["classification"],
            "citations": state["proposal"].get("citations", []),
            "warnings": _warnings(state),
        }
    )
    validated = Decision.model_validate(decision)
    # Fingerprint what was shown, so execute can prove it runs exactly what was approved.
    approved_hash = actions_hash(state["proposal"]["actions"])
    return {"decision": {**validated.model_dump(), "actions_hash": approved_hash}}


def route(state: TriageState) -> str:
    """After approval: which node runs next."""
    approved = state.get("decision", {}).get("approved") is True
    if approved:
        return "execute"
    return "summarize"


async def execute(state: TriageState, runtime: Runtime[AgentDeps]) -> dict[str, Any]:
    """Run the approved actions in order. Stop at the first failure; never raise."""
    decision = state.get("decision", {})
    if decision.get("approved") is not True:
        return {"results": []}  # second check behind the routing: no approval, no writes
    if decision.get("actions_hash") != actions_hash(state["proposal"]["actions"]):
        log.error("Actions changed after approval for %s; refusing to execute", state["number"])
        error = "The proposal changed after it was approved; nothing was executed"
        return {"results": [{"tool": "execute", "ok": False, "error": error}]}

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
    if state.get("guard", {}).get("blocked"):
        signals = ", ".join(state["guard"]["injection"]["signals"])
        summary = (
            f"Not triaged automatically: possible prompt injection ({signals}). Needs a human."
        )
        return {"results": [], "summary": summary}

    c = state["classification"]
    decided = f"{c['category']}, impact {c['impact']}, urgency {c['urgency']}"
    if state.get("decision", {}).get("approved") and not results:
        summary = "Approved, but no action passed the policy checks; nothing changed."
    elif not state.get("decision", {}).get("approved"):
        summary = f"Rejected; nothing changed. Proposed: {decided}, group {c['assignment_group']}."
    elif all(r["ok"] for r in results):
        summary = f"Applied: {decided}; assigned to {c['assignment_group']}."
    else:
        failed = results[-1]
        summary = f"Stopped at {failed['tool']}: {failed['error']}"
    return {"results": results, "summary": summary}
