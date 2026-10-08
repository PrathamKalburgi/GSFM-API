import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import UploadedFile

EXAMPLE = {
    "id": "b74e4198-6c9f-4893-bbaa-2552ae9c20ef",
    "filename": "survey.kml",
    "format": "KML",
    "feature_count": 120,
    "crs": "EPSG:4326",
    "measurement_crs": "EPSG:32643",
    "status": "COMPLETED",
    "warnings": [
        "2 features could not be measured (UNSUPPORTED or INVALID_GEOMETRY); "
        "see per-feature status."
    ],
    "created_at": "2026-10-07T10:00:00Z",
}


class FileSummary(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [EXAMPLE]})

    id: uuid.UUID
    filename: str = Field(description="Display name of the upload; never used as a path.")
    format: Literal["KML", "SHAPEFILE_ZIP"]
    feature_count: int
    crs: str = Field(description="CRS of the input data, for example EPSG:4326.")
    measurement_crs: str | None = Field(
        description="Projected CRS used for all calculations; null when no feature has geometry."
    )
    status: Literal["COMPLETED"]
    warnings: list[str] = Field(description="Non-fatal notes about the upload.")
    created_at: datetime

    @classmethod
    def from_model(cls, file: UploadedFile) -> "FileSummary":
        return cls(
            id=file.id,
            filename=file.original_filename,
            format=file.format,
            feature_count=file.feature_count,
            crs=file.source_crs,
            measurement_crs=file.measurement_crs,
            status=file.status,
            warnings=list(file.warnings),
            created_at=file.created_at,
        )


class FileList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[FileSummary]
