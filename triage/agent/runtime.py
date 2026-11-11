"""Runtime dependencies for the graph: LangGraph's `context_schema`.

These are passed per invocation (`graph.ainvoke(..., context=AgentDeps(...))`) and are
never checkpointed. State holds facts; this holds clients.
"""

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from mcp import Client
from mcp.client.stdio import StdioServerParameters

# Client-side allowlist: what THIS agent may call, a subset of what the server exposes.
# The server also has create_incident, which a triage agent doesn't need (least privilege).
AGENT_TOOLS = frozenset({"get_incident", "update_incident", "assign_group"})
WRITE_TOOLS = frozenset({"update_incident", "assign_group"})


class ToolCallError(RuntimeError):
    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message


class ToolGateway:
    """The only way the agent reaches MCP. Enforces the allowlist and unwraps results."""

    def __init__(self, client: Client, allowed: frozenset[str] = AGENT_TOOLS) -> None:
        self._client = client
        self.allowed = allowed

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call an allowed tool. Returns structured content; raises ToolCallError on failure."""
        if tool not in self.allowed:
            raise ToolCallError(tool, "not in the agent's tool allowlist")
        result = await self._client.call_tool(tool, args)
        if result.is_error:
            text = "\n".join(getattr(c, "text", "") for c in result.content)
            raise ToolCallError(tool, text or "tool returned an error")
        return result.structured_content or {}

    async def read_json(self, uri: str) -> Any:
        result = await self._client.read_resource(uri)
        return json.loads(result.contents[0].text)

    async def input_schemas(self) -> dict[str, dict[str, Any]]:
        """JSON Schemas of the allowed tools (for the propose prompt / validation)."""
        listed = await self._client.list_tools()
        return {t.name: t.input_schema for t in listed.tools if t.name in self.allowed}


@dataclass
class AgentDeps:
    llm: BaseChatModel
    tools: ToolGateway


def stdio_server() -> StdioServerParameters:
    """Launch our MCP server as a subprocess, the way any MCP host would."""
    passthrough = {k: v for k, v in os.environ.items() if k.startswith("SNOW_")}
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "triage.mcp_server"],
        env={**passthrough, "MCP_LOG_LEVEL": os.environ.get("MCP_LOG_LEVEL", "WARNING")},
    )


@asynccontextmanager
async def connect_tools(
    target: Any = None, allowed: frozenset[str] = AGENT_TOOLS
) -> AsyncIterator[ToolGateway]:
    """Open an MCP connection. `target`: StdioServerParameters (default), a URL, or an
    in-process MCPServer (tests)."""
    async with Client(target or stdio_server()) as client:
        yield ToolGateway(client, allowed)
