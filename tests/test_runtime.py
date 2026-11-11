"""ToolGateway: the agent's only path to MCP. Passes before your Phase 0/1 code is done."""

from collections.abc import AsyncIterator

import httpx
import pytest

from tests.conftest import settings_for
from triage.agent.runtime import AGENT_TOOLS, ToolCallError, ToolGateway, connect_tools
from triage.mcp_server.server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def tools(live_server: str, mock_admin: httpx.Client) -> AsyncIterator[ToolGateway]:
    async with connect_tools(build_server(settings_for(live_server))) as gateway:
        yield gateway


async def test_tools_outside_the_allowlist_are_refused_locally(
    tools: ToolGateway, mock_admin: httpx.Client
) -> None:
    # create_incident exists on the server, but this agent has no business calling it.
    with pytest.raises(ToolCallError, match="allowlist"):
        await tools.call(
            "create_incident",
            {"short_description": "x" * 10, "description": "", "correlation_id": "c"},
        )
    assert mock_admin.get("/_mock/requests").json() == []


async def test_only_allowed_tool_schemas_are_exposed_to_the_agent(tools: ToolGateway) -> None:
    schemas = await tools.input_schemas()
    assert set(schemas) == AGENT_TOOLS
    assert "priority" not in schemas["update_incident"]["properties"]


async def test_tool_errors_become_tool_call_errors(tools: ToolGateway) -> None:
    with pytest.raises(ToolCallError) as exc:  # schema validation fails before any handler
        await tools.call("get_incident", {"number": "not-a-number"})
    assert exc.value.tool == "get_incident"
