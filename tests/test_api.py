"""Phase 6: the HTTP API, end to end. Real graph, real MCP server, real mock over HTTP,
real SQLite checkpoints and registry; only the LLM is scripted."""

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import settings_for
from tests.fakes import ScriptedLLM
from tests.test_graph import NUMBER, SCRIPT, incident, writes
from triage.agent.service import RunRegistry
from triage.api.app import create_app
from triage.config import Settings

KEY = {"X-API-Key": "test-key"}
APPROVE = {"approved": True, "reviewer": "deepin", "comment": "looks right"}
REJECT = {"approved": False, "reviewer": "deepin", "comment": "wrong group"}


def api_settings(live_server: str, tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "api_key": "test-key",
        "checkpoint_db": str(tmp_path / "checkpoints.db"),
        "run_log": str(tmp_path / "runs.jsonl"),
    }
    return settings_for(live_server, **(values | overrides))


@pytest.fixture
def client(live_server: str, mock_admin: httpx.Client, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(api_settings(live_server, tmp_path), llm=ScriptedLLM(SCRIPT))
    with TestClient(app, headers=KEY) as c:  # `with` runs the lifespan (DBs, MCP, ...)
        yield c


def wait_for(client: TestClient, thread: str, status: str, timeout_s: float = 10) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        view = client.get(f"/runs/{thread}").json()
        if view["status"] == status:
            return view
        time.sleep(0.05)
    raise AssertionError(f"run {thread} never reached {status}: {view}")


def test_health_is_open_but_everything_else_needs_the_key(client: TestClient) -> None:
    assert client.get("/health", headers={"X-API-Key": ""}).status_code == 200
    assert client.get("/runs", headers={"X-API-Key": ""}).status_code == 401
    assert client.get("/runs", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/runs").status_code == 200


def test_refuses_to_start_without_an_api_key(live_server: str, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="API_KEY"):
        create_app(api_settings(live_server, tmp_path, api_key=None), llm=ScriptedLLM(SCRIPT))


def test_approve_flow_writes_only_after_approval(
    client: TestClient, mock_admin: httpx.Client, live_server: str
) -> None:
    started = client.post("/triage?wait=true", json={"number": NUMBER})
    assert started.status_code == 200
    run = started.json()
    assert run["status"] == "pending_approval"
    assert run["pending"]["actions"] and run["pending"]["rationale"]
    assert run["usage"]["llm_calls"] == 2
    assert writes(mock_admin) == [], "nothing may be written before approval"

    queue = client.get("/runs", params={"status": "pending_approval"}).json()
    assert [r["thread_id"] for r in queue] == [run["thread_id"]]

    done = client.post(f"/runs/{run['thread_id']}/decision", json=APPROVE).json()
    assert done["status"] == "done" and done["summary"].startswith("Applied")
    assert done["decision"]["reviewer"] == "deepin" and done["pending"] is None
    assert incident(live_server)["assignment_group"] == "Network Operations"

    again = client.post(f"/runs/{run['thread_id']}/decision", json=APPROVE)
    assert again.status_code == 409, "a run can only be decided once"

    m = client.get("/metrics").json()
    assert m["runs"] == 1 and m["outcomes"] == {"applied": 1} and m["approval_rate"] == 1.0
    assert m["tokens"] == {"input": 200, "output": 40}


def test_runs_in_the_background_and_can_be_rejected(
    client: TestClient, mock_admin: httpx.Client
) -> None:
    started = client.post("/triage", json={"number": NUMBER})
    assert started.status_code == 202 and started.json()["status"] == "running"
    thread = started.json()["thread_id"]

    pending = wait_for(client, thread, "pending_approval")
    assert pending["pending"]["number"] == NUMBER

    done = client.post(f"/runs/{thread}/decision", json=REJECT).json()
    assert done["status"] == "done" and done["summary"].startswith("Rejected")
    assert writes(mock_admin) == []
    assert client.get("/metrics").json()["outcomes"] == {"rejected": 1}


def test_one_active_run_per_incident(client: TestClient) -> None:
    first = client.post("/triage?wait=true", json={"number": NUMBER}).json()
    second = client.post("/triage", json={"number": NUMBER})
    assert second.status_code == 409
    assert second.json()["detail"]["thread_id"] == first["thread_id"]


def test_bad_input_and_unknown_runs(client: TestClient) -> None:
    assert client.post("/triage", json={"number": "not-a-ticket"}).status_code == 422
    assert client.get("/runs/nope").status_code == 404
    assert client.post("/runs/nope/decision", json=APPROVE).status_code == 404
    run = client.post("/triage?wait=true", json={"number": NUMBER}).json()
    no_reviewer = {"approved": True, "reviewer": ""}
    assert client.post(f"/runs/{run['thread_id']}/decision", json=no_reviewer).status_code == 422
    assert client.get(f"/runs/{run['thread_id']}").json()["status"] == "pending_approval"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_only_one_of_two_simultaneous_approvals_wins(tmp_path: Path, anyio_backend) -> None:
    async with RunRegistry.open(tmp_path / "r.db") as registry:
        assert await registry.create("t-1", NUMBER)
        assert not await registry.create("t-2", NUMBER), "second active run for the incident"
        await registry.set_status("t-1", "pending_approval")
        assert [await registry.claim("t-1"), await registry.claim("t-1")] == [True, False]


def test_a_restart_recovers_runs_left_running(
    live_server: str, mock_admin: httpx.Client, tmp_path: Path
) -> None:
    settings = api_settings(live_server, tmp_path)
    with TestClient(create_app(settings, llm=ScriptedLLM(SCRIPT)), headers=KEY) as c:
        pending = c.post("/triage?wait=true", json={"number": NUMBER}).json()["thread_id"]
        # Simulate a crash: one run was mid-flight, one was claimed by a reviewer mid-resume

        async def crash() -> None:
            async with RunRegistry.open(settings.checkpoint_db) as r:
                await r.create("INC0010002-crashed", "INC0010002")
                await r.claim(pending)

        asyncio.run(crash())

    with TestClient(create_app(settings, llm=ScriptedLLM(SCRIPT)), headers=KEY) as c:
        crashed = c.get("/runs/INC0010002-crashed").json()
        assert crashed["status"] == "error" and "restart" in crashed["error"]
        # It had reached the approval point, so it's still safely waiting for a decision.
        assert c.get(f"/runs/{pending}").json()["status"] == "pending_approval"


# --- demo mode (the public Render deploy) ---------------------------------------------------


@pytest.fixture
def demo_settings(tmp_path: Path) -> Settings:
    # No live_server: in demo mode the app starts its own private mock ServiceNow.
    return api_settings("http://unused", tmp_path, demo_mode=True)


def test_demo_visitors_can_create_triage_and_approve_without_a_key(demo_settings) -> None:
    with TestClient(create_app(demo_settings, llm=ScriptedLLM(SCRIPT))) as visitor:
        seeded = visitor.get("/demo/incidents").json()
        assert len(seeded) == 8 and visitor.get("/health").json()["demo"] is True

        ticket = {"short_description": "Teams keeps crashing", "description": "Since today."}
        created = visitor.post("/demo/incidents", json=ticket)
        assert created.status_code == 201
        number = created.json()["number"]

        run = visitor.post("/triage?wait=true", json={"number": number}).json()
        assert run["status"] == "pending_approval"
        done = visitor.post(f"/runs/{run['thread_id']}/decision", json=APPROVE).json()
        assert done["summary"].startswith("Applied")

        listed = {i["number"]: i for i in visitor.get("/demo/incidents").json()}
        assert listed[number]["assignment_group"] == "Network Operations"  # the script's pick
        assert visitor.get("/runs", headers={"X-API-Key": "wrong"}).status_code == 401


def test_demo_caps_visitor_spend_but_not_the_owner(demo_settings) -> None:
    capped = demo_settings.model_copy(
        update={"demo_max_runs_per_day": 1, "demo_max_incidents_per_day": 1}
    )
    with TestClient(create_app(capped, llm=ScriptedLLM(SCRIPT))) as visitor:
        assert visitor.post("/triage?wait=true", json={"number": "INC0010001"}).status_code == 200
        over = visitor.post("/triage", json={"number": "INC0010002"})
        assert over.status_code == 429 and "daily" in over.json()["detail"]
        owner = visitor.post("/triage?wait=true", json={"number": "INC0010002"}, headers=KEY)
        assert owner.status_code == 200, "the API key skips the demo caps"

        ticket = {"short_description": "Mouse broken", "description": "Left button."}
        assert visitor.post("/demo/incidents", json=ticket).status_code == 201
        assert visitor.post("/demo/incidents", json=ticket).status_code == 429


def test_demo_routes_do_not_exist_outside_demo_mode(client: TestClient) -> None:
    assert client.get("/demo/incidents").status_code == 404
    assert client.get("/health").json()["demo"] is False
