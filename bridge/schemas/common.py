"""Unified API response helpers and error types."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | list[Any] | None = None


class ApiResponse(BaseModel):
    ok: bool = True
    data: Any = None
    error: ErrorBody | None = None


class BridgeError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class ControlBusyError(BridgeError):
    def __init__(self, holder: str) -> None:
        super().__init__(
            "control_busy",
            f"control held by {holder}",
            status_code=409,
            details={"holder": holder},
        )


def ok(data: Any = None) -> dict[str, Any]:
    return ApiResponse(ok=True, data=data).model_dump()


def fail(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return ApiResponse(
        ok=False,
        error=ErrorBody(code=code, message=message, details=details),
    ).model_dump()


async def bridge_error_handler(_: Request, exc: BridgeError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=fail(exc.code, exc.message, exc.details),
    )


async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        return JSONResponse(status_code=exc.status_code, content=fail(**detail))
    return JSONResponse(
        status_code=exc.status_code,
        content=fail("http_error", str(detail)),
    )
