"""FastAPI app: /api/now/table/{table}[/{sys_id}] plus /_mock admin endpoints.

Run:  uv run mock-snow            (or: uv run uvicorn mock_snow.app:create_app --factory)
Docs: http://127.0.0.1:8080/docs
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from . import db, service
from .auth import current_user
from .errors import install_error_handlers
from .faults import FaultInjector, FaultSpec, install_fault_middleware
from .seed import seed
from .serialize import DisplayValue, Renderer, parse_fields
from .settings import MockSettings


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """One connection per request. Write handlers commit explicitly; anything
    uncommitted (e.g. after a validation error) is discarded on close."""
    conn = db.connect(request.app.state.settings.snow_mock_db)
    try:
        yield conn
    finally:
        conn.close()


User = Annotated[str, Depends(current_user)]
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
DisplayParam = Annotated[DisplayValue, Query()]
Payload = Annotated[dict[str, Any], Body()]

table_api = APIRouter(prefix="/api/now/table", tags=["Table API"])


def _renderer(request: Request, conn: sqlite3.Connection, dv: str, exclude: bool) -> Renderer:
    return Renderer(conn, str(request.base_url), dv, exclude)  # type: ignore[arg-type]


@table_api.get("/{table}")
def list_records(
    table: str,
    request: Request,
    response: Response,
    user: User,
    conn: Conn,
    sysparm_query: str = "",
    sysparm_limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    sysparm_offset: Annotated[int, Query(ge=0)] = 0,
    sysparm_fields: str = "",
    sysparm_display_value: DisplayParam = "false",
    sysparm_exclude_reference_link: bool = False,
) -> dict:
    t = service.get_table(table)
    fields = parse_fields(t, sysparm_fields)
    rows, total = service.list_records(conn, t, sysparm_query, sysparm_limit, sysparm_offset)
    r = _renderer(request, conn, sysparm_display_value, sysparm_exclude_reference_link)
    response.headers["X-Total-Count"] = str(total)
    return {"result": [r.record(t, row, fields) for row in rows]}


@table_api.get("/{table}/{sys_id}")
def get_record(
    table: str,
    sys_id: str,
    request: Request,
    user: User,
    conn: Conn,
    sysparm_fields: str = "",
    sysparm_display_value: DisplayParam = "false",
    sysparm_exclude_reference_link: bool = False,
) -> dict:
    t = service.get_table(table)
    fields = parse_fields(t, sysparm_fields)
    row = service.get(conn, t, sys_id)
    r = _renderer(request, conn, sysparm_display_value, sysparm_exclude_reference_link)
    return {"result": r.record(t, row, fields)}


@table_api.post("/{table}", status_code=201)
def create_record(
    table: str,
    payload: Payload,
    request: Request,
    user: User,
    conn: Conn,
    sysparm_fields: str = "",
    sysparm_display_value: DisplayParam = "false",
    sysparm_exclude_reference_link: bool = False,
) -> JSONResponse:
    t = service.get_table(table)
    fields = parse_fields(t, sysparm_fields)
    row = service.create(conn, t, payload, user)
    conn.commit()
    r = _renderer(request, conn, sysparm_display_value, sysparm_exclude_reference_link)
    location = f"{str(request.base_url).rstrip('/')}/api/now/table/{t.name}/{row['sys_id']}"
    return JSONResponse({"result": r.record(t, row, fields)}, 201, {"Location": location})


@table_api.api_route("/{table}/{sys_id}", methods=["PATCH", "PUT"])
def update_record(
    table: str,
    sys_id: str,
    payload: Payload,
    request: Request,
    user: User,
    conn: Conn,
    sysparm_fields: str = "",
    sysparm_display_value: DisplayParam = "false",
    sysparm_exclude_reference_link: bool = False,
) -> dict:
    """PUT and PATCH both do a partial update in the Table API."""
    t = service.get_table(table)
    fields = parse_fields(t, sysparm_fields)
    row = service.update(conn, t, sys_id, payload, user)
    conn.commit()
    r = _renderer(request, conn, sysparm_display_value, sysparm_exclude_reference_link)
    return {"result": r.record(t, row, fields)}


@table_api.delete("/{table}/{sys_id}", status_code=204)
def delete_record(table: str, sys_id: str, user: User, conn: Conn) -> Response:
    service.delete(conn, service.get_table(table), sys_id)
    conn.commit()
    return Response(status_code=204)


admin = APIRouter(prefix="/_mock", tags=["Mock admin (not part of ServiceNow)"])


@admin.post("/faults", status_code=201)
def add_fault(spec: FaultSpec, request: Request) -> dict:
    request.app.state.faults.add(spec)
    return {"queued": spec.model_dump()}


@admin.delete("/faults", status_code=204)
def clear_faults(request: Request) -> Response:
    request.app.state.faults.clear()
    return Response(status_code=204)


@admin.get("/requests")
def request_log(request: Request) -> list[dict]:
    return list(request.app.state.faults.request_log)


@admin.post("/reset")
def reset(request: Request) -> dict:
    """Drop and re-seed the DB, clear queued faults and the request log."""
    _init_db(request.app.state.settings, fresh=True)
    request.app.state.faults.clear()
    request.app.state.faults.request_log.clear()
    return {"status": "reset"}


def _init_db(settings: MockSettings, fresh: bool = False) -> None:
    with closing(db.connect(settings.snow_mock_db)) as conn:
        if fresh:
            db.drop_schema(conn)
        db.create_schema(conn)
        if db.is_empty(conn):
            seed(conn)
        conn.commit()


def create_app(settings: MockSettings | None = None) -> FastAPI:
    settings = settings or MockSettings()
    app = FastAPI(
        title="Mock ServiceNow Table API",
        version="0.1.0",
        description="A small, strict subset of the ServiceNow Table API for local development.",
    )
    app.state.settings = settings
    app.state.faults = FaultInjector(random_rate=settings.snow_mock_fault_rate)
    _init_db(settings)

    install_error_handlers(app)
    install_fault_middleware(app)
    app.include_router(table_api)
    if settings.snow_mock_admin:
        app.include_router(admin)

    @app.get("/health", tags=["Health"])
    def health() -> dict:
        return {"status": "ok"}

    return app
