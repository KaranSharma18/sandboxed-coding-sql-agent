"""Errors as RFC 9457 problem details (`application/problem+json`).

Every `FixloopError` subclass maps to exactly one status code, so clients can branch on
`code` (stable) and show `detail` (human readable).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from fixloop_core import errors

STATUS = {
    errors.InvalidInput: 422,
    errors.NotFound: 404,
    errors.Conflict: 409,
    errors.QuotaExceeded: 429,
    errors.Overloaded: 503,
    errors.Unauthorized: 401,
    errors.Forbidden: 403,
    errors.TooLarge: 413,
    errors.BudgetExhausted: 429,
}


def problem(
    status: int, code: str, detail: str, headers: dict[str, str] | None = None, **extra: Any
) -> JSONResponse:
    body = {
        "type": f"https://fixloop.dev/problems/{code}",
        "title": code.replace("_", " "),
        "status": status,
        "detail": detail,
        "code": code,
        **extra,
    }
    return JSONResponse(body, status_code=status, headers=headers, media_type="application/problem+json")


def install(app: FastAPI) -> None:
    @app.exception_handler(errors.FixloopError)
    async def fixloop_error(_: Request, exc: errors.FixloopError) -> JSONResponse:
        status = next((code for kind, code in STATUS.items() if isinstance(exc, kind)), 500)
        headers = {}
        retry_after = getattr(exc, "retry_after", None)
        if retry_after:
            headers["Retry-After"] = str(int(retry_after))
        detail = exc.message if status < 500 else "internal error"
        return problem(status, exc.code, detail, headers or None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [
            {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()[:50]
        ]
        return problem(422, "invalid_request", "the request does not match the schema", errors=issues)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            404: "not_found",
            405: "method_not_allowed",
            401: "unauthorized",
            403: "forbidden",
            413: "too_large",
        }.get(exc.status_code, "http_error")
        return problem(exc.status_code, code, str(exc.detail), dict(exc.headers or {}) or None)
