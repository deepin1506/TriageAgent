"""The triage graph: wiring only. The logic lives in nodes.py.

    START -> load -> guard_input --ok--> classify -> retrieve -> propose -> check_policy
                     guard_input --blocked (injection, block mode)--> summarize -> END
    check_policy -> approve --approved--> execute -> summarize -> END
                    approve --rejected--> summarize -> END

CONTRACT (the CLI, the tests and later the API rely on exactly this)
    build_graph() returns the UNcompiled builder; callers compile it with a checkpointer.
    Input        {"number": "INC0010001"}
    Interrupt    at most one per run, before any write: {"number", "actions", "rationale",
                 "classification", "citations", "warnings"}. None when the ticket is blocked.
    Resume       Command(resume={"approved": bool, "reviewer": str, "comment": str | None})
    Final state  "summary": str, "results": [{"tool", "ok", "error"}] ([] when rejected),
                 "trace": one timing/usage event per node run (tracing.py)
"""

from langgraph.graph import END, START, StateGraph

from . import nodes
from .runtime import AgentDeps
from .state import TriageState
from .tracing import traced


def build_graph() -> StateGraph:
    graph = StateGraph(TriageState, context_schema=AgentDeps)
    graph.add_node("load", traced("load", nodes.load))
    graph.add_node("guard_input", traced("guard_input", nodes.guard_input))
    graph.add_node("classify", traced("classify", nodes.classify))
    graph.add_node("retrieve", traced("retrieve", nodes.retrieve))
    graph.add_node("propose", traced("propose", nodes.propose))
    graph.add_node("check_policy", traced("check_policy", nodes.check_policy))
    graph.add_node("approve", traced("approve", nodes.approve))
    graph.add_node("execute", traced("execute", nodes.execute))
    graph.add_node("summarize", traced("summarize", nodes.summarize))

    graph.add_edge(START, "load")
    graph.add_edge("load", "guard_input")
    graph.add_conditional_edges("guard_input", nodes.route_after_guard, ["classify", "summarize"])
    graph.add_edge("classify", "retrieve")
    graph.add_edge("retrieve", "propose")
    graph.add_edge("propose", "check_policy")
    graph.add_edge("check_policy", "approve")
    graph.add_conditional_edges("approve", nodes.route, ["execute", "summarize"])
    graph.add_edge("execute", "summarize")
    graph.add_edge("summarize", END)
    return graph
