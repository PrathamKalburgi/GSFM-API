"""Web UI routes for testing and demonstrating the geospatial API."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter(include_in_schema=False)

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
SAMPLES_DIR = STATIC_DIR / "samples"
FALLBACK_SAMPLES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "samples"

ALLOWED_SAMPLES = {"survey.kml", "parcels.zip", "parcels_no_prj.zip"}


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=404, detail="UI not found")
    return HTMLResponse(content=INDEX_HTML.read_text(encoding="utf-8"))


@router.get("/samples/{filename}")
def get_sample(filename: str) -> FileResponse:
    if filename not in ALLOWED_SAMPLES:
        raise HTTPException(status_code=404, detail="Sample not found")

    target = SAMPLES_DIR / filename
    if not target.exists():
        target = FALLBACK_SAMPLES_DIR / filename
    if not target.exists():
        raise HTTPException(status_code=404, detail="Sample not found")

    media_type = (
        "application/zip" if filename.endswith(".zip") else "application/vnd.google-earth.kml+xml"
    )
    return FileResponse(target, media_type=media_type, filename=filename)
