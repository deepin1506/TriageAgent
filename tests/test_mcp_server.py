"""Spec for Phase 1 (triage/mcp_server/tools.py).

Tests talk to the server through a real MCP client: in-process for speed, plus one
end-to-end run over stdio. The first block (contract) passes before you write anything;
the rest need the Phase 0 client and your handlers.
"""

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import CallToolResult

from mock_snow.schema import CATEGORY, STATE
from mock_snow.seed import INCIDENT_IDS
from tests.conftest import PASSWORD, USER, settings_for
from triage.mcp_server.server import build_server

pytestmark = pytest.mark.anyio

TOOL_NAMES = {"get_incident", "create_incident", "update_incident", "assign_group"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def mcp(live_server: str, mock_admin: httpx.Client) -> AsyncIterator[Client]:
    async with Client(build_server(settings_for(live_server))) as client:
        yield client


def text(result: CallToolResult) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


def attempts(admin: httpx.Client, method: str) -> int:
    return sum(1 for r in admin.get("/_mock/requests").json() if r["method"] == method)


def work_notes(base_url: str, number: str) -> list[str]:
    resp = httpx.get(
        f"{base_url}/api/now/table/sys_journal_field",
        params={"sysparm_query": f"element_id={INCIDENT_IDS[number]}^element=work_notes"},
        auth=(USER, PASSWORD),
    )
    return [row["value"] for row in resp.json()["result"]]


# --- contract: passes before any handler is written --------------------------------------


async def test_exposes_exactly_the_allowlisted_tools(mcp: Client) -> None:
    tools = (await mcp.list_tools()).tools
    assert {t.name for t in tools} == TOOL_NAMES
    assert all(t.description and t.output_schema for t in tools)


async def test_update_schema_uses_enums_and_has_no_priority(mcp: Client) -> None:
    tools = {t.name: t for t in (await mcp.list_tools()).tools}
    props = tools["update_incident"].input_schema["properties"]
    assert "priority" not in props

    def enum(prop: str) -> set[str]:
        return {v for option in props[prop]["anyOf"] for v in option.get("enum", [])}

    assert enum("category") == set(CATEGORY) - {""}  # stays in sync with the mock
    assert enum("state") == set(STATE)
    assert enum("impact") == enum("urgency") == {"1", "2", "3"}
    assert tools["update_incident"].input_schema["required"] == ["number"]


async def test_annotations(mcp: Client) -> None:
    ann = {t.name: t.annotations for t in (await mcp.list_tools()).tools}
    assert ann["get_incident"].read_only_hint is True
    assert ann["create_incident"].idempotent_hint is True
    assert ann["update_incident"].destructive_hint is True
    assert ann["assign_group"].destructive_hint is True


@pytest.mark.parametrize(
    "tool,args",
    [
        ("get_incident", {"number": "12345"}),
        ("update_incident", {"number": "INC0010001", "category": "printers"}),
        ("update_incident", {"number": "INC0010001", "priority": "1"}),
        ("create_incident", {"short_description": "x", "description": "", "correlation_id": "c"}),
        (
            "create_incident",
            {
                "short_description": "Printer jam",
                "description": "",
                "correlation_id": "c",
                "caller_user_name": "x^ORDERBYnumber",
            },
        ),
    ],
)
async def test_bad_arguments_are_rejected_before_any_handler_runs(
    mcp: Client, mock_admin: httpx.Client, tool: str, args: dict
) -> None:
    result = await mcp.call_tool(tool, args)
    assert result.is_error
    assert mock_admin.get("/_mock/requests").json() == []  # ServiceNow never called


# --- behaviour: needs your handlers -------------------------------------------------------


async def test_assignment_groups_resource(mcp: Client) -> None:
    result = await mcp.read_resource("snow://assignment-groups")
    groups = json.loads(result.contents[0].text)
    assert len(groups) == 7
    assert {
        "name": "Network Operations",
        "description": "VPN, Wi-Fi, LAN, DNS, firewall, proxy",
    } in groups


async def test_get_incident_returns_a_structured_view(mcp: Client) -> None:
    result = await mcp.call_tool("get_incident", {"number": "INC0010001"})
    assert not result.is_error, text(result)
    view = result.structured_content
    assert view["number"] == "INC0010001"
    assert view["caller"] == "Rahul Verma"
    assert (view["category"], view["assignment_group"]) == ("", "")
    assert (view["impact"], view["urgency"], view["priority"], view["state"]) == (
        "3",
        "3",
        "5",
        "1",
    )

    resolved = (await mcp.call_tool("get_incident", {"number": "INC0010008"})).structured_content
    assert resolved["assignment_group"] == "Network Operations"
    assert resolved["state"] == "6"


async def test_get_incident_not_found(mcp: Client) -> None:
    result = await mcp.call_tool("get_incident", {"number": "INC0099999"})
    assert result.is_error
    assert "INC0099999" in text(result) and "not found" in text(result).lower()


async def test_update_changes_only_given_fields_and_priority_is_derived(
    mcp: Client, live_server: str
) -> None:
    result = await mcp.call_tool(
        "update_incident",
        {
            "number": "INC0010001",
            "category": "network",
            "impact": "2",
            "urgency": "2",
            "work_note": "Triage: VPN instability.",
        },
    )
    assert not result.is_error, text(result)
    view = result.structured_content
    assert (view["category"], view["priority"]) == ("network", "3")
    assert view["short_description"] == "VPN keeps disconnecting every 10 minutes"  # untouched
    assert work_notes(live_server, "INC0010001") == ["Triage: VPN instability."]


async def test_update_with_nothing_to_change(mcp: Client, mock_admin: httpx.Client) -> None:
    result = await mcp.call_tool("update_incident", {"number": "INC0010001"})
    assert result.is_error
    assert "nothing to update" in text(result).lower()
    assert attempts(mock_admin, "PATCH") == 0


async def test_servicenow_validation_detail_reaches_the_model(mcp: Client) -> None:
    result = await mcp.call_tool("update_incident", {"number": "INC0010001", "state": "6"})
    assert result.is_error
    assert "close_code" in text(result)  # the detail is what lets the model fix its call


async def test_assign_group_by_name_case_insensitive(mcp: Client, live_server: str) -> None:
    result = await mcp.call_tool(
        "assign_group",
        {"number": "INC0010003", "group": "database administration", "work_note": "DB timeouts."},
    )
    assert not result.is_error, text(result)
    assert result.structured_content["assignment_group"] == "Database Administration"
    assert work_notes(live_server, "INC0010003") == ["DB timeouts."]


async def test_unknown_group_lists_valid_names(mcp: Client, mock_admin: httpx.Client) -> None:
    result = await mcp.call_tool("assign_group", {"number": "INC0010001", "group": "Netwrk Ops"})
    assert result.is_error
    assert "Network Operations" in text(result) and "Security Operations" in text(result)
    assert attempts(mock_admin, "PATCH") == 0


async def test_create_incident_is_idempotent(mcp: Client, mock_admin: httpx.Client) -> None:
    args = {
        "short_description": "Printer jam on floor 2",
        "description": "Paper stuck in tray 2.",
        "correlation_id": "<msg-42@mail.example.com>",
        "caller_user_name": "asha.rao",
    }
    first = await mcp.call_tool("create_incident", args)
    second = await mcp.call_tool("create_incident", args)
    assert not first.is_error, text(first)
    assert first.structured_content["number"] == second.structured_content["number"]
    assert first.structured_content["caller"] == "Asha Rao"
    assert first.structured_content["state"] == "1"
    assert attempts(mock_admin, "POST") == 1


async def test_create_incident_unknown_caller(mcp: Client, mock_admin: httpx.Client) -> None:
    result = await mcp.call_tool(
        "create_incident",
        {
            "short_description": "Printer jam",
            "description": "",
            "correlation_id": "c-1",
            "caller_user_name": "nobody.here",
        },
    )
    assert result.is_error and "nobody.here" in text(result)
    assert attempts(mock_admin, "POST") == 0


async def test_outage_is_reported_without_internals(
    mcp: Client, mock_admin: httpx.Client, live_server: str
) -> None:
    mock_admin.post("/_mock/faults", json={"status": 503, "count": 10}).raise_for_status()
    result = await mcp.call_tool("get_incident", {"number": "INC0010001"})
    assert result.is_error
    assert "unavailable" in text(result).lower()
    assert live_server not in text(result) and "Traceback" not in text(result)


async def test_bad_credentials_are_reported_without_secrets(
    live_server: str, mock_admin: httpx.Client
) -> None:
    server = build_server(settings_for(live_server, snow_password="hunter2-secret"))
    async with Client(server) as client:
        result = await client.call_tool("get_incident", {"number": "INC0010001"})
    message = text(result)
    assert result.is_error
    assert "credential" in message.lower() or "permission" in message.lower()
    assert "hunter2-secret" not in message and USER not in message


async def test_end_to_end_over_stdio(live_server: str, mock_admin: httpx.Client, tmp_path: Path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "triage.mcp_server"],
        env={"SNOW_BASE_URL": live_server, "SNOW_USERNAME": USER, "SNOW_PASSWORD": PASSWORD},
        cwd=tmp_path,  # no .env here, so only the env above applies
    )
    async with Client(params) as client:
        assert {t.name for t in (await client.list_tools()).tools} == TOOL_NAMES
        result = await client.call_tool("get_incident", {"number": "INC0010002"})
    assert not result.is_error, text(result)
    assert result.structured_content["caller"] == "Meera Iyer"
