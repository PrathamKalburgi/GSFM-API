import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.db.session import get_session
from app.models import UploadedFile
from app.repositories import file_repository as repository
from app.schemas.error import error_responses
from app.schemas.file import FileList, FileSummary
from app.schemas.measurement import FeatureOut, MeasurementsResponse
from app.services.upload_service import process_upload

router = APIRouter(prefix="/api/files", tags=["files"])

CHUNK_SIZE = 1024 * 1024

UPLOAD_DESCRIPTION = """
Upload a `.kml` file or a `.zip` containing exactly one Shapefile (`.shp`, `.shx`, `.dbf`
and a usable `.prj`). Every feature is stored with its geometry, CRS and properties, and
measured: polygon area in m² and line length in m, always after projecting to a metric CRS.

The optional `crs` field (for example `EPSG:32643`) is used only when a Shapefile has no
`.prj`; it is ignored, with a warning, when the file already defines a CRS.
"""

MEASUREMENTS_DESCRIPTION = """
Per-feature results ordered by `feature_index`, plus a `summary` of the **whole file**
(it does not change from page to page). Areas are in m² and lengths in m, measured after
projecting to `measurement_crs`; coordinates in `geometry` stay in the source `crs`.
Set `include_geometry=false` for lightweight pages.
"""

Limit = Annotated[int, Query(ge=1, le=100, description="Page size (1-100).")]
Offset = Annotated[int, Query(ge=0, description="Number of items to skip.")]
FileId = uuid.UUID
SessionDep = Annotated[Session, Depends(get_session)]


@router.post(
    "/",
    status_code=201,
    response_model=FileSummary,
    summary="Upload a KML or zipped Shapefile",
    description=UPLOAD_DESCRIPTION,
    responses=error_responses(
        ErrorCode.unsupported_format,
        ErrorCode.file_too_large,
        ErrorCode.archive_limit_exceeded,
        ErrorCode.too_many_features,
        ErrorCode.invalid_archive,
        ErrorCode.missing_shapefile_component,
        ErrorCode.ambiguous_archive,
        ErrorCode.missing_source_crs,
        ErrorCode.unknown_crs,
        ErrorCode.unreadable_data,
        ErrorCode.empty_dataset,
        ErrorCode.extent_too_large,
        ErrorCode.validation_error,
        ErrorCode.internal_error,
    ),
)
def upload_file(
    file: Annotated[UploadFile, File(description="A .kml file or a .zip with one Shapefile.")],
    session: SessionDep,
    crs: Annotated[
        str | None,
        Form(description="Source CRS for a Shapefile without a .prj, e.g. EPSG:32643."),
    ] = None,
) -> FileSummary:
    with TemporaryDirectory(prefix="upload_") as directory:
        saved = Path(directory) / "upload"  # generated name; the client's name is never a path
        _save_with_limit(file, saved, get_settings().max_upload_bytes)
        uploaded = process_upload(session, saved, file.filename or "", crs)
    return FileSummary.from_model(uploaded)


@router.get(
    "/",
    response_model=FileList,
    summary="List uploaded files",
    description="Most recently uploaded files first.",
    responses=error_responses(ErrorCode.validation_error),
)
def list_files(session: SessionDep, limit: Limit = 20, offset: Offset = 0) -> FileList:
    files, total = repository.list_files(session, limit, offset)
    return FileList(
        total=total,
        limit=limit,
        offset=offset,
        items=[FileSummary.from_model(file) for file in files],
    )


@router.get(
    "/{file_id}/",
    response_model=FileSummary,
    summary="Get one uploaded file",
    responses=error_responses(ErrorCode.file_not_found, ErrorCode.validation_error),
)
def get_file(file_id: FileId, session: SessionDep) -> FileSummary:
    return FileSummary.from_model(_get_or_404(session, file_id))


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Get per-feature measurements",
    description=MEASUREMENTS_DESCRIPTION,
    responses=error_responses(ErrorCode.file_not_found, ErrorCode.validation_error),
)
def get_measurements(
    file_id: FileId,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500, description="Page size (1-500).")] = 100,
    offset: Offset = 0,
    include_geometry: Annotated[
        bool, Query(description="Return each feature's geometry; false gives null instead.")
    ] = True,
) -> MeasurementsResponse:
    file = _get_or_404(session, file_id)
    features, total = repository.list_features(
        session, file_id, limit, offset, include_geometry=include_geometry
    )
    return MeasurementsResponse(
        file_id=str(file.id),
        crs=file.source_crs,
        measurement_crs=file.measurement_crs,
        total=total,
        limit=limit,
        offset=offset,
        summary=repository.get_summary(session, file_id),
        features=[FeatureOut.from_model(f, file.source_crs, include_geometry) for f in features],
    )


@router.delete(
    "/{file_id}/",
    status_code=204,
    summary="Delete an uploaded file and its features",
    responses=error_responses(ErrorCode.file_not_found, ErrorCode.validation_error),
)
def delete_file(file_id: FileId, session: SessionDep) -> Response:
    if not repository.delete_file(session, file_id):
        raise _not_found()
    return Response(status_code=204)


def _get_or_404(session: Session, file_id: uuid.UUID) -> UploadedFile:
    file = repository.get_file(session, file_id)
    if file is None:
        raise _not_found()
    return file


def _not_found() -> AppError:
    return AppError(ErrorCode.file_not_found, "No uploaded file has that ID.")


def _save_with_limit(upload: UploadFile, destination: Path, max_bytes: int) -> None:
    """Copy the upload to disk, counting bytes and aborting at the limit."""
    total = 0
    with open(destination, "wb") as target:
        while chunk := upload.file.read(CHUNK_SIZE):
            total += len(chunk)
            if total > max_bytes:
                raise AppError(
                    ErrorCode.file_too_large,
                    f"The upload is larger than the allowed {max_bytes:,} bytes.",
                )
            target.write(chunk)
