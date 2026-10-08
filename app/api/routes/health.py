import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    database: str


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Runs `SELECT 1` against the database. The body never contains connection details.",
    responses={
        200: {"content": {"application/json": {"example": {"status": "ok", "database": "ok"}}}},
        503: {
            "model": HealthResponse,
            "description": "The database is unreachable.",
            "content": {
                "application/json": {"example": {"status": "unavailable", "database": "error"}}
            },
        },
    },
)
def health(session: Annotated[Session, Depends(get_session)]):
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Health check failed")
        return JSONResponse({"status": "unavailable", "database": "error"}, status_code=503)
    return HealthResponse(status="ok", database="ok")
