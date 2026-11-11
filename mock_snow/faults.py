"""Fault injection, so client retry / timeout / idempotency logic can be tested for real.

Queue faults with POST /_mock/faults. Each matching /api/now request consumes one.
  when="before": wait delay_ms, then fail with `status` without touching the DB
                 (safe to retry). status=null means "just slow".
  when="after":  run the request first (the write happens and is committed!), then wait
                 delay_ms and return `status` instead of the real response. Simulates a lost
                 or slow response, the case that makes blind POST retries dangerous.
                 status=null means "real response, but slow" (client times out after the write).
"""

import asyncio
import random
import threading
from collections import deque
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .errors import envelope

_MESSAGES = {
    429: "Rate limit exceeded",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


class FaultSpec(BaseModel):
    status: int | None = Field(default=503, ge=400, le=599)
    count: int = Field(default=1, ge=1, le=1000)
    when: Literal["before", "after"] = "before"
    delay_ms: int = Field(default=0, ge=0, le=60_000)
    retry_after: int | None = Field(default=None, ge=0, le=60)
    method: str | None = None  # e.g. "POST"; None = any
    path_contains: str | None = None  # e.g. "/incident"; None = any


class FaultInjector:
    def __init__(self, random_rate: float = 0.0) -> None:
        self.random_rate = random_rate
        self.request_log: deque[dict[str, str]] = deque(maxlen=1000)
        self._queue: list[list] = []  # [spec, remaining]
        self._lock = threading.Lock()

    def add(self, spec: FaultSpec) -> None:
        with self._lock:
            self._queue.append([spec, spec.count])

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()

    def take(self, method: str, path: str) -> FaultSpec | None:
        with self._lock:
            for entry in self._queue:
                spec: FaultSpec = entry[0]
                if spec.method and spec.method.upper() != method:
                    continue
                if spec.path_contains and spec.path_contains not in path:
                    continue
                entry[1] -= 1
                if entry[1] <= 0:
                    self._queue.remove(entry)
                return spec
        if self.random_rate and random.random() < self.random_rate:
            return FaultSpec(status=503, retry_after=1)
        return None


def _error_response(spec: FaultSpec) -> JSONResponse:
    assert spec.status is not None
    headers = {"Retry-After": str(spec.retry_after)} if spec.retry_after is not None else None
    body = envelope(_MESSAGES.get(spec.status, "Injected fault"), "Injected by mock fault injector")
    return JSONResponse(body, spec.status, headers)


def install_fault_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def inject_faults(request: Request, call_next):
        if not request.url.path.startswith("/api/now/"):
            return await call_next(request)

        injector: FaultInjector = request.app.state.faults
        injector.request_log.append(
            {"method": request.method, "path": request.url.path, "query": request.url.query}
        )
        spec = injector.take(request.method, request.url.path)
        if spec is None:
            return await call_next(request)

        if spec.when == "before":
            if spec.delay_ms:
                await asyncio.sleep(spec.delay_ms / 1000)
            return _error_response(spec) if spec.status else await call_next(request)

        # "after": the real request runs to completion (and commits) first.
        real = await call_next(request)
        body = b"".join([chunk async for chunk in real.body_iterator])
        if spec.delay_ms:
            await asyncio.sleep(spec.delay_ms / 1000)
        if spec.status:
            return _error_response(spec)
        return Response(body, real.status_code, dict(real.headers), real.media_type)
