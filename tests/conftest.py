import functools
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mock_snow.app import create_app
from mock_snow.settings import MockSettings
from mock_snow.thread import running_mock
from triage.config import Settings
from triage.kb.chunking import load_chunks
from triage.kb.embeddings import HashEmbedder
from triage.kb.store import KBIndex

USER, PASSWORD, TOKEN = "tester", "s3cret", "tok-123"
REPO = Path(__file__).resolve().parent.parent


@functools.cache
def kb_index_for_tests() -> str:
    """The repo's kb/ indexed once per test session with the offline hash embedder."""
    root = tempfile.mkdtemp(prefix="kb-index-")
    KBIndex.build(load_chunks(REPO / "kb"), HashEmbedder()).save(Path(root) / "hash")
    return root  # kb_index_dir; the server looks in <root>/<embed_provider>


def settings_for(base_url: str, **overrides) -> Settings:
    """Agent/MCP-server settings pointed at a test mock. No .env, fast retries, offline KB."""
    values = dict(
        _env_file=None,
        snow_base_url=base_url,
        snow_username=USER,
        snow_password=PASSWORD,
        snow_token=None,
        snow_timeout_s=2.0,
        snow_max_retries=1,
        snow_backoff_base_s=0.01,
        embed_provider="hash",
        kb_index_dir=kb_index_for_tests(),
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
    with running_mock(make_settings(tmp_path_factory.mktemp("live") / "snow.db")) as url:
        yield url


@pytest.fixture
def mock_admin(live_server: str) -> Iterator[httpx.Client]:
    """Admin client for the live mock. Resets DB, faults and request log before each test."""
    with httpx.Client(base_url=live_server) as admin:
        admin.post("/_mock/reset").raise_for_status()
        yield admin
