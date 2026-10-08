"""Orchestrates one upload: detect, extract, read, project, measure, persist."""

import json
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

import shapely
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import UploadedFile
from app.repositories import file_repository
from app.services.archive import extract_shapefile
from app.services.crs import crs_label, resolve_source_crs, select_measurement_crs
from app.services.measurement import Measurement, MeasurementStatus, measure_features
from app.services.reader import FeatureRecord, detect_format, read_dataset

MAX_FILENAME_LENGTH = 255
UNMEASURED = {MeasurementStatus.UNSUPPORTED, MeasurementStatus.INVALID_GEOMETRY}


def process_upload(
    session: Session, upload: Path, filename: str, crs_override: str | None
) -> UploadedFile:
    """Process a saved upload and persist the result. Nothing is stored if any step fails."""
    settings = get_settings()
    display_name = _display_name(filename)
    fmt = detect_format(upload, filename)

    with TemporaryDirectory(prefix="shapefile_") as work_directory:
        if fmt == "SHAPEFILE_ZIP":
            source, encoding = extract_shapefile(
                upload, Path(work_directory), settings.archive_limits
            )
        else:
            source, encoding = upload, None
        dataset = read_dataset(
            source,
            fmt,
            encoding=encoding,
            max_features=settings.max_features,
            layer_name=Path(display_name).stem,
        )  # everything is in memory now; the temporary files are no longer needed

    source_crs, warnings = resolve_source_crs(dataset.crs, crs_override)
    geometries = [feature.geometry for feature in dataset.features]
    measurement_crs, crs_warnings = select_measurement_crs(
        geometries, source_crs, settings.extent_limits
    )
    warnings += crs_warnings
    measurements = measure_features(geometries, source_crs, measurement_crs)

    unmeasured = sum(m.status in UNMEASURED for m in measurements)
    if unmeasured:
        plural = "" if unmeasured == 1 else "s"
        warnings.append(
            f"{unmeasured} feature{plural} could not be measured "
            "(UNSUPPORTED or INVALID_GEOMETRY); see per-feature status."
        )

    file = UploadedFile(
        original_filename=display_name,
        format=dataset.format,
        source_crs=crs_label(source_crs),
        measurement_crs=crs_label(measurement_crs) if measurement_crs else None,
        feature_count=len(dataset.features),
        warnings=warnings,
    )
    rows = [_feature_row(f, m) for f, m in zip(dataset.features, measurements, strict=True)]
    file_repository.create_file_with_features(session, file, rows)
    return file


def _feature_row(feature: FeatureRecord, measurement: Measurement) -> dict[str, Any]:
    geometry = feature.geometry
    return {
        "feature_index": feature.feature_index,
        "source_layer": feature.source_layer,
        "source_feature_id": feature.source_feature_id,
        "geometry_type": None if geometry is None else geometry.geom_type,
        "geometry_json": None if geometry is None else json.loads(shapely.to_geojson(geometry)),
        "properties_json": feature.properties,
        "area_m2": measurement.area_m2,
        "length_m": measurement.length_m,
        "measurement_status": measurement.status.value,
        "measurement_message": measurement.message,
    }


def _display_name(filename: str) -> str:
    """Name for display only; the client's value is never used as a filesystem path."""
    name = PurePosixPath(filename.replace("\\", "/")).name.strip()
    return (name or "unnamed")[:MAX_FILENAME_LENGTH]
