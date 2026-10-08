"""Format detection, layer reading and property normalization.

Each layer is read separately and never concatenated: concatenation would give every
feature the attribute columns of every other layer as nulls.
"""

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from pyogrio.errors import DataLayerError, DataSourceError, FieldError, GeometryError
from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from app.core.errors import AppError, ErrorCode

logger = logging.getLogger(__name__)

Format = Literal["KML", "SHAPEFILE_ZIP"]

ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
KML_SNIFF_BYTES = 16 * 1024
# GDAL's fixed per-feature style fields on KML layers; they are noise for this API.
KML_STYLE_FIELDS = {"altitudeMode", "tessellate", "extrude", "visibility", "drawOrder", "icon"}
KML_CRS = CRS.from_epsg(4326)
READ_ERRORS = (DataSourceError, DataLayerError, FieldError, GeometryError, UnicodeError, ValueError)


@dataclass(frozen=True)
class FeatureRecord:
    feature_index: int
    source_layer: str
    source_feature_id: str | None
    geometry: BaseGeometry | None  # source CRS, as read
    properties: dict[str, Any]  # normalized, JSON-safe


@dataclass(frozen=True)
class Dataset:
    format: Format
    crs: CRS | None  # None only for a Shapefile with no usable CRS
    features: list[FeatureRecord]


def detect_format(path: Path, filename: str) -> Format:
    """Decide the upload format from the extension and the file content."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".kmz":
        raise AppError(
            ErrorCode.unsupported_format,
            "KMZ is not supported. Extract the .kml file from the KMZ and upload that.",
        )
    if suffix == ".zip":
        with open(path, "rb") as handle:
            if handle.read(4) not in ZIP_MAGIC:
                raise AppError(
                    ErrorCode.unsupported_format,
                    "The file has a .zip extension but is not a ZIP archive.",
                )
        return "SHAPEFILE_ZIP"
    if suffix == ".kml":
        with open(path, "rb") as handle:
            head = handle.read(KML_SNIFF_BYTES).decode("utf-8", errors="ignore").lower()
        if "<kml" not in head:
            raise AppError(
                ErrorCode.unsupported_format,
                "The file has a .kml extension but its content is not KML.",
            )
        return "KML"
    raise AppError(
        ErrorCode.unsupported_format,
        "Only .kml files and zipped Shapefiles (.zip) are supported.",
    )


def read_dataset(
    path: Path,
    fmt: Format,
    *,
    encoding: str | None,
    max_features: int,
    layer_name: str | None = None,
) -> Dataset:
    """Read all features. For `SHAPEFILE_ZIP`, `path` is the extracted `.shp`.

    `layer_name` labels Shapefile features (the extracted file is always called `data`,
    so the layer name GDAL reports is not meaningful). KML layers keep their folder names.
    """
    try:
        if fmt == "KML":
            crs, features = _read_kml(path, max_features)
        else:
            crs, features = _read_shapefile(path, encoding, max_features, layer_name)
    except AppError:
        raise
    except READ_ERRORS as exc:
        logger.warning("Could not read dataset", exc_info=True)
        raise AppError(
            ErrorCode.unreadable_data, "The file could not be read as valid KML or Shapefile data."
        ) from exc
    if not features:
        raise AppError(ErrorCode.empty_dataset, "The file contains no features.")
    return Dataset(format=fmt, crs=crs, features=features)


def _read_kml(path: Path, max_features: int) -> tuple[CRS, list[FeatureRecord]]:
    features: list[FeatureRecord] = []
    for layer in pyogrio.list_layers(path)[:, 0]:
        layer = str(layer)
        info = pyogrio.read_info(path, layer=layer)
        _check_feature_limit(len(features) + max(info["features"], 0), max_features)
        integers = _integer_fields(info)
        frame = pyogrio.read_dataframe(path, layer=layer)
        for geometry, properties in _rows(frame):
            raw_id = properties.pop("id", None)
            source_id = None if _is_missing(raw_id) else str(raw_id)
            cleaned = {
                key: safe
                for key, value in properties.items()
                if key not in KML_STYLE_FIELDS and (safe := to_json_safe(value)) is not None
            }
            _restore_integers(cleaned, integers)
            features.append(FeatureRecord(len(features), layer, source_id, geometry, cleaned))
        _check_feature_limit(len(features), max_features)
    return KML_CRS, features


def _read_shapefile(
    path: Path, encoding: str | None, max_features: int, layer_name: str | None
) -> tuple[CRS | None, list[FeatureRecord]]:
    info = pyogrio.read_info(path, encoding=encoding)
    _check_feature_limit(max(info["features"], 0), max_features)
    integers = _integer_fields(info)
    frame = pyogrio.read_dataframe(path, encoding=encoding, fid_as_index=True)
    layer = layer_name or str(pyogrio.list_layers(path)[0][0])
    features = []
    for position, (fid, geometry, properties) in enumerate(_rows_with_index(frame)):
        safe = {key: to_json_safe(value) for key, value in properties.items()}
        _restore_integers(safe, integers)
        features.append(FeatureRecord(position, layer, str(fid), geometry, safe))
    _check_feature_limit(len(features), max_features)
    return frame.crs, features


def _rows(frame: pd.DataFrame):
    for _, geometry, properties in _rows_with_index(frame):
        yield geometry, properties


def _rows_with_index(frame: pd.DataFrame):
    if isinstance(frame, gpd.GeoDataFrame):
        geometries = list(frame.geometry)
        attributes = frame.drop(columns=frame.geometry.name)
    else:  # a layer with no geometry column at all
        geometries = [None] * len(frame)
        attributes = frame
    yield from zip(frame.index, geometries, attributes.to_dict("records"), strict=True)


def _integer_fields(info: dict) -> set[str]:
    """Fields the file declares as integers. pandas turns such a column into floats as soon
    as one row is missing, so `3` would otherwise be reported as `3.0`."""
    return {
        name
        for name, dtype in zip(info["fields"], info["dtypes"], strict=True)
        if str(dtype).startswith(("int", "uint"))
    }


def _restore_integers(properties: dict[str, Any], integer_fields: set[str]) -> None:
    for name in integer_fields:
        value = properties.get(name)
        if isinstance(value, float) and value.is_integer():
            properties[name] = int(value)


def _check_feature_limit(count: int, max_features: int) -> None:
    if count > max_features:
        raise AppError(
            ErrorCode.too_many_features,
            f"The file has more than the allowed {max_features} features.",
        )


def _is_missing(value: Any) -> bool:
    return to_json_safe(value) is None


def to_json_safe(value: Any) -> Any:
    """Convert an attribute value to something JSON (including PostgreSQL JSONB) accepts.

    NaN, NaT, pandas NA and infinity become None: PostgreSQL rejects the `NaN` token that
    Python's JSON encoder would otherwise emit.
    """
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, str | bool | np.bool_):
        return bool(value) if isinstance(value, np.bool_) else value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, int | np.integer):
        return int(value)
    if isinstance(value, float | np.floating):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, np.datetime64):
        stamp = pd.Timestamp(value)
        return None if stamp is pd.NaT else stamp.isoformat()
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list | tuple | np.ndarray):
        return [to_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_json_safe(item) for key, item in value.items()}
    return str(value)
