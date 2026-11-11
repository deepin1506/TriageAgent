import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from mock_snow.app import create_app
from mock_snow.settings import MockSettings
from triage.config import Settings

USER, PASSWORD, TOKEN = "tester", "s3cret", "tok-123"


def settings_for(base_url: str, **overrides) -> Settings:
    """Agent/MCP-server settings pointed at a test mock. No .env, fast retries."""
    values = dict(
        _env_file=None,
        snow_base_url=base_url,
        snow_username=USER,
        snow_password=PASSWORD,
        snow_token=None,
        snow_timeout_s=2.0,
        snow_max_retries=1,
        snow_backoff_base_s=0.01,
    )
    return Settings(**(values | overrides))


def make_settings(db_path: Path) -> MockSettings:
    # Every field explicit + no .env, so a developer's local .env can't leak into tests.
    return MockSettings(
        _env_file=None,
        snow_username=USER,
        snow_password=PASSWORD,
        snow_token=TOKEN,
        snow_mock_db=str(db_path),
        snow_mock_admin=True,
        snow_mock_fault_rate=0.0,
    )


@pytest.fixture
def api(tmp_path: Path) -> Iterator[TestClient]:
    """In-process client for the mock (fast; no real sockets). Authenticated by default."""
    with TestClient(create_app(make_settings(tmp_path / "snow.db"))) as client:
        client.auth = (USER, PASSWORD)
        yield client


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """The mock on a real port, so client timeouts and retries are exercised for real."""
    app = create_app(make_settings(tmp_path_factory.mktemp("live") / "snow.db"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def mock_admin(live_server: str) -> Iterator[httpx.Client]:
    """Admin client for the live mock. Resets DB, faults and request log before each test."""
    with httpx.Client(base_url=live_server) as admin:
        admin.post("/_mock/reset").raise_for_status()
        yield admin
