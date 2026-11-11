"""The graph's state: plain, JSON-friendly facts that get checkpointed after every node.

Values are dicts/lists/strings (not Pydantic objects) so checkpoints stay readable in
`triage show` and survive code changes. Clients (LLM, MCP) never go here; they are
runtime context (see runtime.py).
"""

import operator
from typing import Annotated, Any, TypedDict


class TriageState(TypedDict, total=False):  # total=False: keys appear as nodes fill them
    number: str  # input, e.g. "INC0010001"
    incident: dict[str, Any]  # load: IncidentView as a dict
    guard: dict[str, Any]  # guard_input: {"injection": {score, signals, flagged}, "blocked"}
    groups: list[dict[str, str]]  # load: [{"name", "description"}] from snow://assignment-groups
    classification: dict[str, Any]  # classify: Classification as a dict
    kb: list[dict[str, Any]]  # retrieve: KB chunks with citations (Phase 3)
    proposal: dict[str, Any]  # propose: {"actions": [{"tool", "args"}], "rationale", "citations"}
    policy: dict[str, Any]  # check_policy: {"violations": [str]}
    decision: dict[str, Any]  # approve: {"approved", "reviewer", "comment"}
    results: list[dict[str, Any]]  # execute: [{"tool", "ok", "error"}]
    summary: str  # summarize
    # Every node appends one timing/usage event (tracing.py); the reducer concatenates.
    trace: Annotated[list[dict[str, Any]], operator.add]
