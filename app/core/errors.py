"""Application error types and the exception handlers that emit the error envelope.

This module has no FastAPI imports at module level so the pure geospatial modules
(`crs.py`, `measurement.py`) can raise `AppError` without depending on the web framework;
the handlers import FastAPI inside `register_exception_handlers`.
"""

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    unsupported_format = "unsupported_format"
    file_too_large = "file_too_large"
    archive_limit_exceeded = "archive_limit_exceeded"
    too_many_features = "too_many_features"
    invalid_archive = "invalid_archive"
    missing_shapefile_component = "missing_shapefile_component"
    ambiguous_archive = "ambiguous_archive"
    missing_source_crs = "missing_source_crs"
    unknown_crs = "unknown_crs"
    unreadable_data = "unreadable_data"
    empty_dataset = "empty_dataset"
    extent_too_large = "extent_too_large"
    validation_error = "validation_error"
    file_not_found = "file_not_found"
    internal_error = "internal_error"
    # Framework-level responses (unknown route, wrong method, malformed request body):
    not_found = "not_found"
    method_not_allowed = "method_not_allowed"
    bad_request = "bad_request"


ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.unsupported_format: 415,
    ErrorCode.file_too_large: 413,
    ErrorCode.archive_limit_exceeded: 413,
    ErrorCode.too_many_features: 413,
    ErrorCode.invalid_archive: 400,
    ErrorCode.missing_shapefile_component: 400,
    ErrorCode.ambiguous_archive: 400,
    ErrorCode.missing_source_crs: 422,
    ErrorCode.unknown_crs: 422,
    ErrorCode.unreadable_data: 422,
    ErrorCode.empty_dataset: 422,
    ErrorCode.extent_too_large: 422,
    ErrorCode.validation_error: 422,
    ErrorCode.file_not_found: 404,
    ErrorCode.internal_error: 500,
    ErrorCode.not_found: 404,
    ErrorCode.method_not_allowed: 405,
    ErrorCode.bad_request: 400,
}

# One line per code: used in the OpenAPI docs and mirrors the README error table.
ERROR_DESCRIPTIONS: dict[ErrorCode, str] = {
    ErrorCode.unsupported_format: "Not .kml or .zip, content does not match the extension, or KMZ",
    ErrorCode.file_too_large: "Upload byte limit exceeded",
    ErrorCode.archive_limit_exceeded: "Too many ZIP entries or uncompressed size too large",
    ErrorCode.too_many_features: "Feature-count limit exceeded",
    ErrorCode.invalid_archive: "Corrupt ZIP, unsafe entries, or nested archives",
    ErrorCode.missing_shapefile_component: ".shp, .shx or .dbf missing",
    ErrorCode.ambiguous_archive: "More than one Shapefile in the ZIP",
    ErrorCode.missing_source_crs: "No usable CRS and no crs field",
    ErrorCode.unknown_crs: "CRS present or supplied but cannot be interpreted",
    ErrorCode.unreadable_data: "The driver cannot read the dataset",
    ErrorCode.empty_dataset: "Zero features",
    ErrorCode.extent_too_large: "Extent too wide for a single measurement projection",
    ErrorCode.validation_error: "Bad request parameters",
    ErrorCode.file_not_found: "Unknown file ID",
    ErrorCode.internal_error: "Anything unexpected; no stack trace, paths or secrets",
    ErrorCode.not_found: "Unknown route",
    ErrorCode.method_not_allowed: "HTTP method not supported on this route",
    ErrorCode.bad_request: "Malformed request that never reached a route",
}


def error_body(code: ErrorCode, message: str, request_id: str | None) -> dict[str, Any]:
    """The one error shape used everywhere."""
    return {"error": {"code": str(code), "message": message, "request_id": request_id}}


class AppError(Exception):
    """An expected failure with a stable error code and a message that is safe to return."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def status_code(self) -> int:
        return ERROR_STATUS[self.code]


HTTP_STATUS_CODES = {
    404: ErrorCode.not_found,
    405: ErrorCode.method_not_allowed,
    413: ErrorCode.file_too_large,
}
MAX_VALIDATION_ISSUES = 5


def register_exception_handlers(app: "FastAPI") -> None:
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def respond(request: Request, status: int, code: ErrorCode, message: str, headers=None):
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            error_body(code, message, request_id), status_code=status, headers=headers
        )

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        return respond(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        issues = [
            f"{'.'.join(str(part) for part in issue['loc'][1:]) or issue['loc'][0]}: {issue['msg']}"
            for issue in exc.errors()[:MAX_VALIDATION_ISSUES]
        ]
        message = "Invalid request. " + "; ".join(issues)
        return respond(request, 422, ErrorCode.validation_error, message)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException):
        code = HTTP_STATUS_CODES.get(
            exc.status_code,
            ErrorCode.bad_request if exc.status_code < 500 else ErrorCode.internal_error,
        )
        message = exc.detail if isinstance(exc.detail, str) else ERROR_DESCRIPTIONS[code]
        return respond(request, exc.status_code, code, message, getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None)
        logger.exception("Unhandled error", extra={"request_id": request_id})
        # Raised errors reach this handler outside the request-ID middleware, so the
        # header is added here. Details stay in the log; the client gets a safe message.
        return respond(
            request,
            500,
            ErrorCode.internal_error,
            "An unexpected error occurred.",
            {"X-Request-ID": request_id} if request_id else None,
        )
