from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        error: str,
        message: str,
        **extra: Any,
    ) -> None:
        self.status_code = status_code
        self.error = error
        self.message = message
        self.extra = extra
        super().__init__(message)

    def body(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.error, "message": self.message}
        payload.update(self.extra)
        return payload


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())
