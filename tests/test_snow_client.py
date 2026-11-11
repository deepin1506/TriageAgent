"""Spec for the Phase 0 exercise (triage/snow_client.py). Runs against the mock on a real port.

Tests fail with NotImplementedError until you implement TODO 1 and TODO 2.
"""

import time
from collections.abc import AsyncIterator

import httpx
import pytest

from mock_snow.seed import INCIDENT_IDS
from tests.conftest import PASSWORD, TOKEN, USER
from triage.snow_client import SnowClient, SnowError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client(live_server: str, mock_admin: httpx.Client) -> AsyncIterator[SnowClient]:
    async with SnowClient(
        live_server, username=USER, password=PASSWORD, timeout=2.0, backoff_base=0.01
    ) as c:
        yield c


def add_fault(admin: httpx.Client, **spec) -> None:
    admin.post("/_mock/faults", json=spec).raise_for_status()


def attempts(admin: httpx.Client, method: str | None = None) -> int:
    log = admin.get("/_mock/requests").json()
    return sum(1 for r in log if method is None or r["method"] == method)


# --- happy path ---------------------------------------------------------------


async def test_get_record_returns_the_result_object(client: SnowClient) -> None:
    rec = await client.get_record("incident", INCIDENT_IDS["INC0010001"])
    assert rec["number"] == "INC0010001"


async def test_query_returns_a_list(client: SnowClient) -> None:
    rows = await client.query("incident", "active=true^categoryISEMPTY", limit=50)
    assert len(rows) == 6 and all(r["category"] == "" for r in rows)


async def test_bearer_token(live_server: str, mock_admin: httpx.Client) -> None:
    async with SnowClient(live_server, token=TOKEN) as c:
        assert len(await c.query("sys_user_group")) == 7


# --- errors that must NOT be retried ----------------------------------------------


async def test_401_raises_without_retry(live_server: str, mock_admin: httpx.Client) -> None:
    async with SnowClient(live_server, username=USER, password="wrong", backoff_base=0.01) as c:
        with pytest.raises(SnowError) as exc:
            await c.query("incident")
    assert exc.value.status == 401
    assert exc.value.message == "User Not Authenticated"
    assert attempts(mock_admin) == 1


async def test_400_is_parsed_from_envelope_and_not_retried(
    client: SnowClient, mock_admin: httpx.Client
) -> None:
    with pytest.raises(SnowError) as exc:
        await client.create_record("incident", {"short_description": "x", "category": "printers"})
    assert exc.value.status == 400
    assert exc.value.message == "Invalid choice value"
    assert "category" in exc.value.detail
    assert attempts(mock_admin) == 1


async def test_404_raises(client: SnowClient) -> None:
    with pytest.raises(SnowError) as exc:
        await client.get_record("incident", "0" * 32)
    assert exc.value.status == 404


# --- transient errors on reads ------------------------------------------------------


async def test_retries_503_then_succeeds(client: SnowClient, mock_admin: httpx.Client) -> None:
    add_fault(mock_admin, status=503, count=2)
    assert len(await client.query("sys_user_group")) == 7
    assert attempts(mock_admin) == 3


async def test_gives_up_after_max_retries(client: SnowClient, mock_admin: httpx.Client) -> None:
    add_fault(mock_admin, status=503, count=10)
    with pytest.raises(SnowError) as exc:
        await client.query("sys_user_group")
    assert exc.value.status == 503
    assert attempts(mock_admin) == 4  # 1 attempt + max_retries (3)


async def test_honours_retry_after(client: SnowClient, mock_admin: httpx.Client) -> None:
    add_fault(mock_admin, status=429, retry_after=1)
    start = time.monotonic()
    await client.query("sys_user_group")
    assert time.monotonic() - start >= 0.9  # backoff_base=0.01 alone would be ~instant
    assert attempts(mock_admin) == 2


async def test_read_timeout_on_get_is_retried(live_server: str, mock_admin: httpx.Client) -> None:
    add_fault(mock_admin, status=None, delay_ms=1500, method="GET")
    async with SnowClient(
        live_server, username=USER, password=PASSWORD, timeout=0.5, backoff_base=0.01
    ) as c:
        assert len(await c.query("sys_user_group")) == 7
    assert attempts(mock_admin) == 2


# --- writes: the dangerous part ---------------------------------------------------


async def test_post_is_not_retried_on_5xx(client: SnowClient, mock_admin: httpx.Client) -> None:
    add_fault(mock_admin, status=503, when="after", method="POST")
    with pytest.raises(SnowError) as exc:
        await client.create_record("incident", {"short_description": "x", "correlation_id": "c-1"})
    assert exc.value.status == 503
    assert attempts(mock_admin, "POST") == 1
    # ...and the write DID happen. This is why blind POST retries create duplicates.
    assert len(await client.query("incident", "correlation_id=c-1")) == 1


async def test_post_read_timeout_is_not_retried(live_server: str, mock_admin: httpx.Client) -> None:
    # Server commits the write, then takes 1s to respond; the client gives up at 0.3s.
    add_fault(mock_admin, status=None, delay_ms=1000, when="after", method="POST")
    async with SnowClient(
        live_server, username=USER, password=PASSWORD, timeout=0.3, backoff_base=0.01
    ) as c:
        with pytest.raises(SnowError) as exc:
            await c.create_record("incident", {"short_description": "x", "correlation_id": "c-2"})
        assert exc.value.status == 0
        assert attempts(mock_admin, "POST") == 1
        assert len(await c.query("incident", "correlation_id=c-2")) == 1  # it was written


async def test_post_is_retried_on_429(client: SnowClient, mock_admin: httpx.Client) -> None:
    add_fault(mock_admin, status=429, retry_after=0, method="POST")
    rec = await client.create_record("incident", {"short_description": "rate limited once"})
    assert rec["number"].startswith("INC")
    assert attempts(mock_admin, "POST") == 2


async def test_unreachable_server_gives_status_0() -> None:
    # Nothing listens on port 9 -> connection refused (or a connect timeout on some OSes).
    async with SnowClient(
        "http://127.0.0.1:9", username=USER, password=PASSWORD, timeout=1.0, backoff_base=0.01
    ) as c:
        with pytest.raises(SnowError) as exc:
            await c.create_record("incident", {"short_description": "x"})
    assert exc.value.status == 0


# --- TODO 2: idempotent create --------------------------------------------------------


async def test_idempotent_create_dedupes(client: SnowClient, mock_admin: httpx.Client) -> None:
    a = await client.create_incident_idempotent({"short_description": "Printer jam"}, "mail-1")
    b = await client.create_incident_idempotent({"short_description": "Printer jam"}, "mail-1")
    assert a["sys_id"] == b["sys_id"]
    assert a["correlation_id"] == "mail-1"
    assert attempts(mock_admin, "POST") == 1


async def test_idempotent_create_survives_a_lost_response(
    client: SnowClient, mock_admin: httpx.Client
) -> None:
    add_fault(mock_admin, status=503, when="after", method="POST")  # written, then 503
    rec = await client.create_incident_idempotent({"short_description": "VPN down"}, "mail-2")
    assert rec["correlation_id"] == "mail-2"
    assert len(await client.query("incident", "correlation_id=mail-2")) == 1
    assert attempts(mock_admin, "POST") == 1


async def test_idempotent_create_retries_when_nothing_was_written(
    client: SnowClient, mock_admin: httpx.Client
) -> None:
    add_fault(mock_admin, status=503, when="before", method="POST")  # failed, nothing written
    rec = await client.create_incident_idempotent({"short_description": "VPN down"}, "mail-3")
    assert rec["correlation_id"] == "mail-3"
    assert len(await client.query("incident", "correlation_id=mail-3")) == 1
    assert attempts(mock_admin, "POST") == 2


async def test_idempotent_create_does_not_swallow_validation_errors(
    client: SnowClient, mock_admin: httpx.Client
) -> None:
    with pytest.raises(SnowError) as exc:
        await client.create_incident_idempotent({"short_description": "x", "impact": "9"}, "mail-4")
    assert exc.value.status == 400
    assert attempts(mock_admin, "POST") == 1


async def test_idempotent_create_escapes_carets_in_correlation_id(
    client: SnowClient, mock_admin: httpx.Client
) -> None:
    # correlation_id ends up inside an encoded query. An unescaped '^' starts a new query
    # term (encoded-query injection), so the lookup silently misses and you get duplicates.
    # The Table API escapes a literal caret as '^^'.
    cid = "mail^ORDERBYnumber"
    a = await client.create_incident_idempotent({"short_description": "Caret test"}, cid)
    b = await client.create_incident_idempotent({"short_description": "Caret test"}, cid)
    assert a["sys_id"] == b["sys_id"]
    assert attempts(mock_admin, "POST") == 1


async def test_local_urls_ignore_proxy_settings(
    live_server: str, mock_admin: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Corporate machines and VPNs often set a system proxy. If local traffic went through
    # it, the mock would never see the request. Port 9 = nothing listening.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    async with SnowClient(live_server, username=USER, password=PASSWORD) as c:
        assert len(await c.query("sys_user_group")) == 7


def test_non_json_success_body_is_a_snow_error_not_a_crash() -> None:
    from triage.snow_client import _result

    page = httpx.Response(200, text="<html>Blocked</html>", headers={"Content-Type": "text/html"})
    with pytest.raises(SnowError) as exc:
        _result(page)
    assert exc.value.status == 502 and "proxy" in exc.value.detail
