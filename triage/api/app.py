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

Demo mode (DEMO_MODE=true, the public Render deploy): the app starts its own private mock
ServiceNow, visitors can call everything without a key, and two extra routes let them
create and list tickets. Visitor LLM spend is capped per day; requests with the key skip it.
"""

import argparse
import asyncio
import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import APIKeyHeader
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, Field, SecretStr

from mock_snow.thread import private_mock
from triage.agent.graph import build_graph
from triage.agent.runtime import AgentDeps, connect_tools
from triage.agent.schemas import Decision
from triage.agent.service import NotPending, RunConflict, RunRegistry, TriageService
from triage.config import Settings, get_settings
from triage.kb.build import ensure_index
from triage.llm import make_chat_model
from triage.mcp_server.models import IncidentNumber
from triage.mcp_server.server import build_server, make_snow_client
from triage.observability import RunLog, metrics

log = logging.getLogger("triage.api")

Status = Literal["running", "pending_approval", "done", "error"]
INCIDENT_FIELDS = [
    "number",
    "short_description",
    "category",
    "priority",
    "assignment_group",
    "state",
    "sys_created_on",
]


class TriageRequest(BaseModel):
    number: IncidentNumber


class NewIncident(BaseModel):
    short_description: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=3, max_length=2000)


def _service(request: Request) -> TriageService:
    return request.app.state.service


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


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
        async with AsyncExitStack() as stack:
            agent_settings = settings
            if settings.demo_mode:
                # A private ServiceNow per container: visitors can break nothing real, and
                # every restart (Render's free tier sleeps when idle) starts from the seed.
                url, user, password = stack.enter_context(private_mock(settings.snow_username))
                agent_settings = settings.model_copy(
                    update={
                        "snow_base_url": url,
                        "snow_username": user,
                        "snow_password": SecretStr(password),
                        "snow_token": None,
                    }
                )
                await asyncio.to_thread(ensure_index, agent_settings)  # empty disk on boot
                snow = make_snow_client(agent_settings)
                stack.push_async_callback(snow.aclose)
                app.state.snow = snow
                app.state.new_incidents = {}  # {"YYYY-MM-DD": count}

            Path(settings.checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
            saver = await stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(settings.checkpoint_db)
            )
            registry = await stack.enter_async_context(RunRegistry.open(settings.checkpoint_db))
            tools = await stack.enter_async_context(
                connect_tools(tools_target or build_server(agent_settings))
            )
            deps = AgentDeps(
                llm=chat,
                tools=tools,
                injection_mode=settings.guard_injection_mode,
                injection_threshold=settings.guard_injection_threshold,
            )
            graph = build_graph().compile(checkpointer=saver)
            run_log = RunLog(settings.run_log)
            service = TriageService(graph, deps, registry, run_log, agent_settings)
            if fixed := await service.reconcile():
                log.warning("Recovered %d run(s) left unfinished by a restart", fixed)
            app.state.service = service
            app.state.tasks = set()
            yield
            for task in app.state.tasks:  # unfinished runs are recovered by reconcile()
                task.cancel()
            await asyncio.gather(*app.state.tasks, return_exceptions=True)

    app = FastAPI(title="Triage agent API", version="0.7.0", lifespan=lifespan)
    if settings.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api_cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["X-API-Key", "Content-Type"],
        )

    header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def caller(key: str | None = Depends(header)) -> bool:
        """True for the owner (valid key). In demo mode, no key = a visitor (False)."""
        # compare_digest: constant-time, so the key can't be guessed byte by byte from timing
        if key and secrets.compare_digest(key.encode(), expected_key):
            return True
        if settings.demo_mode and not key:
            return False
        raise HTTPException(401, "Missing or wrong X-API-Key")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "demo": settings.demo_mode}

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    api = APIRouter(dependencies=[Depends(caller)])

    async def run_in_background(service: TriageService, thread: str, value: Any) -> None:
        try:
            await service.advance(thread, value)
        except Exception:  # already recorded as status 'error' and in the run log
            log.exception("Run %s failed", thread)

    async def check_demo_caps(service: TriageService) -> None:
        """Visitors spend the owner's LLM quota, so cap it: per day, and in flight at once."""
        today = await service.registry.count(since=_today())
        if today >= settings.demo_max_runs_per_day:
            raise HTTPException(429, "The demo's daily run limit is used up; try tomorrow.")
        if await service.registry.count(status="running") >= settings.demo_max_active_runs:
            raise HTTPException(429, "The demo is busy; try again in a minute.")

    @api.post("/triage", status_code=202)
    async def start(
        body: TriageRequest,
        request: Request,
        response: Response,
        wait: bool = False,
        owner: bool = Depends(caller),
    ) -> dict[str, Any]:
        service = _service(request)
        if settings.demo_mode and not owner:
            await check_demo_caps(service)
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

    if settings.demo_mode:
        # Demo only: the agent itself still can't create incidents (least privilege). These
        # routes use the ServiceNow client directly, against the container's private mock.

        @api.post("/demo/incidents", status_code=201)
        async def create_incident(
            body: NewIncident, request: Request, owner: bool = Depends(caller)
        ) -> dict[str, str]:
            counts = request.app.state.new_incidents
            if not owner and counts.get(_today(), 0) >= settings.demo_max_incidents_per_day:
                raise HTTPException(429, "The demo's daily ticket limit is used up.")
            created = await request.app.state.snow.create_record("incident", body.model_dump())
            counts[_today()] = counts.get(_today(), 0) + 1
            return {"number": created["number"], "short_description": body.short_description}

        @api.get("/demo/incidents")
        async def list_incidents(request: Request) -> list[dict[str, Any]]:
            return await request.app.state.snow.query(
                "incident",
                "active=true^ORDERBYDESCnumber",
                limit=50,
                fields=INCIDENT_FIELDS,
                display_value="true",
            )

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
