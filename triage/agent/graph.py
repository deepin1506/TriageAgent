"""The triage graph: wiring only. The logic lives in nodes.py.

    START -> load -> classify -> retrieve -> propose -> approve
    approve --approved--> execute -> summarize -> END
    approve --rejected--> summarize -> END

CONTRACT (the CLI, the tests and later the API rely on exactly this)
    build_graph() returns the UNcompiled builder; callers compile it with a checkpointer.
    Input        {"number": "INC0010001"}
    Interrupt    exactly one per run, before any write: {"number", "actions", "rationale", ...}
    Resume       Command(resume={"approved": bool, "reviewer": str, "comment": str | None})
    Final state  "summary": str, "results": [{"tool", "ok", "error"}] ([] when rejected)
"""

from langgraph.graph import END, START, StateGraph

from . import nodes
from .runtime import AgentDeps
from .state import TriageState


def build_graph() -> StateGraph:
    graph = StateGraph(TriageState, context_schema=AgentDeps)
    graph.add_node("load", nodes.load)
    graph.add_node("classify", nodes.classify)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("propose", nodes.propose)
    graph.add_node("approve", nodes.approve)
    graph.add_node("execute", nodes.execute)
    graph.add_node("summarize", nodes.summarize)

    graph.add_edge(START, "load")
    graph.add_edge("load", "classify")
    graph.add_edge("classify", "retrieve")
    graph.add_edge("retrieve", "propose")
    graph.add_edge("propose", "approve")
    graph.add_conditional_edges("approve", nodes.route, ["execute", "summarize"])
    graph.add_edge("execute", "summarize")
    graph.add_edge("summarize", END)
    return graph
