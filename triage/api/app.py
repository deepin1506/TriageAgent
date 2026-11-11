"""HTTP API for the triage agent: what a web UI (Phase 8) or another system calls.

    uv run triage-api                         # http://127.0.0.1:8000/docs
    GET  /health                              # no key: for load balancers / Render
    POST /triage            {"number"}        # start a run (202; ?wait=true blocks)
    GET  /runs?status=pending_approval        # the approval queue
    GET  /runs/{thread_id}                    # one run: proposal, warnings, result, usage
    POST /runs/{thread_id}/decision {"approved", "reviewer", "comment"}
    GET  /metrics                             # aggregates from the run log

Every route except /health needs the X-API-Key header. Without API_KEY configured the app
refuses to start (fail closed). Runs execute in the background because an LLM run can
take a minute; the client polls GET /runs/{thread_id}.
"""

import argparse
import asyncio
import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

from triage.agent.graph import build_graph
from triage.agent.runtime import AgentDeps, connect_tools
from triage.agent.schemas import Decision
from triage.agent.service import NotPending, RunConflict, RunRegistry, TriageService
from triage.config import Settings, get_settings
from triage.llm import make_chat_model
from triage.mcp_server.models import IncidentNumber
from triage.mcp_server.server import build_server
from triage.observability import RunLog, metrics

log = logging.getLogger("triage.api")

Status = Literal["running", "pending_approval", "done", "error"]


class TriageRequest(BaseModel):
    number: IncidentNumber


def _service(request: Request) -> TriageService:
    return request.app.state.service


def create_app(
    settings: Settings | None = None, *, llm: Any = None, tools_target: Any = None
) -> FastAPI:
    """`llm` and `tools_target` are for tests; by default the configured LLM and an
    in-process MCP server (same tools and allowlist as the CLI, no subprocess)."""
    settings = settings or get_settings()
    if settings.api_key is None:
        raise RuntimeError("API_KEY is not set; the API refuses to start without one")
    expected_key = settings.api_key.get_secret_value().encode()
    chat = llm or make_chat_model(settings)  # fails fast on missing LLM config

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        Path(settings.checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
        async with (
            AsyncSqliteSaver.from_conn_string(settings.checkpoint_db) as saver,
            RunRegistry.open(settings.checkpoint_db) as registry,
            connect_tools(tools_target or build_server(settings)) as tools,
        ):
            deps = AgentDeps(
                llm=chat,
                tools=tools,
                injection_mode=settings.guard_injection_mode,
                injection_threshold=settings.guard_injection_threshold,
            )
            graph = build_graph().compile(checkpointer=saver)
            service = TriageService(graph, deps, registry, RunLog(settings.run_log), settings)
            if fixed := await service.reconcile():
                log.warning("Recovered %d run(s) left unfinished by a restart", fixed)
            app.state.service = service
            app.state.tasks = set()
            yield
            for task in app.state.tasks:  # unfinished runs are recovered by reconcile()
                task.cancel()
            await asyncio.gather(*app.state.tasks, return_exceptions=True)

    app = FastAPI(title="Triage agent API", version="0.6.0", lifespan=lifespan)
    if settings.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api_cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["X-API-Key", "Content-Type"],
        )

    header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def require_key(key: str | None = Depends(header)) -> None:
        # compare_digest: constant-time, so the key can't be guessed byte by byte from timing
        if not key or not secrets.compare_digest(key.encode(), expected_key):
            raise HTTPException(401, "Missing or wrong X-API-Key")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    api = APIRouter(dependencies=[Depends(require_key)])

    async def run_in_background(service: TriageService, thread: str, value: Any) -> None:
        try:
            await service.advance(thread, value)
        except Exception:  # already recorded as status 'error' and in the run log
            log.exception("Run %s failed", thread)

    @api.post("/triage", status_code=202)
    async def start(
        body: TriageRequest, request: Request, response: Response, wait: bool = False
    ) -> dict[str, Any]:
        service = _service(request)
        try:
            thread = await service.create(body.number)
        except RunConflict as err:
            raise HTTPException(409, {"message": str(err), "thread_id": err.thread_id}) from err
        if wait:
            response.status_code = 200
            try:
                return await service.advance(thread, {"number": body.number})
            except Exception:
                return await service.view(thread) or {}  # status 'error' with the message
        task = asyncio.create_task(run_in_background(service, thread, {"number": body.number}))
        request.app.state.tasks.add(task)  # keep a reference, or the task can be GC'd
        task.add_done_callback(request.app.state.tasks.discard)
        return await service.view(thread) or {}

    @api.get("/runs")
    async def list_runs(
        request: Request, status: Status | None = None, limit: int = Query(50, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        return await _service(request).registry.list(status=status, limit=limit)

    @api.get("/runs/{thread_id}")
    async def get_run(thread_id: str, request: Request) -> dict[str, Any]:
        view = await _service(request).view(thread_id)
        if view is None:
            raise HTTPException(404, f"No run {thread_id}")
        return view

    @api.post("/runs/{thread_id}/decision")
    async def decide(thread_id: str, body: Decision, request: Request) -> dict[str, Any]:
        service = _service(request)
        try:
            return await service.decide(thread_id, body.model_dump())
        except NotPending as err:
            raise HTTPException(404 if err.status is None else 409, str(err)) from err
        except Exception:
            return await service.view(thread_id) or {}  # status 'error' with the message

    @api.get("/metrics")
    async def get_metrics(request: Request) -> dict[str, Any]:
        records = await asyncio.to_thread(_service(request).run_log.read)
        return metrics(records)

    app.include_router(api)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="triage-api", description="Run the triage HTTP API")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    uvicorn.run("triage.api.app:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
