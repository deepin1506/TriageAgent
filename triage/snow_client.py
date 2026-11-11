"""Async client for the (mock) ServiceNow Table API: auth, timeouts, retries, error mapping.

Retry policy in one sentence: retry what is transient AND safe to repeat. A GET can always
be repeated; a POST that may have reached the server cannot (it could create a duplicate),
so POSTs only retry on 429 (rate limited before processing) or when the connection never
opened. Duplicate-safe creation is handled separately by create_incident_idempotent.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "PATCH", "DELETE"})


class SnowError(Exception):
    """A request we gave up on. status=0 means no HTTP response at all (timeout, network)."""

    def __init__(self, status: int, message: str, detail: str = "") -> None:
        super().__init__(f"{status} {message}" + (f": {detail}" if detail else ""))
        self.status = status
        self.message = message
        self.detail = detail


class SnowClient:
    def __init__(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_max: float = 8.0,
    ) -> None:
        headers = {"Accept": "application/json"}
        auth: httpx.Auth | None = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif username is not None and password is not None:
            auth = httpx.BasicAuth(username, password)
        else:
            raise ValueError("Provide a token, or a username and password")

        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        # httpx picks up proxy settings from the environment, and on Windows also from the
        # system (registry) proxy that corporate machines and VPNs set. Local traffic must
        # never go through that proxy, so ignore it for localhost URLs.
        is_local = httpx.URL(base_url).host in LOCAL_HOSTS
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            auth=auth,
            timeout=timeout,
            trust_env=not is_local,
        )

    async def __aenter__(self) -> SnowClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ----------------------------------------------------------------- core request
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send one logical request, retrying transient failures. Returns the 2xx response.

        At most `max_retries` retries (max_retries + 1 attempts). Waits honour Retry-After,
        otherwise exponential backoff with full jitter. Raises SnowError when giving up.
        """
        attempt = 0
        while True:
            try:
                resp = await self._http.request(method, path, params=params, json=json)
            except httpx.TransportError as exc:
                # No HTTP response at all. If the connection never opened, the server never
                # saw the request, so even a POST is safe to send again.
                never_sent = isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
                safe = method in IDEMPOTENT_METHODS or never_sent
                if not safe or attempt >= self.max_retries:
                    raise SnowError(0, "No response", str(exc) or type(exc).__name__) from exc
                wait = self._backoff(attempt)
            else:
                if resp.is_success:
                    return resp
                if not self._retryable(method, resp.status_code) or attempt >= self.max_retries:
                    raise _to_error(resp)
                wait = _retry_after(resp)
                if wait is None:
                    wait = self._backoff(attempt)

            await asyncio.sleep(wait)
            attempt += 1

    @staticmethod
    def _retryable(method: str, status: int) -> bool:
        if status == 429:  # rate limited: rejected before processing, safe for any method
            return True
        return method in IDEMPOTENT_METHODS and status in RETRYABLE_STATUS

    def _backoff(self, attempt: int) -> float:
        """Full jitter: random wait in [0, base * 2^attempt], capped. Spreads retries out so
        many clients don't hammer a recovering server in lockstep."""
        return random.uniform(0, min(self.backoff_max, self.backoff_base * 2**attempt))

    # ---------------------------------------------------------------- table wrappers
    async def get_record(
        self, table: str, sys_id: str, *, display_value: str = "false"
    ) -> dict[str, Any]:
        resp = await self._request(
            "GET",
            f"/api/now/table/{table}/{sys_id}",
            params={"sysparm_display_value": display_value},
        )
        return _result(resp)

    async def query(
        self,
        table: str,
        query: str = "",
        *,
        limit: int = 20,
        offset: int = 0,
        fields: list[str] | None = None,
        display_value: str = "false",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "sysparm_query": query,
            "sysparm_limit": limit,
            "sysparm_offset": offset,
            "sysparm_display_value": display_value,
        }
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        resp = await self._request("GET", f"/api/now/table/{table}", params=params)
        return _result(resp)

    async def create_record(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request("POST", f"/api/now/table/{table}", json=data)
        return _result(resp)

    async def update_record(self, table: str, sys_id: str, data: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request("PATCH", f"/api/now/table/{table}/{sys_id}", json=data)
        return _result(resp)

    # ------------------------------------------------------------ idempotent create
    async def create_incident_idempotent(
        self, data: dict[str, Any], correlation_id: str
    ) -> dict[str, Any]:
        """Create an incident at most once per correlation_id (e.g. the source email's ID).

        The Table API has no idempotency keys, so we use the incident's correlation_id:
        look it up first; POST only if it's missing; after an ambiguous failure (5xx or no
        response: the write MAY have happened) look again before trying another POST.

        Known limit: two workers racing on the same correlation_id can both see "missing"
        and both POST. Fixing that needs the server to enforce uniqueness (a unique index,
        or a ServiceNow business rule), or a single consumer per source (e.g. a queue).
        """
        data = {**data, "correlation_id": correlation_id}
        # '^' separates terms in an encoded query; '^^' is a literal caret. Without this,
        # an ID containing '^' would silently change the query (encoded-query injection).
        query = f"correlation_id={correlation_id.replace('^', '^^')}"

        for _ in range(self.max_retries + 1):
            existing = await self.query("incident", query, limit=1)
            if existing:
                return existing[0]
            try:
                return await self.create_record("incident", data)
            except SnowError as err:
                ambiguous = err.status == 0 or err.status >= 500
                if not ambiguous:
                    raise  # 4xx: the request itself is wrong; retrying won't help
        raise SnowError(0, "Gave up creating incident", f"correlation_id={correlation_id}")


def _retry_after(resp: httpx.Response) -> float | None:
    """Seconds from a Retry-After header, or None if absent/unparseable."""
    try:
        return max(0.0, float(resp.headers["Retry-After"]))
    except (KeyError, ValueError):
        return None


def _to_error(resp: httpx.Response) -> SnowError:
    """Build a SnowError from ServiceNow's {"error": {"message", "detail"}} envelope."""
    try:
        err = resp.json()["error"]
        return SnowError(resp.status_code, err.get("message") or "", err.get("detail") or "")
    except (ValueError, KeyError, TypeError, AttributeError):
        return SnowError(resp.status_code, resp.reason_phrase, resp.text[:500])


def _result(resp: httpx.Response) -> Any:
    """The "result" of a 2xx Table API response. A 2xx body that isn't ServiceNow JSON
    (typically an HTML page from a proxy in the way) becomes a SnowError, not a crash."""
    try:
        return resp.json()["result"]
    except (ValueError, KeyError, TypeError) as exc:
        content_type = resp.headers.get("Content-Type", "?")
        raise SnowError(
            502,
            "Unexpected response",
            f"HTTP {resp.status_code} {content_type} is not a ServiceNow result "
            "(is a proxy intercepting the request?)",
        ) from exc
