"""Run the real agent on every labelled ticket, end to end, with automatic approval.

Self-contained: it starts its own mock ServiceNow (fresh database) in a background thread,
creates each ticket through the client, runs the graph exactly as the CLI does, approves
the proposal as "eval-bot", then reads the ticket back to check what was really written.
The only thing that differs from production is who clicks "approve".
"""

import asyncio
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import SecretStr

from mock_snow.settings import MockSettings
from mock_snow.thread import running_mock
from triage.agent.graph import build_graph
from triage.agent.runtime import AgentDeps, connect_tools
from triage.config import Settings
from triage.mcp_server.server import build_server, make_snow_client

from .dataset import EvalCase

EVAL_USER = "eval.runner"
APPROVE = {"approved": True, "reviewer": "eval-bot", "comment": None}


@dataclass
class CaseResult:
    case_id: str
    number: str | None = None
    error: str | None = None  # set when the run crashed
    classification: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)  # after the policy check
    citations: list[str] = field(default_factory=list)
    dropped_citations: list[str] = field(default_factory=list)
    retrieved_articles: list[str] = field(default_factory=list)
    injection_flagged: bool = False
    violations: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    final: dict[str, Any] | None = None  # the ticket as ServiceNow has it afterwards
    latency_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _from_state(result: CaseResult, state: dict[str, Any]) -> None:
    proposal = state.get("proposal", {})
    result.classification = state.get("classification")
    result.actions = proposal.get("actions", [])
    result.citations = [c["id"] for c in proposal.get("citations", [])]
    result.dropped_citations = proposal.get("dropped_citations", [])
    result.retrieved_articles = list(dict.fromkeys(h["article_id"] for h in state.get("kb", [])))
    result.injection_flagged = bool(state.get("guard", {}).get("injection", {}).get("flagged"))
    result.violations = state.get("policy", {}).get("violations", [])
    result.results = state.get("results", [])


async def run_eval(
    cases: list[EvalCase],
    llm_for: Callable[[EvalCase], Any],
    settings: Settings,
    *,
    sleep_s: float = 0.0,
    progress: Callable[[str], None] = lambda _: None,
) -> list[CaseResult]:
    """`llm_for(case)` returns the chat model for that case (the same real model for every
    case in a real run; a per-case scripted model in tests)."""
    password = secrets.token_urlsafe(16)
    mock_settings = MockSettings(
        _env_file=None,
        snow_username=EVAL_USER,
        snow_password=password,
        snow_token=None,
        snow_mock_db=str(Path(tempfile.mkdtemp(prefix="eval-snow-")) / "snow.db"),
        snow_mock_admin=False,
        snow_mock_fault_rate=0.0,
    )
    results: list[CaseResult] = []
    with running_mock(mock_settings) as url:
        agent_settings = settings.model_copy(
            update={
                "snow_base_url": url,
                "snow_username": EVAL_USER,
                "snow_password": SecretStr(password),  # model_copy doesn't re-validate
                "snow_token": None,
            }
        )
        graph = build_graph().compile(checkpointer=InMemorySaver())
        snow = make_snow_client(agent_settings)
        try:
            async with connect_tools(build_server(agent_settings)) as tools:
                for i, case in enumerate(cases, start=1):
                    result = CaseResult(case.id)
                    start = time.perf_counter()
                    try:
                        created = await snow.create_incident_idempotent(
                            {
                                "short_description": case.short_description,
                                "description": case.description,
                            },
                            correlation_id=f"eval-{case.id}",
                        )
                        result.number = created["number"]
                        deps = AgentDeps(
                            llm=llm_for(case),
                            tools=tools,
                            injection_mode="flag",  # measure the agent, don't skip it
                            injection_threshold=settings.guard_injection_threshold,
                        )
                        config = {"configurable": {"thread_id": f"eval-{case.id}"}}
                        state = await graph.ainvoke({"number": result.number}, config, context=deps)
                        if "__interrupt__" in state:
                            state = await graph.ainvoke(
                                Command(resume=APPROVE), config, context=deps
                            )
                        _from_state(result, state)
                        result.final = await tools.call("get_incident", {"number": result.number})
                    except Exception as err:  # one bad case must not stop the whole run
                        result.error = f"{type(err).__name__}: {err}"
                    result.latency_s = round(time.perf_counter() - start, 3)
                    results.append(result)
                    status = "ERROR" if result.error else "ok"
                    progress(f"[{i}/{len(cases)}] {case.id} {status} ({result.latency_s:.1f}s)")
                    if sleep_s and i < len(cases):
                        await asyncio.sleep(sleep_s)  # stay under free-tier rate limits
        finally:
            await snow.aclose()
    return results
