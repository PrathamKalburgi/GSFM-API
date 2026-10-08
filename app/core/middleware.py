"""Request ID propagation and one structured access-log line per request."""

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import ErrorCode, error_body

logger = logging.getLogger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"
# Client-supplied IDs are echoed into logs and headers, so only a safe shape is accepted.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._incoming_id(scope) or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status = 500

        async def send_with_header(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            logger.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )

    @staticmethod
    def _incoming_id(scope: Scope) -> str | None:
        for name, value in scope["headers"]:
            if name == b"x-request-id":
                candidate = value.decode("latin-1")
                return candidate if _SAFE_ID.match(candidate) else None
        return None


TOO_LARGE_MESSAGE = "The upload exceeds the maximum allowed size."


class RequestBodyTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail=TOO_LARGE_MESSAGE)


class BodySizeLimitMiddleware:
    """Stop reading a request body once it passes a byte limit.

    Starlette parses a multipart upload completely before the route runs, so without this
    a client could send an unbounded body. A `Content-Length` over the limit is refused
    immediately; otherwise bytes are counted as they arrive, so a missing or lying header
    does not help. The exact per-file limit is enforced again when the file is saved.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = self._declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            request_id = scope.get("state", {}).get("request_id")
            body = error_body(ErrorCode.file_too_large, TOO_LARGE_MESSAGE, request_id)
            await JSONResponse(body, status_code=413)(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLarge()
            return message

        await self.app(scope, limited_receive, send)

    @staticmethod
    def _declared_length(scope: Scope) -> int | None:
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return None
        return None
