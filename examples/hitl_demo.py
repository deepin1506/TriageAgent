"""Concept check: interrupt() + a persistent checkpointer. No LLM, no API keys.

    uv run python examples/hitl_demo.py                         # start: runs until approval, exits
    uv run python examples/hitl_demo.py <thread_id> approve     # resume in a NEW process
    uv run python examples/hitl_demo.py <thread_id> reject

What to notice:
  1. The first run exits while paused. The state survives in data/demo_checkpoints.db.
  2. On resume, "[approve] node entered" prints AGAIN: the interrupted node re-runs from
     the top, and only then does interrupt() return the resume value. That's why side
     effects never go before interrupt(); they live in their own node (execute).
"""

import sys
import uuid
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

DB = Path("data/demo_checkpoints.db")


class State(TypedDict, total=False):
    ticket: str
    proposal: dict
    decision: str
    result: str


def propose(state: State) -> State:
    print("[propose] runs once")
    return {"proposal": {"tool": "assign_group", "args": {"group": "Network Operations"}}}


def approve(state: State) -> State:
    print("[approve] node entered")
    decision = interrupt({"question": "Approve this action?", "proposal": state["proposal"]})
    print(f"[approve] interrupt() returned {decision!r}")
    return {"decision": decision}


def execute(state: State) -> State:
    print("[execute] side effect happens here, exactly once")
    return {"result": f"executed {state['proposal']['tool']}"}


def summarize(state: State) -> State:
    return {"result": state.get("result", "rejected: nothing was changed")}


def route_after_approval(state: State) -> str:
    return "execute" if state["decision"] == "approve" else "summarize"


builder = StateGraph(State)
builder.add_node("propose", propose)
builder.add_node("approve", approve)
builder.add_node("execute", execute)
builder.add_node("summarize", summarize)
builder.add_edge(START, "propose")
builder.add_edge("propose", "approve")
builder.add_conditional_edges("approve", route_after_approval, ["execute", "summarize"])
builder.add_edge("execute", "summarize")
builder.add_edge("summarize", END)


def main() -> None:
    DB.parent.mkdir(exist_ok=True)
    with SqliteSaver.from_conn_string(str(DB)) as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)

        if len(sys.argv) == 1:
            thread_id = uuid.uuid4().hex[:8]
            config = {"configurable": {"thread_id": thread_id}}
            out = graph.invoke({"ticket": "VPN drops every 10 minutes"}, config)
            print("\nPAUSED. Payload for the human:", out["__interrupt__"][0].value)
            print("Waiting at node(s):", graph.get_state(config).next)
            print(f"\nResume with:  uv run python examples/hitl_demo.py {thread_id} approve")
            return

        thread_id, decision = sys.argv[1], sys.argv[2]
        config = {"configurable": {"thread_id": thread_id}}
        out = graph.invoke(Command(resume=decision), config)
        print("\nFINAL STATE:", out)


if __name__ == "__main__":
    main()
