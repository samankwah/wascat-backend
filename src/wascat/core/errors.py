"""Error responses.

The pre-migration API returned Zod-shaped errors:

    { "error": { "code": "invalid_query",
                 "message": "One or more query parameters are invalid.",
                 "details": { "oktas": ["Number must be less than or equal to 8"] } } }

FastAPI's default handler returns ``{"detail": [...]}`` instead, which would
break every client reading ``error.details.<field>[0]``. Both the shape and
the individual message strings are pinned by recorded fixtures, so the
handlers below replace FastAPI's defaults wholesale.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from wascat.core.envelope import CanonicalJSONResponse

# Zod's field errors are ``{ field: string[] }``. Preserve that even for a
# single message, because clients index into the array.
FieldErrors = dict[str, list[str]]


class AppError(Exception):
    """Base for errors that map onto the public error envelope."""

    status_code: int = 400
    code: str = "bad_request"
    message: str = "The request could not be processed."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: FieldErrors | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        super().__init__(self.message)

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}


class InvalidQueryError(AppError):
    status_code = 400
    code = "invalid_query"
    message = "One or more query parameters are invalid."


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "Not found"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"
    message = "Authentication is required."


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"
    message = "You do not have permission to perform this action."


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    message = "The request conflicts with the current state of the resource."


class ReleaseImmutableError(ConflictError):
    code = "release_immutable"
    message = "Published releases are immutable."


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"
    message = "The uploaded file is too large."


class UnprocessableUploadError(AppError):
    status_code = 422
    code = "unprocessable_upload"
    message = "The uploaded file could not be processed."


def _response(exc: AppError) -> CanonicalJSONResponse:
    # Deliberately no rate-limit headers: the TypeScript error paths used a
    # bare NextResponse.json and never set them, and the fixtures record that.
    return CanonicalJSONResponse(exc.body(), status_code=exc.status_code)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> CanonicalJSONResponse:
        return _response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> CanonicalJSONResponse:
        # Reached only for routes that lean on Pydantic parsing. The public
        # image query builds its own Zod-shaped errors; this is the safety net
        # for admin routes so they never leak FastAPI's default shape.
        details: FieldErrors = {}
        for error in exc.errors():
            field = str(error["loc"][-1]) if error.get("loc") else "_"
            details.setdefault(field, []).append(str(error.get("msg", "Invalid")))
        return _response(InvalidQueryError(details=details))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> CanonicalJSONResponse:
        code = {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
            413: "payload_too_large",
            415: "unsupported_media_type",
            429: "rate_limited",
        }.get(exc.status_code, "error")
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return _response(AppError(detail, status_code=exc.status_code, code=code))
