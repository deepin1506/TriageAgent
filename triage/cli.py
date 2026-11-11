"""Command line for the triage agent.

    uv run triage run INC0010001              # triage; pauses for your approval
    uv run triage run INC0010001 --no-prompt  # stop at the approval point and exit
    uv run triage resume <thread> approve [--comment "..."]
    uv run triage resume <thread> reject  [--comment "..."]
    uv run triage show <thread>               # checkpointed state of a run

Needs the mock API running (uv run mock-snow). The MCP server is launched over stdio.
Every finished run is appended to the run log (RUN_LOG, default runs/runs.jsonl).
"""

import argparse
import asyncio
import getpass
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from triage.agent.graph import build_graph
from triage.agent.runtime import AgentDeps, connect_tools
from triage.agent.service import NotPending, RunConflict, RunRegistry, TriageService
from triage.config import get_settings
from triage.llm import LLMConfigError, make_chat_model
from triage.observability import RunLog


def _print_json(label: str, value: Any) -> None:
    print(f"\n{label}\n{json.dumps(value, indent=2, default=str)}")


@asynccontextmanager
async def _session() -> AsyncIterator[TriageService]:
    settings = get_settings()
    llm = make_chat_model(settings)  # fails fast on missing config
    Path(settings.checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
    async with (
        AsyncSqliteSaver.from_conn_string(settings.checkpoint_db) as saver,
        RunRegistry.open(settings.checkpoint_db) as registry,
        connect_tools() as tools,
    ):
        deps = AgentDeps(
            llm=llm,
            tools=tools,
            injection_mode=settings.guard_injection_mode,
            injection_threshold=settings.guard_injection_threshold,
        )
        graph = build_graph().compile(checkpointer=saver)
        yield TriageService(graph, deps, registry, RunLog(settings.run_log), settings)


def _ask_decision() -> dict[str, Any]:
    answer = input("\nApprove these actions? [y/N] ").strip().lower()
    comment = input("Comment (optional): ").strip() or None
    return {"approved": answer in ("y", "yes"), "reviewer": getpass.getuser(), "comment": comment}


def _print_usage(view: dict[str, Any]) -> None:
    u = view["usage"]
    cost = "n/a" if u["cost_usd"] is None else f"${u['cost_usd']:.6f}"
    print(
        f"\nAgent time {u['agent_ms'] / 1000:.1f}s, {u['llm_calls']} LLM call(s), "
        f"{u['input_tokens']} in / {u['output_tokens']} out tokens, cost {cost}"
    )


async def _drive(service: TriageService, view: dict[str, Any], prompt: bool) -> None:
    thread = view["thread_id"]
    while view["status"] == "pending_approval":
        payload = view["pending"]
        _print_json(f"Waiting for approval (thread {thread}):", payload)
        for warning in payload.get("warnings", []):
            print(f"\n!! WARNING: {warning}")
        if not prompt:
            print(f"\nResume with:  uv run triage resume {thread} approve|reject")
            return
        view = await service.decide(thread, _ask_decision())
    _print_json("Results:", view.get("results") or [])
    print(f"\nSummary: {view.get('summary') or '(none)'}")
    _print_usage(view)


async def _run(args: argparse.Namespace) -> str | None:
    async with _session() as service:
        try:
            view = await service.start(args.number, args.thread)
        except RunConflict as err:
            return f"{err}. Resume it, or wait for it to finish."
        await _drive(service, view, prompt=not args.no_prompt)
    return None


async def _resume(args: argparse.Namespace) -> str | None:
    decision = {
        "approved": args.decision == "approve",
        "reviewer": args.reviewer or getpass.getuser(),
        "comment": args.comment,
    }
    async with _session() as service:
        try:
            view = await service.decide(args.thread, decision)
        except NotPending as err:
            return str(err)
        await _drive(service, view, prompt=True)
    return None


async def _show(args: argparse.Namespace) -> str | None:
    async with _session() as service:
        snapshot = await service.graph.aget_state({"configurable": {"thread_id": args.thread}})
        if not snapshot.values:
            return f"No checkpoints for thread {args.thread}."
        _print_json("State:", snapshot.values)
        print(f"\nNext: {snapshot.next or '(finished)'}")
        if snapshot.interrupts:
            _print_json("Pending approval:", snapshot.interrupts[0].value)
    return None


def _first_leaf(err: BaseException) -> BaseException:
    while isinstance(err, BaseExceptionGroup):
        err = err.exceptions[0]
    return err


def main() -> None:
    parser = argparse.ArgumentParser(prog="triage", description="IT ticket triage agent")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="triage an incident")
    run.add_argument("number", help="e.g. INC0010001")
    run.add_argument("--thread", help="thread id (default: <number>-<random>)")
    run.add_argument("--no-prompt", action="store_true", help="exit at the approval point")
    run.set_defaults(handler=_run)

    resume = sub.add_parser("resume", help="approve or reject a paused run")
    resume.add_argument("thread")
    resume.add_argument("decision", choices=["approve", "reject"])
    resume.add_argument("--comment")
    resume.add_argument("--reviewer")
    resume.set_defaults(handler=_resume)

    show = sub.add_parser("show", help="show a run's checkpointed state")
    show.add_argument("thread")
    show.set_defaults(handler=_show)

    args = parser.parse_args()
    try:
        message = asyncio.run(args.handler(args))
    except LLMConfigError as err:
        sys.exit(f"Config error: {err}")
    except KeyboardInterrupt:
        sys.exit(130)
    except BaseExceptionGroup as group:  # anyio task groups (MCP client) wrap errors
        if os.environ.get("TRIAGE_DEBUG"):
            raise
        leaf = _first_leaf(group)
        sys.exit(f"Error: {type(leaf).__name__}: {leaf}  (set TRIAGE_DEBUG=1 for the traceback)")
    if message:
        sys.exit(message)


if __name__ == "__main__":
    main()
