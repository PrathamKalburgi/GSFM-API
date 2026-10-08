"""Per-geometry measurement. Pure functions: no web or database imports.

Geometry is reprojected to the measurement CRS before measuring; coordinates in degrees
are never measured directly.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import geopandas as gpd
import numpy as np
import shapely
from pyproj import CRS
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

AREA_TYPES = {"Polygon", "MultiPolygon"}
LENGTH_TYPES = {"LineString", "MultiLineString"}
POINT_TYPES = {"Point", "MultiPoint"}


class MeasurementStatus(StrEnum):
    MEASURED = "MEASURED"
    NOT_REQUIRED = "NOT_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID_GEOMETRY = "INVALID_GEOMETRY"


@dataclass(frozen=True)
class Measurement:
    area_m2: float | None
    length_m: float | None
    status: MeasurementStatus
    message: str | None


def measure_features(
    geoms: Sequence[BaseGeometry | None], source_crs: CRS, measurement_crs: CRS | None
) -> list[Measurement]:
    """Measure every geometry; the result has the same order and length as the input."""
    results: list[Measurement | None] = [None] * len(geoms)
    to_project: list[int] = []
    for position, geom in enumerate(geoms):
        verdict = _classify(geom)
        if verdict is None:
            to_project.append(position)
        else:
            results[position] = verdict

    if to_project:
        if measurement_crs is None:
            raise ValueError("measurement_crs is required when features need measuring")
        projected = _project(
            [geoms[position] for position in to_project], source_crs, measurement_crs
        )
        for position, geom in zip(to_project, projected, strict=True):
            results[position] = _measure_projected(geom)
    return results  # type: ignore[return-value]  # every slot is filled above


def _classify(geom: BaseGeometry | None) -> Measurement | None:
    """Return a final Measurement when no calculation is needed, else None."""
    if geom is None or geom.is_empty:
        return _failed(MeasurementStatus.INVALID_GEOMETRY, "geometry is empty")
    geom_type = geom.geom_type
    if geom_type in POINT_TYPES:
        return Measurement(None, None, MeasurementStatus.NOT_REQUIRED, "points need no measurement")
    if geom_type not in AREA_TYPES | LENGTH_TYPES:
        return _failed(MeasurementStatus.UNSUPPORTED, f"geometry type {geom_type} is not supported")
    if not geom.is_valid:  # checked on the source geometry; never repaired
        return _failed(MeasurementStatus.INVALID_GEOMETRY, explain_validity(geom))
    return None


def _project(
    geoms: list[BaseGeometry], source_crs: CRS, measurement_crs: CRS
) -> list[BaseGeometry]:
    # Measurement is planar, so drop Z first; the source geometry is left untouched.
    flat = shapely.force_2d(np.array(geoms, dtype=object))
    return list(gpd.GeoSeries(flat, crs=source_crs).to_crs(measurement_crs))


def _measure_projected(geom: BaseGeometry) -> Measurement:
    is_area = geom.geom_type in AREA_TYPES
    value = float(geom.area if is_area else geom.length)
    if not math.isfinite(value):
        return _failed(
            MeasurementStatus.INVALID_GEOMETRY, "geometry could not be projected for measurement"
        )
    return Measurement(
        area_m2=value if is_area else None,
        length_m=None if is_area else value,
        status=MeasurementStatus.MEASURED,
        message=None,
    )


def _failed(status: MeasurementStatus, message: str) -> Measurement:
    return Measurement(None, None, status, message)
