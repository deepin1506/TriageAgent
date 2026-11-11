"""ServiceNow-style error envelope: {"error": {"message", "detail"}, "status": "failure"}."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class SnowAPIError(Exception):
    def __init__(self, status: int, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


def envelope(message: str, detail: str = "") -> dict:
    return {"error": {"message": message, "detail": detail}, "status": "failure"}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(SnowAPIError)
    async def _snow(_: Request, exc: SnowAPIError) -> JSONResponse:
        headers = {"WWW-Authenticate": 'Basic realm="mock-snow"'} if exc.status == 401 else None
        return JSONResponse(envelope(exc.message, exc.detail), exc.status, headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        parts = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("query", "body"))
            parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
        return JSONResponse(envelope("Invalid request", "; ".join(parts)), 400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(envelope(str(exc.detail)), exc.status_code)
