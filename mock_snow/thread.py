"""Run the mock in a background thread on a free port (tests, the eval runner, the demo)."""

import secrets
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from .app import create_app
from .settings import MockSettings


@contextmanager
def running_mock(settings: MockSettings, timeout: float = 10.0) -> Iterator[str]:
    """Start the mock, yield its base URL, stop it on exit."""
    app = create_app(settings)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@contextmanager
def private_mock(user: str = "triage.agent") -> Iterator[tuple[str, str, str]]:
    """A fresh, seeded mock that only this process can use: random password, no /_mock admin
    routes, a new database in a temp dir. For the eval runner and the public demo, where
    nobody else should be able to read, reset or break it. Yields (base_url, user, password).
    """
    password = secrets.token_urlsafe(16)
    settings = MockSettings(
        _env_file=None,
        snow_username=user,
        snow_password=password,
        snow_token=None,
        snow_mock_db=str(Path(tempfile.mkdtemp(prefix="snow-")) / "snow.db"),
        snow_mock_admin=False,
        snow_mock_fault_rate=0.0,
    )
    with running_mock(settings) as url:
        yield url, user, password
