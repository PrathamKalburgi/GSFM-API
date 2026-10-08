from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.models import FeatureResult

EXAMPLE = {
    "file_id": "b74e4198-6c9f-4893-bbaa-2552ae9c20ef",
    "crs": "EPSG:4326",
    "measurement_crs": "EPSG:32643",
    "total": 120,
    "limit": 100,
    "offset": 0,
    "summary": {
        "features": 120,
        "by_status": {"MEASURED": 110, "NOT_REQUIRED": 8, "UNSUPPORTED": 1, "INVALID_GEOMETRY": 1},
        "total_area_m2": 245072.18,
        "total_length_m": 18234.55,
    },
    "features": [
        {
            "feature_index": 0,
            "source_layer": "Parcels",
            "source_feature_id": "p1",
            "geometry_type": "Polygon",
            "crs": "EPSG:4326",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[77.59, 12.97], [77.5904, 12.97], [77.5904, 12.9704], [77.59, 12.9704],
                     [77.59, 12.97]]
                ],
            },
            "properties": {"Name": "North parcel"},
            "measurement": {
                "area_m2": 1922.72,
                "length_m": None,
                "status": "MEASURED",
                "message": None,
            },
        }
    ],
}  # fmt: skip


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


class MeasurementSummary(BaseModel):
    """Totals over the whole file, not just the current page."""

    features: int
    by_status: dict[str, int]
    total_area_m2: float
    total_length_m: float

    @field_serializer("total_area_m2", "total_length_m")
    def _round_totals(self, value: float) -> float:
        return round(value, 2)


class FeatureMeasurement(BaseModel):
    area_m2: float | None = Field(description="Polygon area in square metres, otherwise null.")
    length_m: float | None = Field(description="Line length in metres, otherwise null.")
    status: str = Field(description="MEASURED, NOT_REQUIRED, UNSUPPORTED or INVALID_GEOMETRY.")
    message: str | None = Field(description="Why there is no measurement, when there is none.")

    @field_serializer("area_m2", "length_m")
    def _round_values(self, value: float | None) -> float | None:
        return _round(value)


class FeatureOut(BaseModel):
    feature_index: int = Field(description="Zero-based index across the whole file.")
    source_layer: str
    source_feature_id: str | None
    geometry_type: str | None = Field(description="Original geometry type; null without geometry.")
    crs: str = Field(description="CRS of the coordinates in `geometry`.")
    geometry: dict[str, Any] | None = Field(
        description="GeoJSON geometry in the source CRS; null without geometry or when "
        "include_geometry=false."
    )
    properties: dict[str, Any]
    measurement: FeatureMeasurement

    @classmethod
    def from_model(cls, feature: FeatureResult, crs: str, include_geometry: bool) -> "FeatureOut":
        return cls(
            feature_index=feature.feature_index,
            source_layer=feature.source_layer,
            source_feature_id=feature.source_feature_id,
            geometry_type=feature.geometry_type,
            crs=crs,
            # Not read at all when excluded: the column is deferred in the query.
            geometry=feature.geometry_json if include_geometry else None,
            properties=feature.properties_json,
            measurement=FeatureMeasurement(
                area_m2=feature.area_m2,
                length_m=feature.length_m,
                status=feature.measurement_status,
                message=feature.measurement_message,
            ),
        )


class MeasurementsResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [EXAMPLE]})

    file_id: str
    crs: str
    measurement_crs: str | None
    total: int = Field(description="Number of features in the whole file.")
    limit: int
    offset: int
    summary: MeasurementSummary
    features: list[FeatureOut]
