"""MCP server bootstrap.

    uv run python -m triage.mcp_server        # stdio: what an MCP host launches

The TOOLS list below is the server-side allowlist: nothing else is exposed.
With stdio, stdout carries the protocol. Never print(); log to stderr.
"""

import logging
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError
from mcp_types import ToolAnnotations

from triage.config import Settings, get_settings
from triage.snow_client import SnowClient

from . import tools
from .models import GroupView

INSTRUCTIONS = (
    "Tools for triaging ServiceNow incidents. Refer to incidents by number (e.g. INC0010001). "
    "Set impact and urgency; priority is derived from them. Valid assignment group names are "
    "in the resource snow://assignment-groups."
)

# Annotations are hints for hosts (e.g. when to ask for confirmation). They are self-declared
# by the server, so a client must not treat them as a security control.
TOOLS: list[tuple[Callable[..., Any], ToolAnnotations]] = [
    (
        tools.get_incident,
        ToolAnnotations(title="Get incident", read_only_hint=True, open_world_hint=False),
    ),
    (
        tools.create_incident,
        ToolAnnotations(
            title="Create incident",
            read_only_hint=False,
            destructive_hint=False,  # only adds
            idempotent_hint=True,  # same correlation_id -> same incident
            open_world_hint=False,
        ),
    ),
    (
        tools.update_incident,
        ToolAnnotations(
            title="Update incident",
            read_only_hint=False,
            destructive_hint=True,  # overwrites fields
            idempotent_hint=False,  # work notes append
            open_world_hint=False,
        ),
    ),
    (
        tools.assign_group,
        ToolAnnotations(
            title="Assign group",
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    ),
]


def make_snow_client(settings: Settings) -> SnowClient:
    token = settings.snow_token.get_secret_value() if settings.snow_token else None
    return SnowClient(
        settings.snow_base_url,
        token=token,
        username=None if token else settings.snow_username,
        password=None if token else settings.snow_password.get_secret_value(),
        timeout=settings.snow_timeout_s,
        max_retries=settings.snow_max_retries,
        backoff_base=settings.snow_backoff_base_s,
    )


def build_server(settings: Settings | None = None) -> MCPServer:
    settings = settings or get_settings()
    app = tools.AppContext()

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[tools.AppContext]:
        app.snow = make_snow_client(settings)  # one connection pool per server run
        try:
            yield app  # tools reach it via ctx.request_context.lifespan_context
        finally:
            await app.snow.aclose()
            app.snow = None

    server = MCPServer("triage-snow", version="0.1.0", instructions=INSTRUCTIONS, lifespan=lifespan)
    for fn, annotations in TOOLS:
        server.add_tool(fn, annotations=annotations, structured_output=True)

    @server.resource(
        "snow://assignment-groups",
        name="assignment-groups",
        description="Active assignment groups (valid values for assign_group)",
        mime_type="application/json",
    )
    async def _assignment_groups() -> list[GroupView]:
        if app.snow is None:
            raise ResourceError("Server is not running")
        return await tools.assignment_groups(app.snow)

    return server


def main() -> None:
    level = os.environ.get("MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("mcp").setLevel(level)
    build_server().run("stdio")
