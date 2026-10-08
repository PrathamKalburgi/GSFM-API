from typing import Any

from pydantic import BaseModel, Field

from app.core.errors import ERROR_DESCRIPTIONS, ERROR_STATUS, ErrorCode


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable explanation, safe to show to users.")
    request_id: str | None = Field(description="Matches the X-Request-ID response header.")


class ErrorResponse(BaseModel):
    error: ErrorDetail


def error_responses(*codes: ErrorCode) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses=` entries for the error codes a route can return."""
    grouped: dict[int, list[ErrorCode]] = {}
    for code in codes:
        grouped.setdefault(ERROR_STATUS[code], []).append(code)
    return {
        status: {
            "model": ErrorResponse,
            "description": "\n".join(f"- `{code}`: {ERROR_DESCRIPTIONS[code]}" for code in group),
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": str(group[0]),
                            "message": ERROR_DESCRIPTIONS[group[0]],
                            "request_id": "5f0c2a9e1d3b4c7f8a6e",
                        }
                    }
                }
            },
        }
        for status, group in grouped.items()
    }
