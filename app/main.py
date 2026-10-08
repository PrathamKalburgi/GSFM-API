from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import files, health
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import BodySizeLimitMiddleware, RequestIdMiddleware
from app.db.session import get_engine

MULTIPART_OVERHEAD_BYTES = 1024 * 1024  # form boundaries and the small `crs` field


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_engine()  # creates the default SQLite data/ directory on startup
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Geospatial File Measurement API",
        version="0.1.0",
        description="Upload KML or zipped Shapefiles and get per-feature area and length.",
        openapi_tags=[
            {"name": "files", "description": "Upload files and read their measurements."},
            {"name": "health", "description": "Liveness and database connectivity."},
        ],
        lifespan=lifespan,
    )
    # The last middleware added is the outermost, so every response gets a request ID.
    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES
    )
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(files.router)
    app.include_router(health.router)
    return app


app = create_app()
