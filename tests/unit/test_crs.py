import numpy as np
import pytest
import shapely
from pyproj import CRS, Transformer
from shapely.geometry import Point, box

from app.core.errors import AppError, ErrorCode
from app.services.crs import (
    ExtentLimits,
    crs_label,
    resolve_source_crs,
    select_measurement_crs,
)

WGS84 = CRS.from_epsg(4326)
LIMITS = ExtentLimits()


def reproject(geom, transformer):
    return shapely.transform(
        geom, lambda coords: np.column_stack(transformer.transform(coords[:, 0], coords[:, 1]))
    )


def test_label_uses_epsg_when_available():
    assert crs_label(WGS84) == "EPSG:4326"
    assert crs_label(CRS.from_epsg(3857)) == "EPSG:3857"


def test_label_falls_back_to_crs_string():
    custom = CRS.from_proj4(
        "+proj=tmerc +lat_0=12 +lon_0=77 +k=0.9996 +x_0=500 +y_0=0 +datum=WGS84"
    )
    assert not crs_label(custom).startswith("EPSG:")
    assert crs_label(custom)


class TestResolveSourceCrs:
    def test_dataset_crs_is_used(self):
        crs, warnings = resolve_source_crs(WGS84, None)
        assert crs == WGS84
        assert warnings == []

    def test_missing_crs_is_rejected(self):
        with pytest.raises(AppError) as error:
            resolve_source_crs(None, None)
        assert error.value.code == ErrorCode.missing_source_crs
        assert error.value.status_code == 422

    def test_blank_override_counts_as_absent(self):
        with pytest.raises(AppError) as error:
            resolve_source_crs(None, "   ")
        assert error.value.code == ErrorCode.missing_source_crs

    def test_override_used_when_dataset_has_no_crs(self):
        crs, warnings = resolve_source_crs(None, "EPSG:32643")
        assert crs.to_epsg() == 32643
        assert len(warnings) == 1
        assert "supplied by the client" in warnings[0]

    def test_override_ignored_with_warning_when_dataset_has_crs(self):
        crs, warnings = resolve_source_crs(WGS84, "EPSG:32643")
        assert crs == WGS84
        assert len(warnings) == 1
        assert "ignored" in warnings[0]

    @pytest.mark.parametrize("bad", ["banana", "EPSG:99999999", "EPSG:"])
    def test_invalid_override_is_rejected(self, bad):
        with pytest.raises(AppError) as error:
            resolve_source_crs(None, bad)
        assert error.value.code == ErrorCode.unknown_crs

    def test_non_geographic_non_projected_crs_is_unknown(self):
        engineering = CRS.from_wkt(
            'ENGCRS["local",EDATUM["local datum"],CS[Cartesian,2],'
            'AXIS["x",east],AXIS["y",north],UNIT["metre",1]]'
        )
        with pytest.raises(AppError) as error:
            resolve_source_crs(engineering, None)
        assert error.value.code == ErrorCode.unknown_crs


class TestSelectMeasurementCrs:
    def test_northern_hemisphere_zone(self):
        crs, warnings = select_measurement_crs([box(77.59, 12.97, 77.60, 12.98)], WGS84, LIMITS)
        assert crs.to_epsg() == 32643
        assert warnings == []

    def test_southern_hemisphere_zone(self):
        crs, _ = select_measurement_crs([box(151.2, -33.9, 151.21, -33.89)], WGS84, LIMITS)
        assert crs.to_epsg() == 32756

    def test_projected_input_selects_the_same_zone(self):
        to_mercator = Transformer.from_crs(4326, 3857, always_xy=True)
        geom = reproject(box(77.59, 12.97, 77.60, 12.98), to_mercator)
        crs, _ = select_measurement_crs([geom], CRS.from_epsg(3857), LIMITS)
        assert crs.to_epsg() == 32643

    @pytest.mark.parametrize(
        ("lat", "epsg"), [(88.0, 32661), (84.5, 32661), (-88.0, 32761), (-82.0, 32761)]
    )
    def test_polar_latitudes_fall_back_to_ups(self, lat, epsg):
        crs, _ = select_measurement_crs([box(10, lat, 10.5, lat + 0.1)], WGS84, LIMITS)
        assert crs.to_epsg() == epsg

    def test_wide_extent_warns(self):
        geoms = [Point(77.0, 12.0), Point(84.0, 13.0)]  # 7 degrees of longitude
        crs, warnings = select_measurement_crs(geoms, WGS84, LIMITS)
        assert crs is not None
        assert len(warnings) == 1
        assert "less accurate" in warnings[0]

    def test_very_wide_extent_is_rejected(self):
        geoms = [Point(60.0, 12.0), Point(95.0, 13.0)]
        with pytest.raises(AppError) as error:
            select_measurement_crs(geoms, WGS84, LIMITS)
        assert error.value.code == ErrorCode.extent_too_large
        assert error.value.status_code == 422

    def test_tall_extent_is_rejected(self):
        with pytest.raises(AppError) as error:
            select_measurement_crs([Point(77.0, -20.0), Point(77.1, 20.0)], WGS84, LIMITS)
        assert error.value.code == ErrorCode.extent_too_large

    def test_antimeridian_crossing_is_rejected_without_special_casing(self):
        geoms = [Point(179.9, -17.0), Point(-179.9, -17.0)]
        with pytest.raises(AppError) as error:
            select_measurement_crs(geoms, WGS84, LIMITS)
        assert error.value.code == ErrorCode.extent_too_large

    def test_antimeridian_crossing_projected_input_is_rejected(self):
        to_utm = Transformer.from_crs(4326, 32660, always_xy=True)  # UTM zone 60N, covers 180
        geoms = [reproject(Point(x, 17.0), to_utm) for x in (179.9, -179.9)]
        with pytest.raises(AppError) as error:
            select_measurement_crs(geoms, CRS.from_epsg(32660), LIMITS)
        assert error.value.code == ErrorCode.extent_too_large

    def test_custom_limits_are_honoured(self):
        geoms = [Point(77.0, 12.0), Point(78.5, 12.0)]
        with pytest.raises(AppError):
            select_measurement_crs(geoms, WGS84, ExtentLimits(warn_degrees=0.5, max_degrees=1.0))

    @pytest.mark.parametrize("geoms", [[], [None], [None, Point()], [Point()]])
    def test_no_usable_geometry_gives_no_crs(self, geoms):
        crs, warnings = select_measurement_crs(geoms, WGS84, LIMITS)
        assert crs is None
        assert len(warnings) == 1

    def test_null_geometries_are_ignored_when_others_exist(self):
        crs, _ = select_measurement_crs([None, box(77.59, 12.97, 77.60, 12.98)], WGS84, LIMITS)
        assert crs.to_epsg() == 32643
